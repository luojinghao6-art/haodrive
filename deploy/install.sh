#!/usr/bin/env bash
# 裸机安装 HaoDrive：系统用户 + venv + 依赖 + systemd 服务。
#
#   sudo ./deploy/install.sh --domain drive.example.com
#
# 可选参数：
#   --prefix DIR   安装目录（默认 /opt/haodrive）
#   --port N       监听端口（默认 8000，只绑定 127.0.0.1）
#   --user NAME    运行用户（默认 haodrive）
#   --source DIR   从哪个目录取代码（默认脚本所在仓库）
#   --no-service   只装文件，不创建/启动 systemd 服务（测试用）
#   --dry-run      只打印会执行什么，不动系统
#
# 反代（Caddy / nginx）另配，见 docs/DEPLOY.md。
set -euo pipefail

PREFIX=/opt/haodrive
PORT=8000
RUN_USER=haodrive
NO_SERVICE=0
DRY_RUN=0
SECRET_PATH=""
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="${2:?--prefix 需要值}"; shift 2 ;;
        --port) PORT="${2:?--port 需要值}"; shift 2 ;;
        --user) RUN_USER="${2:?--user 需要值}"; shift 2 ;;
        --source) SOURCE="${2:?--source 需要值}"; shift 2 ;;
        --no-service) NO_SERVICE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }
run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '  + %s\n' "$*"
    else
        "$@"
    fi
}
die() { printf '\n安装失败：%s\n' "$*" >&2; exit 1; }

random_string() {
    python3 - "$1" <<'PY'
import secrets, string, sys
alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(int(sys.argv[1]))))
PY
}

# ---- 前置检查 ------------------------------------------------------------
step "检查环境"
[ -f "$SOURCE/app/main.py" ] || die "在 $SOURCE 下没找到 app/main.py，用 --source 指定代码目录"
[ -f "$SOURCE/requirements.txt" ] || die "缺少 requirements.txt"
command -v python3 >/dev/null 2>&1 || die "没装 python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "需要 Python 3.10 或更新版本（当前 $(python3 -V 2>&1)）"
echo "  源码：$SOURCE"
echo "  目标：$PREFIX（端口 $PORT，用户 $RUN_USER）"

AS_ROOT=0
[ "$(id -u)" -eq 0 ] && AS_ROOT=1
if [ "$AS_ROOT" -eq 0 ] && [ "$NO_SERVICE" -eq 0 ]; then
    die "创建系统用户和安装服务需要 root，请用 sudo 运行（只想装文件可加 --no-service）"
fi
if [ "$AS_ROOT" -eq 0 ]; then
    RUN_USER="$(id -un)"
    echo "  非 root：只装文件，运行用户为 $RUN_USER"
fi

# ---- 用户与目录 ----------------------------------------------------------
if [ "$AS_ROOT" -eq 1 ]; then
    step "系统用户与目录"
    if id -u "$RUN_USER" >/dev/null 2>&1; then
        echo "  用户 $RUN_USER 已存在，复用"
    else
        run useradd --system --user-group --home-dir "$PREFIX" --shell /usr/sbin/nologin "$RUN_USER"
    fi
    run mkdir -p "$PREFIX"
fi
run mkdir -p "$PREFIX/data"

# ---- 代码 ----------------------------------------------------------------
step "安装代码"
run cp -a "$SOURCE/app" "$PREFIX/"
run cp -a "$SOURCE/requirements.txt" "$PREFIX/"
run mkdir -p "$PREFIX/deploy"
run cp -a "$SOURCE/deploy/systemd" "$PREFIX/deploy/"
if [ -f "$SOURCE/docs/DEPLOY.md" ]; then
    run mkdir -p "$PREFIX/docs"
    run cp -a "$SOURCE/docs/DEPLOY.md" "$PREFIX/docs/"
fi

# ---- 配置 ----------------------------------------------------------------
step "配置文件"
GENERATED=0
if [ -f "$PREFIX/.env" ]; then
    echo "  $PREFIX/.env 已存在，保留不动"
else
    [ -f "$SOURCE/.env.example" ] || die "缺少 .env.example，无法生成配置"
    run cp -a "$SOURCE/.env.example" "$PREFIX/.env"
    SECRET_PATH="$(random_string 24)"
    ADMIN_PASSWORD="$(random_string 32)"
    APP_SECRET="$(random_string 48)"
    if [ "$DRY_RUN" -eq 0 ]; then
        sed -i "s|^SECRET_PATH=.*|SECRET_PATH=$SECRET_PATH|" "$PREFIX/.env"
        sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$ADMIN_PASSWORD|" "$PREFIX/.env"
        sed -i "s|^APP_SECRET=.*|APP_SECRET=$APP_SECRET|" "$PREFIX/.env"
        chmod 600 "$PREFIX/.env"
    fi
    GENERATED=1
    echo "  已从 .env.example 生成 $PREFIX/.env（含随机密钥路径、随机管理员密码）"
fi

# Windows 上 checkout 或解压出来的 .env.example 是 CRLF，systemd 读
# EnvironmentFile 会把 CR 当成值的一部分，密码直接失效。
if [ "$DRY_RUN" -eq 0 ]; then
    sed -i 's/\r$//' "$PREFIX/.env"
fi

# ---- Python 环境 ---------------------------------------------------------
step "Python 虚拟环境"
if [ -x "$PREFIX/.venv/bin/python" ]; then
    echo "  $PREFIX/.venv 已存在，复用"
else
    run python3 -m venv "$PREFIX/.venv"         || die "创建 venv 失败，Debian/Ubuntu 上可能需要先装 python3-venv"
fi
run "$PREFIX/.venv/bin/python" -m pip install --quiet --upgrade pip
run "$PREFIX/.venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"

if [ "$AS_ROOT" -eq 1 ]; then
    run chown -R "$RUN_USER:$RUN_USER" "$PREFIX"
    # .env 里有密码，别让同机其他用户读
    run chmod 600 "$PREFIX/.env"
fi

# ---- systemd -------------------------------------------------------------
if [ "$NO_SERVICE" -eq 1 ]; then
    step "跳过 systemd（--no-service）"
else
    step "安装 systemd 服务"
    UNIT_SRC="$SOURCE/deploy/systemd/haodrive.service"
    [ -f "$UNIT_SRC" ] || die "缺少 $UNIT_SRC"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  + 写入 /etc/systemd/system/haodrive.service（替换 @PREFIX@ / @USER@ / @PORT@）"
        echo "  + systemctl daemon-reload && systemctl enable --now haodrive"
    else
        sed -e "s|@PREFIX@|$PREFIX|g" \
            -e "s|@USER@|$RUN_USER|g" \
            -e "s|@PORT@|$PORT|g" \
            "$UNIT_SRC" > /etc/systemd/system/haodrive.service
        systemctl daemon-reload
        systemctl enable haodrive >/dev/null
        systemctl restart haodrive
        sleep 2
        systemctl is-active haodrive >/dev/null \
            || die "服务没起来，看 journalctl -u haodrive -n 50"
        echo "  haodrive.service 已启动"
    fi
fi

# ---- 收尾 ----------------------------------------------------------------
step "完成"
echo "  安装目录：$PREFIX"
echo "  数据目录：$PREFIX/data（包含 Telegram 会话、设置、缓存）"
if [ "$GENERATED" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
    echo
    echo "  管理员密码：$ADMIN_PASSWORD   <- 只显示这一次，记到密码管理器里"
    echo "  站点密钥路径：/$SECRET_PATH"
fi
cat <<EOF

接下来：
  1. 编辑 $PREFIX/.env，把 DOMAIN 改成你的域名（国内机器上不了 Telegram 的话再加 PROXY）
  2. 重启服务：systemctl restart haodrive
  3. 配反向代理（二选一）：
       Caddy：把 Caddyfile 里的 UPSTREAM 指向 127.0.0.1:$PORT
       nginx：用 deploy/nginx/haodrive.conf 作模板
     详细步骤见 docs/DEPLOY.md
  4. 打开 https://你的域名/<密钥路径>/ 登录（路径在 .env 的 SECRET_PATH）

常用命令：
  systemctl status haodrive
  journalctl -u haodrive -f
EOF
