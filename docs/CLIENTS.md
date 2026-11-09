# HaoDrive 客户端计划 / HaoDrive Client Plan

状态：草案 v0.1 · 2026-11-09
Status: Draft v0.1 · 2026-11-09

本项目是 haodrive 仓库内的一份**计划文档**，不是新项目。三个客户端将来也放在本仓库里实现。

This is a **planning document** inside the haodrive repository, not a new project. The three clients will be implemented in this repository as well.

---

## 1. 一句话 / In one sentence

做三个**独立运行、不依赖服务器**的图形客户端（Windows exe、Linux 二进制、Android apk），各自直连 Telegram，文件只上传一次；分片合并成逻辑文件的「map」逻辑在本地，规则与现有服务端完全一致；跨设备一致性靠一个**可选的** `map.txt` 覆盖层解决。

Three **standalone GUI clients** (Windows exe, Linux binary, Android apk) that connect to Telegram directly and upload each file only once. The "map" logic that stitches parts into one logical file lives locally and follows exactly the same rules as the existing server. Cross-device consistency is handled by an **optional** `map.txt` overlay.

---

## 2. 架构 / Architecture

```
                 ┌──────────────────┐
  Windows exe ───┤                  │
  Linux binary ──┤  三个客户端       ├──直连──▶ Telegram DC（频道=文件夹，消息=文件）
  Android apk ───┤  （本地登录）      │
                 └──────────────────┘
                          │  可选：读/写频道里的 map.txt

                 ┌──────────────────┐
  浏览器 ────────▶│  haodrive 服务端  ├──▶ Telegram DC
                 └──────────────────┘
                   （Web UI / 分享 / WebDAV，与本计划无依赖）
```

两个世界**平行存在**，共用同一套频道与消息约定，但互不依赖：客户端不需要服务端，服务端也不需要客户端。

Two **parallel** worlds that share the same channel and message conventions but no runtime dependency: the clients do not need the server, and the server does not need the clients.

---

## 3. 目标 / Goals

- 设备直连 Telegram，**文件只传一次**（不再经过 VPS 中转）
- 三端都有图形界面：Windows exe、Linux 二进制、Android apk
- 文件浏览、上传、下载、预览、删除、重命名，全部本地完成
- 逻辑文件（多分片）的映射在本地重建，不依赖数据库
- 与服务端共存：同一账号、同一批频道，互相能看到对方传的文件
- 不新增独立仓库 / 独立项目

- Connect to Telegram directly; **each file is uploaded once** (no VPS hop)
- A GUI on all three platforms: Windows exe, Linux binary, Android apk
- Browse, upload, download, preview, delete, rename — all local
- Rebuild logical-file (multi-part) mappings locally, without a database
- Coexist with the server: same account, same channels, each sees the other's files
- No separate repository or project

---

## 4. 非目标 / Non-goals

- ❌ WebDAV（那是服务端的 rclone 入口，客户端不需要）
- ❌ 分享链接（需要服务端与公网域名）
- ❌ 常驻后台服务 / 无人值守运行
- ❌ 浏览器版本（服务端已经有 Web UI）
- ❌ 多账号切换（第一版只支持一个账号）

- ❌ WebDAV (a server-side rclone entry point; clients do not need it)
- ❌ Share links (these need a server and a public domain)
- ❌ Always-on background service
- ❌ A browser build (the server already ships a web UI)
- ❌ Multi-account switching (single account for v1)

---

## 5. 三个客户端 / The three clients

| 端 / Platform | 语言 / Language | 建议技术栈 / Proposed stack | 产物 / Artifact |
|---|---|---|---|
| Windows | Go | `gotd/td` + 本地 Web UI 外壳（WebView） | `HaoDrive.exe`（单文件） |
| Linux | Go | 同上 / same | `haodrive`（单二进制，静态链接） |
| Android | Kotlin | Compose + TDLib（JNI） | `HaoDrive-x.y.z.apk` |

**为什么 Go 用本地 Web UI 外壳**：上传/下载/预览都需要列表、进度条、视频播放，HTML 这套最成熟；Go 只做本地 HTTP 服务和 Telegram 通信，窗口用 WebView 包一层。备选是 Fyne（纯 Go），但视频播放和样式都弱一截。

**Why a local web UI shell for Go**: lists, progress bars and video playback are all mature in HTML; Go serves a local HTTP API and speaks to Telegram, while a WebView wraps it as a window. Fyne (pure Go) is the alternative, but its video playback and styling are weaker.

---

## 6. 数据模型 / Data model（与服务端一致 / same as the server）

- 文件夹 = Telegram 私有频道
- 单个文件 = 一条消息
- 大文件 = 连续的 N 条消息，每条一条分片

- A folder is a private Telegram channel
- A small file is one message
- A large file is N consecutive messages, one part each

---

## 7. 映射规则 / Mapping rules

**这一节是客户端与服务端的共同契约，改动必须两边同时改。**服务端自己还额外存了随机的 `group_key`，所以不依赖下文的 run 拆分规则；客户端只靠 caption，必须遵守。

**This section is the shared contract between clients and the server; any change must land on both sides.**

### 7.1 单分片 / Single part

一条消息就是完整文件，用原始文件名，不加 caption。

One message is the whole file, under its original name, with no caption.

### 7.2 多分片 / Multiple parts

| 项 / Item | 规则 / Rule |
|---|---|
| 分片文件名 | `原名.part001`、`原名.part002`……（三位数，从 001 开始） |
| 分片 caption | `[TD-PART i/N]`，i 从 1 开始 |
| 组键 / group key | 第 1 片的消息 id（`first_message_id`） |
| 排序 / order | 按 caption 里的 i |
| 逻辑大小 / logical size | 所有分片大小之和 |
| 分片大小 / part size | 默认 1.9GB，必须 < 2GB（Telegram 单条上限；Premium 账号 4GB） |

- Part file name: `name.part001`, `name.part002`, … (three digits, starting at 001)
- Part caption: `[TD-PART i/N]`, i starts at 1
- Group key: the first part's message id (`first_message_id`)
- Order: by the i in the caption
- Logical size: sum of all part sizes
- Part size: 1.9GB by default, must stay under 2GB (Telegram's per-message cap; 4GB for Premium)

### 7.3 本地重建算法 / Local reconstruction

```
1. 拉取频道消息（按 id 升序）
2. 对每条：解析 caption 是否匹配 ^\[TD-PART (\d+)/(\d+)\]$
   ├─ 不匹配 → 当作单文件（文件名取消息里的文档名）
   └─ 匹配   → 进入分片池：键 = (去掉 .partNNN 的文件名, N)
              同一键按消息 id 顺序累积；遇到已经出现过的 i，
              就开一个新的 run（同名同片数的两次上传靠这个分开）
3. 池里每个键：按 i 排序，检查 i 是否 1..N 连续
   ├─ 连续 → 合成一个逻辑文件，组键 = 第 1 片 message id
   └─ 不连续 → 标记为「未完成的上传」，医生界面里允许删除或续传
4. 单文件与逻辑文件合并成一个列表，按消息时间排序
```

```
1. Fetch channel messages in ascending id order
2. For each message, test the caption against ^\[TD-PART (\d+)/(\d+)\]$
   ├─ no match → a single file (name from the document attribute)
   └─ match    → add to the parts pool, keyed by (base name without .partNNN, N)
                 messages accumulate in id order; a repeated i starts a new run
                 (this is what separates two uploads of the same name)
3. Per key: sort by i and check that i is a contiguous 1..N
   ├─ contiguous   → one logical file, group key = first part's message id
   └─ not contiguous → an incomplete upload; allow delete or resume
4. Merge singles and logical files into one list ordered by message time
```

### 7.4 边界情况 / Edge cases

- **上传中断**：缺片不合成，显示为「未完成」；补齐后自动变成正常文件
- **同名文件**：同名同 N 的消息按 id 顺序分成连续的 run，重复的分片序号开始新 run；两次上传完全交错时 caption 信息不足以可靠拆分，会显示为未完成
- **文件名本身以 `.partNNN` 结尾**：只看 caption，不看文件名，避免误判
- **历史消息没有 caption**（v10 之前的旧文件）：当作单文件处理
- **重命名**：Telegram 消息不能改名，只能落在本地；跨设备一致要靠第 8 节的 `map.txt`

- **Interrupted upload**: incomplete parts are not merged and show as "incomplete"; they become a normal file once the missing parts arrive
- **Duplicate names**: messages with the same name and N split into consecutive runs in id order, and a repeated part index starts a new run. If two uploads interleave completely the caption holds too little information to split them reliably, and they show as incomplete
- **Names that literally end in `.partNNN`**: rely on the caption, never the file name
- **Legacy messages without captions** (files from before v10): treated as single files
- **Rename**: Telegram messages cannot be renamed; renames stay local unless the `map.txt` overlay (section 8) is used

---

## 8. 可选的 map 覆盖层 / Optional map overlay

**不是必须的。** 没有它，第 7 节的规则已经能重建全部文件。它只解决 caption 表达不了的东西：重命名、自定义排序。

**Optional.** Without it, section 7 already reconstructs everything. It only carries what captions cannot: renames and custom ordering.

### 8.1 存放位置 / Where it lives

每个文件夹频道里放一份置顶文档 `_hdmap.txt`；客户端读到就用，读不到就纯按 caption 重建。

One pinned document named `_hdmap.txt` per folder channel. If present, clients apply it; if absent, they fall back to pure caption reconstruction.

### 8.2 格式 / Format

```
rev: 42
updated: 2026-11-09T19:40:00Z
rename -1001234567890 1024 电影-final.mkv
order  -1001234567890 1024 3
```

- 每行是「指令 频道 组键 值」，`#` 开头是注释
- `rev` 单调递增，写入前 `rev + 1`
- 冲突：两台设备同时改，`rev` 大的赢；rev 相同比 `updated`；仍相同则保留内容 hash 较大的那份
- 客户端每次启动读一次；上传/重命名后可选地写回

- Each line is `command channel group_key value`; lines starting with `#` are comments
- `rev` is monotonic; writers increment it before saving
- Conflicts: higher `rev` wins; tie → later `updated`; tie → larger content hash
- Clients read it at startup, and may write it back after a rename

### 8.3 与服务端重命名的关系 / Relation to server-side renames

服务端的重命名存在它自己的 SQLite（`file_names` 表）里，**不写 `map.txt`**。所以：

- 只用客户端：rename 走 `map.txt`，多端一致
- 服务端改过名的文件，客户端看不到新名字（**已知差异**）
- 想让两边统一，需要服务端将来也读/写 `map.txt`（本计划暂不做）

The server stores renames in its own SQLite (`file_names`) and **does not write `map.txt`**. Therefore:

- Clients only: renames go through `map.txt` and stay consistent
- Files renamed on the server keep their old name in clients (**known gap**)
- Making both agree requires the server to read/write `map.txt` later (out of scope here)

---

## 9. 登录与会话 / Login and sessions

- 每台设备各自登录（api_id / api_hash 由用户在 my.telegram.org 自建）
- 手机号 + 验证码（可能发在 Telegram 客户端里）+ 两步验证密码
- 会话凭据加密保存在本地；卸载/退出即清除
- 多设备同时在线是 Telegram 原生能力，但会话太多会被踢，文档里写清楚

- Each device logs in on its own (the user creates api_id / api_hash at my.telegram.org)
- Phone number + code (often delivered inside Telegram) + two-step password
- Session credentials are stored encrypted on the device and cleared on logout
- Multiple simultaneous sessions are a native Telegram feature, but too many will get evicted; document it

---

## 10. 功能范围 / Feature scope

| 功能 / Feature | Windows | Linux | Android |
|---|:--:|:--:|:--:|
| 浏览文件夹与文件 / browse | ✅ | ✅ | ✅ |
| 上传（自动分片）/ upload with slicing | ✅ | ✅ | ✅ |
| 下载（断点续传）/ resumable download | ✅ | ✅ | ✅ |
| 图片/视频/音频/PDF 预览 / preview | ✅ | ✅ | ✅ |
| 重命名 / rename | ✅ | ✅ | ✅ |
| 删除 / delete | ✅ | ✅ | ✅ |
| 新建文件夹（建频道）/ create folder | ✅ | ✅ | ✅ |
| 搜索 / search | ✅ | ✅ | ✅ |
| 上传下载进度与速度 / progress + speed | ✅ | ✅ | ✅ |
| 拖拽上传 / drag and drop | ✅ | ✅ | — |
| 后台继续传输 / background transfer | — | — | ✅（前台服务） |

---

## 11. 里程碑 / Milestones

| 阶段 | 内容 | 验收 |
|---|---|---|
| M0 | 把第 7、8 节写成可执行的规范 + 测试样本（一个测试频道里的固定消息） | 规范评审通过 |
| M1 | Go 核心库：登录、列文件、上传切片、下载拼装、本地 map | CLI 能完成一次 5GB 文件的上传和回读，逐字节一致 |
| M2 | Windows GUI（本地 Web UI + WebView 外壳） | 双击 exe 完成登录与上传 |
| M3 | Linux 二进制（同一套代码，交叉编译 + 打包说明） | 无依赖单文件运行 |
| M4 | Android（Compose + TDLib） | 在真机上与 M1/M2 传的文件互认 |
| M5 | `map.txt` 覆盖层 + 跨端一致性 | 三端看到的重命名/排序一致 |

| Stage | Scope | Acceptance |
|---|---|---|
| M0 | Turn sections 7–8 into an executable spec plus test fixtures (fixed messages in a test channel) | Spec reviewed |
| M1 | Go core: login, list, upload with slicing, download reassembly, local map | CLI uploads a 5GB file and reads it back byte-for-byte |
| M2 | Windows GUI (local web UI + WebView shell) | Double-click the exe, log in, upload |
| M3 | Linux binary (same code, cross-compile + packaging notes) | Single file, no dependencies |
| M4 | Android (Compose + TDLib) | Talks to files uploaded by M1/M2 on a real device |
| M5 | `map.txt` overlay + cross-client consistency | All three clients agree on renames and ordering |

---

## 12. 一致性验证 / Consistency tests

- 同一个测试频道，服务端与客户端各传一个多分片文件，双方都能看到并正确下载
- 三端对同一批消息重建出的文件列表完全一致（顺序、名字、大小、分片数）
- 中断上传：只传 2/5 片，三端都显示「未完成」；补完第 3 片后自动变成正常文件
- `map.txt` 缺失、损坏、rev 回退：客户端都能退回 caption 重建

- In one test channel, the server and a client each upload a multi-part file; both see and download it correctly
- All three clients reconstruct identical file lists (order, name, size, part count)
- Interrupted upload: with 2 of 5 parts, every client shows "incomplete"; finishing part 3 turns it into a normal file
- Missing, corrupt, or rolled-back `map.txt`: clients always fall back to caption reconstruction

---

## 13. 风险 / Risks

| 风险 | 说明 | 对策 |
|---|---|---|
| 账号封禁 | 第三方客户端 + 大量上传，违反 Telegram 条款的风险真实存在 | 文档写明；出错时停止重试，不刷接口 |
| 多设备会话 | 每台设备一个会话，过多会被踢 | 提供「退出所有设备」入口；文档说明 |
| api_id 滥用限制 | 公开的 api_id 会被限制 | 引导用户自建；不内置公共 api_id |
| 文件上限 | 普通账号单文件 2GB，Premium 4GB | 分片大小可配；超限时明确提示 |
| 本地索引丢失 | 卸载即丢 | caption 重建保证不丢数据；map.txt 可选备份 |
| 映射分歧 | 服务端改名客户端看不到 | 已列为已知差异；未来让服务端也读写 map.txt |

| Risk | Notes | Mitigation |
|---|---|---|
| Account ban | Third-party clients + heavy uploads genuinely risk ToS action | Documented; stop on errors instead of retrying hard |
| Multiple sessions | One per device; too many get evicted | Provide "log out everywhere"; document it |
| api_id abuse limits | Public api_ids get restricted | Guide users to create their own; never ship a shared one |
| File caps | 2GB per file (4GB Premium) | Configurable part size; clear errors |
| Local index loss | Uninstall wipes it | Caption reconstruction keeps the data; map.txt optional backup |
| Mapping drift | Server renames invisible to clients | Listed as a known gap; later let the server read/write map.txt |

---

## 14. 与服务端的关系 / Relationship to the server

- 无运行时依赖，可分别安装、分别升级
- 共用同一套频道约定（第 6、7 节）
- 服务端独有能力保持不变：Web UI、分享链接、WebDAV、常驻
- 本仓库同时包含服务端与三个客户端，但构建/发布各自独立

- No runtime dependency; install and upgrade independently
- Share the channel conventions in sections 6–7
- Server-only capabilities stay: web UI, share links, WebDAV, always-on
- This repository holds both the server and the three clients, but they build and ship independently

---

## 15. 未决问题 / Open questions

1. 桌面端 GUI：WebView 外壳 vs Fyne —— 倾向 WebView
2. Android 传输层：TDLib（+~30MB）vs 纯 Kotlin MTProto —— 倾向 TDLib
3. `map.txt` 的写入时机：仅重命名时 vs 每次上传后都刷新
4. 第一版是否要支持「续传未完成的分片」（M1 先做不做）
5. 服务端将来是否也读写 `map.txt`（让四端完全一致）

1. Desktop GUI: WebView shell vs Fyne — leaning WebView
2. Android transport: TDLib (+~30MB) vs pure Kotlin MTProto — leaning TDLib
3. When to write `map.txt`: only on rename, or after every upload
4. Whether v1 supports resuming an incomplete part set (M1 scope)
5. Whether the server should read/write `map.txt` later (for full four-way consistency)
