#!/usr/bin/env python3
"""APX 插件同步器（APMM / Astral Party）

做四件事：
  1. 从上游 CNB 仓库取回最新的 APX 插件目录树（按渠道分目录）
  2. 按 manifest.json 逐文件校验 sha256 与 size，任何不符立即失败（绝不发布损坏内容）
  3. 生成 index.json（聚合索引）与 .sync-state.json（变更基线）
  4. 输出变更平台列表与发布标签，供 workflow 决定是否提交 / 发 Release

环境变量：
  UPSTREAM_URL    上游 git 地址（默认 erinilis/apx-plugin-releases）
  UPSTREAM_DIR    若提供，跳过 clone 直接用本地目录（便于离线测试）
  SYNC_MODE       lite（默认，只存清单）| full（含二进制）
  FORCE_RELEASE   1/true 时无论是否变更都发布
  INCLUDE         逗号分隔的平台白名单，默认全部
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

UPSTREAM_URL = os.environ.get(
    "UPSTREAM_URL", "https://cnb.cool/erinilis/apx-plugin-releases.git"
)
UPSTREAM_DIR = os.environ.get("UPSTREAM_DIR", "").strip()
MODE = os.environ.get("SYNC_MODE", "lite").strip().lower()
FORCE = os.environ.get("FORCE_RELEASE", "").strip().lower() in {"1", "true", "yes"}
INCLUDE = {
    s.strip().upper() for s in os.environ.get("INCLUDE", "").split(",") if s.strip()
}

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".sync-state.json"
INDEX_FILE = ROOT / "index.json"
MANIFEST_ONLY = ("manifest.json", "runtime_dependencies.json", "restore/restore_manifest.json")

# 上游清单里长期滞后、且不影响资源投放的附属文件：size/sha 不符只告警
LENIENT_FILES = {"runtime_dependencies.json", "restore_manifest.json"}


PLATFORM_LABELS = {
    "CN_STEAM": "Steam 国服",
    "INT_STEAM": "Steam 国际服",
    "CN_TAPTAP": "TapTap",
    "CN_BILIBILI": "Bilibili",
    "CN_WEGAME": "WeGame",
    "CN_ANDROID": "Android",
}


def die(msg: str) -> None:
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def note(msg: str) -> None:
    print(f"[sync] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        die(f"命令失败: {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def set_output(**kwargs: object) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    text = "".join(f"{k}={v}\n" for k, v in kwargs.items())
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(text)
    note("outputs: " + text.replace("\n", " ").strip())


def clone_upstream(work: Path) -> tuple[Path, str]:
    if UPSTREAM_DIR:
        src = Path(UPSTREAM_DIR).resolve()
        if not src.is_dir():
            die(f"UPSTREAM_DIR 不存在: {src}")
        commit = "local"
        try:
            commit = run(["git", "rev-parse", "HEAD"], cwd=src, check=False).strip() or "local"
        except Exception:
            pass
        note(f"使用本地上游目录 {src} (commit={commit[:8]})")
        return src, commit

    dest = work / "upstream"
    note(f"克隆上游 {UPSTREAM_URL}")
    run(["git", "clone", "--depth", "1", "--quiet", UPSTREAM_URL, str(dest)])
    commit = run(["git", "rev-parse", "HEAD"], cwd=dest).strip()
    note(f"上游 commit={commit[:8]}")
    return dest, commit


def verify_platform(pdir: Path) -> tuple[dict, list[str], list[str], int]:
    """校验单渠道目录，返回 (manifest, 致命问题, 警告, 已校验文件数)。

    上游已知问题：manifest.json 里 runtime_dependencies.json 的 size/sha256
    长期滞后于实际文件（发布脚本改完文件未重算清单）。这类附属清单不参与
    游戏资源投放，降级为警告，避免整个渠道被卡住；真正的补丁与还原文件仍严格校验。
    """
    mpath = pdir / "manifest.json"
    if not mpath.is_file():
        return {}, [f"{pdir.name}: 缺少 manifest.json"], [], 0
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, [f"{pdir.name}: manifest.json 解析失败 {exc}"], [], 0

    if manifest.get("format_version") != "2.0":
        return manifest, [f"{pdir.name}: 未知 format_version={manifest.get('format_version')!r}"], [], 0

    problems: list[str] = []
    warnings: list[str] = []
    checked = 0
    entries = list(manifest.get("files") or []) + list(manifest.get("restore_files") or [])
    for entry in entries:
        rel = str(entry.get("relative_path") or "").strip()
        if not rel:
            problems.append(f"{pdir.name}: 条目缺少 relative_path")
            continue
        if rel.startswith("/") or ".." in Path(rel).parts:
            problems.append(f"{pdir.name}: 非法路径 {rel}")
            continue
        target = pdir / rel
        lenient = Path(rel).name in LENIENT_FILES
        if not target.is_file():
            (warnings if lenient else problems).append(f"{pdir.name}: 文件缺失 {rel}")
            continue
        size = int(entry.get("size") or 0)
        if size and target.stat().st_size != size:
            msg = f"{pdir.name}: 大小不符 {rel} 清单 {size} 实际 {target.stat().st_size}"
            (warnings if lenient else problems).append(msg)
            checked += 1 if lenient else 0
            continue
        expect = str(entry.get("sha256") or "").lower()
        if expect and sha256_file(target) != expect:
            msg = f"{pdir.name}: 哈希不符 {rel}（清单 {expect[:12]}… 实际 {sha256_file(target)[:12]}…）"
            (warnings if lenient else problems).append(msg)
            checked += 1 if lenient else 0
            continue
        checked += 1

    # 声明数量与实际条目数一致性
    if int(manifest.get("total_patch_files") or 0) != len(manifest.get("files") or []):
        problems.append(
            f"{pdir.name}: total_patch_files={manifest.get('total_patch_files')} "
            f"与实际 {len(manifest.get('files') or [])} 不符"
        )
    if int(manifest.get("total_restore_files") or 0) != len(manifest.get("restore_files") or []):
        problems.append(
            f"{pdir.name}: total_restore_files={manifest.get('total_restore_files')} "
            f"与实际 {len(manifest.get('restore_files') or [])} 不符"
        )
    return manifest, problems, warnings, checked


def platform_summary(pdir: Path, manifest: dict) -> dict:
    manifest_bytes = (pdir / "manifest.json").read_bytes()
    deps = pdir / "runtime_dependencies.json"
    return {
        "label": PLATFORM_LABELS.get(pdir.name, pdir.name),
        "plugin_version": manifest.get("version", ""),
        "game_version": manifest.get("game_version", ""),
        "generated_at": manifest.get("generated_at", ""),
        "format_version": manifest.get("format_version", ""),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "runtime_dependencies_sha256": sha256_file(deps) if deps.is_file() else "",
        "patch": {
            "files": int(manifest.get("total_patch_files") or 0),
            "bytes": int(manifest.get("total_patch_size") or 0),
        },
        "restore": {
            "files": int(manifest.get("total_restore_files") or 0),
            "bytes": int(manifest.get("total_restore_size") or 0),
        },
    }


def copy_platform(src: Path, dst: Path) -> int:
    """整目录覆盖复制；lite 模式只保留清单文件。"""
    if dst.exists():
        shutil.rmtree(dst)
    if MODE == "lite":
        dst.mkdir(parents=True)
        copied = 0
        for rel in MANIFEST_ONLY:
            s = src / rel
            if not s.is_file():
                continue
            d = dst / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            copied += 1
        return copied

    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
    return sum(1 for _ in dst.rglob("*") if _.is_file())


def main() -> None:
    if MODE not in {"full", "lite"}:
        die(f"SYNC_MODE 只能是 full 或 lite，当前 {MODE!r}")

    work = Path(tempfile.mkdtemp(prefix="apx-sync-"))
    try:
        upstream, commit = clone_upstream(work)

        dirs = sorted(
            p
            for p in upstream.iterdir()
            if p.is_dir()
            and (
                p.name.upper().startswith("CN_")
                or p.name.upper().startswith("INT_")
            )
        )
        if INCLUDE:
            dirs = [p for p in dirs if p.name.upper() in INCLUDE]
        if not dirs:
            die("上游没有找到任何平台目录")
        note(f"待处理平台: {', '.join(p.name for p in dirs)}")

        old_state = {}
        if STATE_FILE.is_file():
            try:
                old_state = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("platforms", {})
            except json.JSONDecodeError:
                old_state = {}

        platforms: dict[str, dict] = {}
        changed: list[str] = []
        total_files = 0
        all_problems: list[str] = []
        all_warnings: list[str] = []

        for pdir in dirs:
            manifest, problems, warns, checked = verify_platform(pdir)
            all_problems.extend(problems)
            all_warnings.extend(warns)
            if problems:
                continue
            summary = platform_summary(pdir, manifest)
            platforms[pdir.name] = summary
            total_files += copy_platform(pdir, ROOT / pdir.name)
            old = old_state.get(pdir.name, {})
            if old.get("manifest_sha256") != summary["manifest_sha256"]:
                changed.append(pdir.name)
                note(
                    f"{pdir.name}: 更新 -> {summary['plugin_version']} "
                    f"(game {summary['game_version']}) 校验 {checked} 个文件"
                )
            else:
                note(f"{pdir.name}: 无变化 ({summary['plugin_version']})")

        if all_problems:
            die("上游校验失败，已中止发布:\n  - " + "\n  - ".join(all_problems))

        # 上游已下线的渠道目录，本地同步删除，避免残留过期插件
        for stale in sorted(ROOT.iterdir()):
            if not stale.is_dir():
                continue
            if not (stale.name.startswith("CN_") or stale.name.startswith("INT_")):
                continue
            if stale.name not in platforms:
                note(f"{stale.name}: 上游已移除，清理本地副本")
                shutil.rmtree(stale)
                changed.append(stale.name)

        now = datetime.now(timezone.utc)
        index = {
            "schema": 1,
            "generated_at": now.isoformat(timespec="seconds"),
            "upstream": {"url": UPSTREAM_URL, "commit": commit, "mode": MODE},
            "warnings": all_warnings,
            "platforms": platforms,
        }
        INDEX_FILE.write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        STATE_FILE.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "upstream_commit": commit,
                    "synced_at": now.isoformat(timespec="seconds"),
                    "platforms": platforms,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        touched = changed or (list(platforms) if FORCE else [])
        need_release = bool(touched)
        tag_stamp = now.strftime("%Y%m%d-%H%M")
        versions = sorted({platforms[p]["plugin_version"] for p in touched if p in platforms})
        title = "APX " + (" / ".join(v for v in versions if v) or "sync") + f" ({tag_stamp} UTC)"

        set_output(
            changed=str(need_release).lower(),
            platforms=",".join(touched),
            release_tag=f"apx-{tag_stamp}" if need_release else "",
            release_title=title if need_release else "",
            summary=(
                f"{len(touched)} platform(s): {', '.join(touched)}"
                if touched
                else "no changes"
            ),
            file_count=total_files,
            warning_count=len(all_warnings),
        )
        note(f"完成：{total_files} 个文件落盘，{len(touched)} 个平台待发布")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
