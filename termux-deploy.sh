#!/data/data/com.termux/files/usr/bin/bash
# 在 Termux 里部署并后台启动 live-capture 服务（供 adb 触发）。
# 进度与错误写到 /sdcard/live-capture/DEPLOY_STATUS.txt，方便从 adb 侧读取。
SRC=/sdcard/live-capture
DST="$HOME/live-capture"
ST="$SRC/DEPLOY_STATUS.txt"
say() { echo "[$(date +%H:%M:%S)] $*" >> "$ST"; }

: > "$ST" 2>/dev/null || { echo "无法写状态文件（/sdcard 不可写）" >&2; exit 1; }
say "begin user=$(id -un) home=$HOME"

if [ ! -r "$SRC/run.py" ]; then
  say "FATAL 读不到 /sdcard/live-capture，请先在 Termux 里执行 termux-setup-storage 并授权"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  say "python3 缺失，开始 pkg install python"
  pkg install -y python >> "$ST" 2>&1
  say "pkg rc=$?"
fi
PY="$(command -v python3)"
say "python=$PY"
if [ -z "$PY" ]; then say "FATAL 仍无 python3，请手动执行 pkg install python"; exit 1; fi

mkdir -p "$DST/data" "$DST/web" >> "$ST" 2>&1
cp "$SRC"/*.py "$SRC"/requirements.txt "$SRC"/start.sh "$DST"/ >> "$ST" 2>&1
cp "$SRC"/web/index.html "$DST"/web/ >> "$ST" 2>&1
cd "$DST" || { say "FATAL 无法进入 $DST"; exit 1; }

if ! "$PY" -c 'import websockets' >/dev/null 2>&1; then
  say "websockets 缺失，开始 pip install"
  "$PY" -m pip install -q -r requirements.txt >> "$ST" 2>&1
  say "pip rc=$?"
fi
"$PY" -c 'import websockets; print("websockets", websockets.__version__)' >> "$ST" 2>&1
say "deps rc=$?"

if [ -f "$DST/data/service.pid" ]; then
  OLD="$(cat "$DST/data/service.pid" 2>/dev/null)"
  if [ -n "$OLD" ]; then kill "$OLD" 2>/dev/null && say "killed old pid=$OLD"; fi
fi

termux-wake-lock 2>/dev/null || true
nohup "$PY" run.py >> "$DST/data/service.log" 2>&1 &
echo $! > "$DST/data/service.pid"
say "launched pid=$(cat "$DST/data/service.pid")"

sleep 3
"$PY" - >> "$ST" 2>&1 <<'PY'
import urllib.request
try:
    r = urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=4)
    print("HEALTH", r.status, r.read().decode("utf-8", "replace")[:200])
except Exception as exc:
    print("HEALTH FAIL", repr(exc))
PY
say "done"
