# 部署方式

HaoDrive 只有一个进程：读 `.env`、监听 `127.0.0.1:8000`、把数据写在 `./data`。
所以怎么部署都行，差别只在于「用什么把它挂到公网上」和「怎么让它开机自启」。

| 方式 | 适合谁 | 难度 | 说明 |
|---|---|---|---|
| **A. 裸机 systemd + Caddy** | 有 VPS、有域名 | ★ | 推荐。线上就是这套，内存占用最小 |
| **B. 裸机 systemd + nginx** | 已经装了 nginx / 熟 nginx | ★★ | 配置见 `deploy/nginx/haodrive.conf` |
| **C. 无公网 IP + 隧道** | 家里宽带、NAS、公司内网 | ★★ | Cloudflare Tunnel / frp / tailscale funnel |
| **D. Windows 本机** | 只想先跑起来看看 | ★ | `deploy/windows/run-haodrive.ps1` |
| **E. Docker Compose** | 不想装 Python | ★ | 见 README 的 Docker 章节；常驻内存比裸机高 |

---

## 通用前提

1. **Python 3.10+**（裸机方式）。Debian/Ubuntu 上还需要 `python3-venv`。
2. **能连 Telegram**。国内机器通常连不上，需要在 `.env` 里填 `PROXY`，
   例如 `PROXY=socks5://127.0.0.1:1080`。
3. **反向代理必须关掉请求体缓冲**。v11.1 起的流式上传是「浏览器发一块、
   服务端立刻转给 Telegram」，如果代理先把整个请求体收完再转发，这个特性就
   等于没开，速度会退回旧的串行路径。三个关键项：

   ```nginx
   proxy_request_buffering off;   # 上传
   proxy_buffering off;           # 下载/播放
   proxy_read_timeout 3600s;      # 大文件
   ```

   Caddy 默认就是流式转发，不用额外配置（`flush_interval -1` 已写在示例里）。
4. **站点只在一个随机路径下响应**（`.env` 的 `SECRET_PATH`），这个前缀是
   反代负责剥掉的，应用本身不检查。所以如果把应用端口直接暴露到公网，
   这层保护就没有了——**一定要让流量经过反代**。

---

## A. 裸机 systemd + Caddy

### 1. 装 Caddy

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

### 2. 跑安装脚本

把仓库传到服务器（例如 `/root/haodrive`），然后：

```bash
cd /root/haodrive
sudo bash deploy/install.sh
```

脚本会做这些事：

- 创建系统用户 `haodrive`
- 把 `app/`、`requirements.txt` 拷到 `/opt/haodrive`
- 创建 venv、装依赖
- 从 `.env.example` 生成 `.env`，**自动填随机的 `SECRET_PATH` / `ADMIN_PASSWORD` / `APP_SECRET`**，
  并把管理员密码打印一次（只打印一次，记下来）
- 安装并启动 `haodrive.service`（只监听 `127.0.0.1:8000`）

可选参数：

```bash
sudo bash deploy/install.sh --prefix /srv/haodrive --port 8010 --user drive
sudo bash deploy/install.sh --dry-run          # 只看会干什么
sudo bash deploy/install.sh --no-service       # 只装文件（测试/自定义 service 用）
```

> 前缀别放在 `/home` 下面：服务单元开了 `ProtectHome=true`。

### 3. 改配置

```bash
sudo nano /opt/haodrive/.env
```

至少改：

```env
DOMAIN=drive.example.com
```

国内机器再加 `PROXY=socks5://127.0.0.1:1080`。改完 `systemctl restart haodrive`。

### 4. 配 Caddy

把仓库里的 `Caddyfile` 拷到 `/etc/caddy/Caddyfile`，改两处或用环境变量：

```bash
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo mkdir -p /etc/caddy
sudo tee /etc/caddy/caddy.env >/dev/null <<'EOF'
DOMAIN=drive.example.com
SECRET_PATH=你的密钥路径
UPSTREAM=127.0.0.1:8000
EOF
sudo systemctl edit caddy    # 在 [Service] 下加一行 EnvironmentFile=/etc/caddy/caddy.env
sudo systemctl restart caddy
```

Caddy 会自动申请 HTTPS 证书（域名要能解析到这台机器，80 端口要通）。
验证配置：`caddy validate --config /etc/caddy/Caddyfile`。

`Caddyfile` 里的 `{$UPSTREAM:app:8000}` 默认值是 Docker 用的服务名，
裸机部署把它设成 `127.0.0.1:8000` 即可。

### 5. 打开

```
https://drive.example.com/<SECRET_PATH>/
```

---

## B. 裸机 + nginx

安装步骤和 A 相同（`install.sh` 照跑），只把反代换成 nginx：

```bash
sudo cp deploy/nginx/haodrive.conf /etc/nginx/conf.d/haodrive.conf
sudo nano /etc/nginx/conf.d/haodrive.conf   # 替换 <DOMAIN> 和 <SECRET_PATH>
sudo nginx -t && sudo systemctl reload nginx
```

证书可以用 certbot：

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d drive.example.com
```

> 这份配置是按 nginx 1.20+ 写的。`http2 on;` 需要 1.25.1+，
> 更老的版本按文件里的注释改成 `listen 443 ssl http2;`。

---

## C. 没有公网 IP：隧道

适用：家里宽带、NAS、公司内网、懒得开端口。

思路是「本机照常跑，隧道把公网请求转进来」。**别把 8000 直接怼给隧道**，
否则随机路径那层保护就没了；先让本机 Caddy 剥前缀，再交给隧道。

```bash
# 1. 应用还是本机 8000
.\deploy\windows\run-haodrive.ps1   # Windows
# 或 systemd 安装（方案 A 的 1~3 步）

# 2. 本机 Caddy 开一个明文端口，保留前缀行为
SECRET_PATH=$(grep '^SECRET_PATH=' .env | cut -d= -f2) \
UPSTREAM=127.0.0.1:8000 \
caddy run --config deploy/caddy/Caddyfile.tunnel

# 3. 隧道指向 8080
cloudflared tunnel --url http://127.0.0.1:8080
```

cloudflared 会给你一个 `https://xxxx.trycloudflare.com` 的临时域名；
正式用的话在 Cloudflare 后台建 named tunnel 绑自己的域名。

frp / tailscale funnel / ngrok 同理，只要能转到 `127.0.0.1:8080`。

---

## D. Windows 本机

```powershell
.\deploy\windows\run-haodrive.ps1              # 自动建 venv、装依赖、生成 .env
.\deploy\windows\run-haodrive.ps1 -Port 8010   # 换端口
.\deploy\windows\run-haodrive.ps1 -Reinstall   # 重建 venv
```

脚本只绑定 `127.0.0.1`，本机能访问，外网访问不了。要给外网用，按方案 C
在它前面挂 Caddy + 隧道。

> 首次运行会生成 `.env`（随机密码和密钥路径），把打印出来的管理员密码存好。

---

## E. Docker Compose

见 README 的「部署（Docker，五分钟）」。三个服务（app / caddy / 数据卷），
更新代码后 `docker compose up -d --build`。

Docker 路线常驻内存比裸机高（Python 镜像 + 容器开销），资源紧张时用方案 A。

---

## 更新代码

```bash
cd /root/haodrive && git pull          # 或重新上传
sudo bash deploy/install.sh             # 幂等：保留 .env 和 data，只更新代码和依赖
systemctl status haodrive
```

Docker：`docker compose up -d --build`。

## 备份

要备的就三样，全在数据目录里：

| 文件 | 内容 |
|---|---|
| `data/haodrive.db` | 设置、文件夹顺序、分享链接、上传分组、流量统计、**Telegram 会话（加密）** |
| `data/secret.key` | 加密密钥与会话签名密钥 |
| `data/cache/` | 缩略图和媒体块缓存，**可以不要** |

`data/tmp/` 是上传中转，不用备。

## 排查

```bash
systemctl status haodrive
journalctl -u haodrive -f
curl -s http://127.0.0.1:8000/healthz        # 应返回 {"ok":"1"}
caddy validate --config /etc/caddy/Caddyfile
```

常见问题：

| 现象 | 原因 |
|---|---|
| 根路径 404 | 设计如此：只有 `/<SECRET_PATH>/` 下响应 |
| 上传速度回到 1MB/s | 反代开了请求体缓冲，见「通用前提 3」 |
| 登录一直 429 | 登录限流，等五分钟；或者 IP 拿错了（反代要传 `X-Forwarded-For`） |
| 传文件卡在 0% | 服务器连不上 Telegram，检查 `.env` 的 `PROXY` |
| 服务起不来 | `journalctl -u haodrive -n 50`，多半是 `.env` 少 `ADMIN_PASSWORD` 或太短（<10 位） |
