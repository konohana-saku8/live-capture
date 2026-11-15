#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot 开机自启 —— 拉起 sshd。
# 安装位置：~/.termux/boot/10-sshd.sh（需 chmod +x）
export PATH=/data/data/com.termux/files/usr/bin:$PATH
export HOME=/data/data/com.termux/files/home

termux-wake-lock >/dev/null 2>&1

# 等网络就绪（最多 60 秒），sshd 本身不依赖网络，但避免局域网还没上来就连不上
i=0
while [ $i -lt 30 ]; do
  if ip route 2>/dev/null | grep -q default; then break; fi
  sleep 2
  i=$((i + 1))
done

# 已经在跑就不要重复起
if ! ps -A 2>/dev/null | grep -q '[s]shd'; then
  sshd
fi
