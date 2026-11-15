# live-capture 自愈：打开 Termux（或新开终端）时确保 sshd 与采集服务在跑。
# 幂等：进程活着就什么都不做。
if [ -z "$LC_AUTOSTART" ]; then
  export LC_AUTOSTART=1

  termux-wake-lock >/dev/null 2>&1

  if ! ps -A 2>/dev/null | grep -q '[s]shd'; then
    sshd >/dev/null 2>&1
  fi

  LC_PID=""
  [ -f "$HOME/live-capture/data/service.pid" ] && LC_PID="$(cat "$HOME/live-capture/data/service.pid" 2>/dev/null)"
  if [ -z "$LC_PID" ] || ! kill -0 "$LC_PID" 2>/dev/null; then
    if [ -d "$HOME/live-capture" ]; then
      (
        cd "$HOME/live-capture" || exit 0
        mkdir -p data
        nohup "$HOME/live-capture/.venv/bin/python" run.py >> data/service.log 2>&1 < /dev/null &
        echo $! > data/service.pid
      ) >/dev/null 2>&1
    fi
  fi
  unset LC_PID
fi
