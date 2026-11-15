#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot 开机自启 —— 拉起 live-capture 采集服务。
# 安装位置：~/.termux/boot/20-capture.sh（需 chmod +x）
export PATH=/data/data/com.termux/files/usr/bin:$PATH
export HOME=/data/data/com.termux/files/home

termux-wake-lock >/dev/null 2>&1

# 服务要联网拉弹幕，等网络就绪（最多 60 秒）
i=0
while [ $i -lt 30 ]; do
  if ip route 2>/dev/null | grep -q default; then break; fi
  sleep 2
  i=$((i + 1))
done

DST="$HOME/live-capture"
[ -d "$DST" ] || exit 0
cd "$DST" || exit 0
mkdir -p data

# pid 文件里的进程还活着就不要再起一个
if [ -f data/service.pid ]; then
  OLD="$(cat data/service.pid 2>/dev/null)"
  if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
    exit 0
  fi
fi

nohup python3 run.py >> data/service.log 2>&1 &
echo $! > data/service.pid
