# APX 插件同步与自动发布

把 [erinilis/apx-plugin-releases](https://cnb.cool/erinilis/apx-plugin-releases)
（Astral Party Mod Manager 的第三方 APX 插件源）镜像到本仓库，并在每次检测到上游变更时
**自动校验、自动提交、自动打 Release**。

上游只提供裸 git 仓库 + `manifest.json` 清单，没有 API、没有投稿系统；本仓库用 GitHub Actions
把它变成一套可审计、可回滚、可归档的发布流水线。

---

## 它是怎么工作的

```
        ┌─────────────────────────────────────────┐
        │ 上游 CNB（每 6 小时轮询一次）             │
        │ cnb.cool/erinilis/apx-plugin-releases    │
        │   CN_STEAM/ CN_TAPTAP/ CN_BILIBILI/ …    │
        └───────────────────┬─────────────────────┘
                            │ git clone --depth 1
                            ▼
        ┌─────────────────────────────────────────┐
        │ scripts/sync.py                          │
        │ 1. 解析各渠道 manifest.json（v2.0）       │
        │ 2. 逐文件校验 sha256 + size（不符即中止） │
        │ 3. 生成 index.json / .sync-state.json     │
        │ 4. 输出变更渠道 + Release 标签            │
        └───────────────────┬─────────────────────┘
                            │ 有变更
              ┌─────────────┴──────────────┐
              ▼                            ▼
    ┌──────────────────┐        ┌────────────────────────┐
    │ 提交镜像到本仓库   │        │ scripts/package.py      │
    │ CN_*/ 保持原结构   │        │ 按渠道打 zip + SHA256SUMS│
    └──────────────────┘        └───────────┬────────────┘
                                            ▼
                                ┌────────────────────────┐
                                │ GitHub Release（tag:    │
                                │ apx-YYYYMMDD-HHMM）     │
                                └────────────────────────┘
```

### 为什么不是简单 `git mirror`

上游的 `manifest.json` 是**唯一的完整性依据**：每个文件的 `relative_path`、`target_type`
（`locallow` 落 Unity 缓存 / `game_root` 落游戏根）、`sha256`、`size` 都在里面。镜像器逐条
核对，任何一项不符就**整批中止**，避免把半截或损坏的补丁发布出去——这是把 APX 装进
`%LOCALAPPDATA%Low\feimo\...` 后能正常工作的前提。

## 目录结构

默认 `SYNC_MODE=lite`，仓库只存清单（每渠道 3 个 JSON，约 11 KB），二进制只走 Release：

```
.
├── .github/workflows/sync.yml   # 定时 + 手动触发
├── scripts/
│   ├── sync.py                  # 拉取、校验、索引、变更判定
│   └── package.py               # 打 Release 资产
├── CN_STEAM/                    # 镜像内容（与上游同构的目录层级）
│   ├── manifest.json            # 投放清单：路径、哈希、大小、目标根
│   ├── runtime_dependencies.json
│   └── restore/restore_manifest.json
├── CN_TAPTAP/ CN_BILIBILI/ CN_WEGAME/ CN_ANDROID/
├── index.json                   # 聚合索引（本仓库生成）
└── .sync-state.json             # 上次同步基线与指纹
```

`SYNC_MODE=full` 时，每个渠道目录会额外包含 `com.unity.addressables/AssetBundles/**/__data`、
`dependence/*.dll`、`restore/**` 等二进制，体积从 11 KB 涨到约 22 MB/渠道。

保留 `CN_*/` 这层目录，是为了让 raw 直链的**路径**与上游一致：

```
https://raw.githubusercontent.com/<你>/apx-plugin-sync/main/CN_STEAM/manifest.json
```

注意：lite 模式下这个地址只能取到清单，清单里指向的 `dependence/*.dll` 等文件不在仓库里。

## 存储策略（SYNC_MODE）

| | `lite`（默认） | `full` |
|---|---|---|
| 每渠道落盘 | 3 个 JSON ≈ 11 KB | 14 个文件 ≈ 22 MB |
| 全仓 5 渠道 | ≈ 93 KB（内容），磁盘约 188 KB | ≈ 105 MB |
| 一次上游更新增多少 | 几 KB | 9–22 MB（`__data` 为 LZ4 压缩，git 无法有效差分） |
| 能当 raw 插件源 | 否 | 是 |
| 二进制来源 | Release 的 zip（约 15 MB/渠道） | 仓库 + Release 各一份 |

选 `lite` 的理由：单渠道 22 MB 里有 21.6 MB 集中在 4 个 `__data` 上，而这几个文件是已压缩的
AssetBundle，git 每改一个字节就要存一整份新 blob，且旧 blob 永不出历史，仓库只增不减。
上游本身也是**单提交快照**（`Latest output snapshot <时间戳>`），不保留历史，镜像考古没有意义。

改回 `full` 只需把 `.github/workflows/sync.yml` 里的 `SYNC_MODE` 改为 `full`，或直接设环境变量。

## 触发方式

| 方式 | 说明 |
|---|---|
| 定时 | 每 6 小时一次（`cron: 23 */6 * * *`，UTC） |
| 手动 | Actions → *Sync & Release APX Plugins* → Run workflow |
| 参数 `force` | 上游无变更也强制发一次 Release（验证流水线用） |
| 参数 `platforms` | 只处理指定渠道，如 `CN_STEAM,CN_TAPTAP` |

## 产物

每次有变更：

1. 一次提交，消息形如 `sync(apx): 2 platform(s): CN_STEAM, CN_TAPTAP`
2. 一个 Release，tag `apx-20261125-1139`，资产包括：
   - `apx-<平台>-<插件版本>.zip`（含该渠道完整目录，解压即可对位）
   - `SHA256SUMS.txt`
   - `RELEASE_NOTES.md`（把 `RELEASE_NOTES.md` 的表格直接显示在 Release 页）

`index.json` 里可以看到每个渠道的插件版本、游戏版本、清单指纹与补丁/还原体积。

## 已知的上游问题（本仓库已容错）

上游 5 个渠道的 `manifest.json` 里，`runtime_dependencies.json` 这一项的 `size` 与 `sha256`
**全部滞后于实际文件**（例如 CN_STEAM 登记 3108 B / `6a09d0a9…`，实际 3061 B / `1f8e613d…`）。
原因是发布端改完文件没有重算清单。

该文件只是运行时依赖说明，不参与资源投放，因此 `sync.py` 对它**降级为警告**并写入
`index.json` 的 `warnings`；真正的补丁（`com.unity.addressables/**`）与还原备份仍严格校验。
如需全严格模式，把 `scripts/sync.py` 里的 `LENIENT_FILES` 置空即可。

## 让 APMM 客户端用上这个镜像

APMM 把插件源**写死**在源码里：

```rust
// src-tauri/src/apx.rs
pub(crate) const PLUGIN_REPO_RAW_PREFIX: &str =
    "https://cnb.cool/erinilis/apx-plugin-releases/-/git/raw/main/";
```

所以本镜像不能直接替换客户端源，有三条路：

1. **只作分发/归档**：从 Releases 下 zip，解压后作为离线插件包使用（推荐，零改动，与 `lite` 模式兼容）。
2. **fork 客户端源码**：把 `PLUGIN_REPO_RAW_PREFIX` 改成本仓库 raw 地址，`pnpm tauri build`。
   同时 `config/cnb.json` 的 `plugin.rawPrefix` 也要改。
   ⚠️ 走这条路必须先把本仓库切成 `SYNC_MODE=full`，否则客户端能拿到 `manifest.json`、
   但里面的 `dependence/*.dll`、`__data` 全部 404。
3. **本地反代**：把 `cnb.cool/erinilis/apx-plugin-releases/-/git/raw/main/` 指向本仓库
   （hosts/代理层），客户端无感。同样要求 `full`。

## 关于安全

- **不要**把 GitHub token 写进任何文件。本仓库的工作流只用内置 `secrets.GITHUB_TOKEN`，
  需要 `permissions: contents: write`（已在 workflow 中声明）。
- 若要用 PAT 手动触发，请存成仓库 Secret（如 `GH_PAT`），并在 workflow 里 `${{ secrets.GH_PAT }}` 引用。
- APX 插件本身**没有签名**（对比工坊的 Ed25519 `manifest.sig`），只有 SHA-256。本仓库的校验
  只能证明“与上游清单一致”，不能证明“上游可信”——信任锚仍是上游仓库的控制权。

## 合规

补丁内容版权归原作者与游戏厂商，本仓库仅做镜像与校验，不主张任何权利，也不修改补丁内容。
上游问题请到 [erinilis/apx-plugin-releases](https://cnb.cool/erinilis/apx-plugin-releases) 反馈。
