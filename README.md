# 海鸥云盘 · seagulldrive

把 **Telegram 私有频道当硬盘**的自托管云盘。部署在你自己的 VPS 上，浏览器直接用，Caddy 自动签 HTTPS。

思路和 [Telegram-Drive](https://github.com/caamer20/Telegram-Drive) 一样：**一个文件夹 = 一个私有广播频道，一个文件 = 频道里的一条消息**。但它不是桌面客户端，而是一个 Web 服务，任何设备打开浏览器就能用。

---

## 功能

- 网页登录（单管理员密码），会话签名 Cookie
- 首次使用向导：api_id / api_hash / 手机号 / 验证码 / 两步验证
- 文件夹管理：新建 = 在 Telegram 建私有频道（标题带 `[TD]`），自动识别桌面版建的旧频道
- 文件列表、搜索、分页
- 拖拽 / 多选上传，浏览器→服务器、服务器→Telegram 两段进度
- 下载，支持 HTTP Range（断点续传、视频拖动进度条）
- 在线预览：图片、视频、音频、PDF、文本
- 删除、移动到其它文件夹（转发+删除原消息）
- 分享链接：可选有效期、可选访问密码，独立公开页面
- 响应式 UI，手机能用

## 架构

```
浏览器 ──HTTPS──> Caddy ──> FastAPI (uvicorn)
                              ├─ Telethon (MTProto, 你的账号)
                              ├─ SQLite  (设置/文件夹顺序/分享链接)
                              └─ Telegram 私有频道  ← 文件真正存在这里
```

- **文件不落在 VPS 磁盘上**（上传时只在 `/data/tmp` 中转过一下），VPS 只需要很小的盘。
- 你的 Telegram 账号就是存储账号，频道就是文件夹，手机上用 Telegram 也能直接看到这些文件。
- Telegram 单文件上限 **2GB**（非 Premium），超过会失败。

---

## 部署（Docker，五分钟）

### 1. 域名

在域名服务商加一条 **A 记录**，指向 VPS 公网 IP，例如 `drive.example.com`。

> 如果套了 Cloudflare，部署时先关掉橙色云朵（仅 DNS），等 Caddy 签完证书再开。

### 2. 装 Docker

```bash
curl -fsSL https://get.docker.com | sh
```

（国内机器慢的话用阿里云镜像源。）

### 3. 上传代码并配置

把本目录传到 VPS，例如 `/opt/seagulldrive`，然后：

```bash
cd /opt/seagulldrive
cp .env.example .env
nano .env
```

必改两项：

```env
DOMAIN=drive.example.com
ADMIN_PASSWORD=换成一个长密码
```

如果这台 VPS 连不上 Telegram（比如国内机器），加代理：

```env
PROXY=socks5://127.0.0.1:1080
```

### 4. 启动

```bash
docker compose up -d --build
```

第一次启动 Caddy 会去申请证书，等 10~30 秒：

```bash
docker compose logs -f caddy
```

看到 `certificate obtained successfully` 就成了。

### 5. 连接 Telegram

打开 `https://你的域名`：

1. 输入 `ADMIN_PASSWORD` 登录。
2. 按向导填 **api_id / api_hash**（去 [my.telegram.org](https://my.telegram.org) → API development tools 自己建一个），
   再填手机号（带国家码，如 `+8613800000000`）。
3. 收验证码（可能发在 Telegram 客户端里而不是短信），提交。
4. 有两步验证就再输一次密码。
5. 进主界面后点「新建文件夹」——它会在 Telegram 里建一个私有频道，然后就能传文件了。

---

## 常用操作

```bash
docker compose logs -f app      # 看后端日志
docker compose restart app      # 重启
docker compose down             # 停止
docker compose up -d --build    # 更新代码后重建
```

数据都在 `./data`：

| 文件 | 内容 |
|---|---|
| `data/seagulldrive.db` | 设置、文件夹顺序、分享链接；**Telegram 会话也加密存在这里** |
| `data/secret.key` | 加密密钥 + 会话签名密钥 |
| `data/tmp/` | 上传中转，会自动清理 |
| `data/cache/` | 缩略图缓存 |

**备份就备份整个 `data/` 目录**。丢了 `secret.key`，数据库里的会话和密钥就解不开了，需要重新登录 Telegram。

---

## 常见问题

**打开页面 502 / 证书没签下来**
- 域名 A 记录没生效，或者 80/443 被别的服务占了（Nginx/Apache 先停掉）。
- Cloudflare 开了代理模式，先关。

**登录时报 `API_ID_PUBLISHED_FLOOD`**
- 你用的是网上抄来的 api_id。去 my.telegram.org 新建一个自己的应用。

**`FLOOD_WAIT`**
- Telegram 限流，等提示的时间再操作。上传是串行的，别一次塞几百个文件。

**上传卡在 0% / 一直“上传到 Telegram…”**
- VPS 到 Telegram 不通。配 `PROXY`，重启后再试。
- 看 `docker compose logs -f app` 里的报错。

**文件超过 2GB**
- Telegram 硬限制。桌面版客户端也一样。

**想用桌面版 Telegram-Drive 建的旧文件夹**
- 会自动识别（标题里的 `[TD]` 或频道简介里的 `[telegram-drive-folder]` 标记）。
  找到的旧频道会写进本地数据库，之后即使标记丢了也不会从列表里消失。

---

## 本地试跑（不连 Telegram）

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
DEMO_MODE=1 ADMIN_PASSWORD=demo uvicorn app.main:app --port 8010
```

打开 http://127.0.0.1:8010 ，密码 `demo`。演示模式用 `data/demo/` 目录假装是云盘，适合先看界面。

---

## 安全说明

- 只暴露 8000 给 Caddy，应用本身不对公网开端口。
- 登录密码用 scrypt 哈希比对，登录限流 8 次 / 5 分钟。
- 会话 Cookie：HttpOnly + SameSite=Lax，HTTPS 下自动 Secure。
- 所有写接口要求自定义请求头（跨站表单伪造不了）。
- 分享链接 token 128 位随机，可选密码 + 过期时间。
- **注意**：这台服务器一旦被拿下，等于你的 Telegram 账号会话被拿下。VPS 安全自己上点心（SSH 密钥登录、防火墙、及时更新）。

## 目录结构

```
app/
  main.py          FastAPI 路由
  tg_backend.py    Telegram（Telethon）后端
  demo_backend.py  本地目录模拟后端
  db.py            SQLite
  security.py      密码/会话/限流
  config.py        环境变量
  static/          前端（原生 JS，无构建）
Dockerfile
docker-compose.yml
Caddyfile
.env.example
```

---

## WebDAV 挂载（v6）

页面左下角「WebDAV 挂载」里能看到地址、用户名和密码（密码自动生成，可一键更换）。

- 地址：`https://你的域名/<随机路径>/dav/`（端口跟站点一致；本文档的 VPS 部署用 24443，Docker 部署默认 443）
- 支持 `PROPFIND / GET / HEAD / PUT / DELETE / MKCOL / MOVE / COPY / LOCK / UNLOCK`
- `rclone`、Cyberduck、RaiDrive、macOS Finder 都能挂；Windows 资源管理器只认 443 端口，非标准端口挂不上，用 rclone 替代：

```bash
rclone config create seagull webdav url https://你的域名:24443/<随机路径>/dav/ vendor other user seagulldrive pass <密码>
rclone ls seagull:
```

## 断点续传（分片上传）

网页端按 8MB 分片上传，服务端按 offset 校验并落库：

- 浏览器刷新/断网后重新选同一个文件，会从 `localStorage` 里找回上传会话继续传
- 每片失败自动重试 3 次；整段失败会保留会话，界面上有「重试」和「取消」
- 上传会话存在 SQLite，服务重启也不会丢

## 视频播放

- 服务端 4MB 分块缓存（`MEDIA_CACHE_GB`，默认 4GB），命中后直接本地读盘
- 播放时自动预取后面 3 块，拖动进度条不会每次都回 Telegram 拉
- 支持 HTTP Range、`If-Range`、`If-None-Match`，不可满足的 Range 返回 416

## 速度与卡顿

- **下载慢**：浏览器到 VPS 是一条 TCP 连接，跨国链路 RTT 一高，默认 cubic 拥塞控制就把单连接吞吐压在 2MB/s 上下（同一台机器开 4 条连接能跑到 6.6MB/s，说明不是 VPS 出口的问题）。在 VPS 上用 root 跑一次：

  ```bash
  bash tools/optimize-vps.sh
  ```

  它会开 BBR + fq 并放大 TCP 缓冲，单连接吞吐通常翻几倍。已建立的连接要断开重连才生效。

- **视频卡顿**：播放器是按 Range 一段段要数据的，以前每个块都要等 Telegram。现在打开视频/音频时，服务端会按 `PREBUFFER_MB`（默认 128MB）在后台提前把缓存填好，后续拖动、续播直接读本地盘；预取窗口也从 3 块加到 6 块。
- 媒体块缓存默认 4GB（`MEDIA_CACHE_GB`），命中情况看 `GET /api/status`。
- 注意：这些只减少等待和重复下载，突破不了 VPS 出口带宽的物理上限。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

覆盖：会话/分享令牌隔离、Range 解析、分片上传、Range/HEAD/416、分享密码、WebDAV 全流程、媒体缓存命中与淘汰。CI 配置在 `.github/workflows/ci.yml`。

## 运维

```bash
systemctl status seagulldrive caddy
journalctl -u seagulldrive -f
```

- `/healthz`：进程活着就 200
- `/readyz`：查数据库、磁盘剩余、Telegram 状态、缓存；磁盘 < 200MB 会返回 503
- `GET /api/status`（需登录）：缓存命中统计、磁盘、任务数
- 日志带请求方法/路径/状态/耗时；分享链接路径会脱敏成 `/s/***`

## 安全说明（v6）

- 分享解锁用的是独立 HMAC 令牌，访客拿到也**不能**当管理员会话用
- 退出登录会让所有已签发会话立即失效（会话版本号）
- 登录限流按真实客户端 IP（Caddy 的 X-Forwarded-For），失败越多延迟越长
- 分享页强制 CSP，非图片/视频/音频/PDF 的分享一律 `Content-Disposition: attachment`
- 上传分片有会话锁，避免并发写坏文件；临时文件 0600
- WebDAV 写操作会检查 `If` 锁令牌，冲突返回 423

## 许可证

MIT，见 [LICENSE](LICENSE)。
