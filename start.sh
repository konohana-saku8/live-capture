#!/usr/bin/env bash
# Termux 一键启动：检查依赖后拉起服务。
# 用法：bash start.sh [--port 8765] [--host 0.0.0.0]
set -e
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "没有找到 Python，先在 Termux 里执行：pkg install python" >&2
  exit 1
fi

if ! "$PY" -c 'import websockets' >/dev/null 2>&1; then
  echo "缺少 websockets，正在安装…"
  "$PY" -m pip install -r requirements.txt
fi

exec "$PY" run.py "$@"
