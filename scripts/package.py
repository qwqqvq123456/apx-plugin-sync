#!/usr/bin/env python3
"""把同步好的渠道目录打包成 Release 资产。

用法：
  python3 scripts/package.py <输出目录> <tag> [平台,...]

产物：
  <out>/apx-<PLATFORM>-<version>.zip   每个变更渠道一个快照包（含目录结构，解压即可对位）
  <out>/SHA256SUMS.txt                 所有资产的校验和
  <out>/RELEASE_NOTES.md               Release 说明
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.json"


def human(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < step or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= step
    return f"{n} B"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: package.py <输出目录> <tag> [平台,...]", file=sys.stderr)
        return 2

    out = Path(sys.argv[1]).resolve()
    tag = sys.argv[2]
    only = {s.strip().upper() for s in (sys.argv[3] if len(sys.argv) > 3 else "").split(",") if s.strip()}
    out.mkdir(parents=True, exist_ok=True)

    index = json.loads(INDEX.read_text(encoding="utf-8"))
    platforms = index.get("platforms", {})
    commit = (index.get("upstream") or {}).get("commit", "")[:8]

    assets: list[Path] = []
    rows: list[str] = []

    for name, info in platforms.items():
        if only and name not in only:
            continue
        src = ROOT / name
        if not src.is_dir():
            print(f"[package] 跳过 {name}：目录不存在", file=sys.stderr)
            continue
        version = info.get("plugin_version") or "unknown"
        target = out / f"apx-{name}-{version}.zip"
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(src.rglob("*")):
                if path.is_file():
                    zf.write(path, Path(name) / path.relative_to(src))
        assets.append(target)
        rows.append(
            "| {p} | {lab} | {pv} | {gv} | {pf} / {ps} | {rf} / {rs} | {ts} |".format(
                p=name,
                lab=info.get("label", ""),
                pv=version,
                gv=info.get("game_version", ""),
                pf=info.get("patch", {}).get("files", 0),
                ps=human(int(info.get("patch", {}).get("bytes", 0))),
                rf=info.get("restore", {}).get("files", 0),
                rs=human(int(info.get("restore", {}).get("bytes", 0))),
                ts=info.get("generated_at", ""),
            )
        )

    if not assets:
        print("[package] 没有可打包的平台", file=sys.stderr)
        return 1

    sums = out / "SHA256SUMS.txt"
    with sums.open("w", encoding="utf-8") as fh:
        for item in assets:
            fh.write(f"{sha256_file(item)}  {item.name}\n")
    assets.append(sums)

    notes = out / "RELEASE_NOTES.md"
    lines = [
        f"# APX 插件同步快照 `{tag}`",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 上游：`{index.get('upstream', {}).get('url', '')}`  commit `{commit}`",
        f"- 同步模式：`{index.get('upstream', {}).get('mode', 'full')}`",
        "",
        "## 渠道",
        "",
        "| 平台 | 名称 | 插件版本 | 游戏版本 | 补丁 | 还原备份 | 上游生成时间 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        *rows,
        "",
        "## 资产",
        "",
        *[f"- `{a.name}`" for a in assets],
        "",
        "## 使用",
        "",
        "1. 下载对应渠道的 zip，解压得到 `<平台>/` 目录；",
        "2. 目录结构与 APMM 的 APX 插件源一致，可直接作为离线插件包目录使用；",
        "3. 每个文件都经上游 `manifest.json` 的 SHA-256 逐项校验，见 `SHA256SUMS.txt`。",
        "",
        "> 本仓库是第三方镜像，不持有补丁版权；出现问题请回到上游 "
        "[erinilis/apx-plugin-releases](https://cnb.cool/erinilis/apx-plugin-releases) 反馈。",
        "",
    ]
    notes.write_text("\n".join(lines), encoding="utf-8")

    print(f"[package] 生成 {len(assets) - 1} 个平台包 + 校验和 + 说明")
    for a in assets:
        print(f"  {a.name}  {a.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
