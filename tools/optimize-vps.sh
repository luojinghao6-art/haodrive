#!/usr/bin/env bash
# 开 BBR + 放大 TCP 缓冲。跨境高 RTT 链路上，单连接吞吐主要卡在拥塞控制，
# 默认 cubic 在丢包时会把窗口砍半，浏览器一个连接下载/播放就会很慢。
# 用法（VPS 上，root）：bash tools/optimize-vps.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root"
    exit 1
fi

modprobe tcp_bbr 2>/dev/null || true
if ! sysctl -n net.ipv4.tcp_available_congestion_control | grep -qw bbr; then
    echo "内核不支持 BBR（需要 4.9+），跳过拥塞控制，只调缓冲。"
    CC_LINE=""
else
    CC_LINE="net.ipv4.tcp_congestion_control=bbr"
fi

cat > /etc/sysctl.d/99-seagulldrive-net.conf <<CONF
net.core.default_qdisc=fq
${CC_LINE}
net.core.rmem_max=134217728
net.core.wmem_max=134217728
net.ipv4.tcp_rmem=4096 87380 67108864
net.ipv4.tcp_wmem=4096 65536 67108864
net.ipv4.tcp_fastopen=3
CONF

sysctl --system >/dev/null
echo "拥塞控制: $(sysctl -n net.ipv4.tcp_congestion_control)"
echo "队列规则: $(sysctl -n net.core.default_qdisc)"
echo "完成。已建立的连接要断开重连才生效。"
