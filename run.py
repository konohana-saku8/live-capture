# -*- coding: utf-8 -*-
"""Live danmaku / Super Chat capture service.

    python run.py                          # 0.0.0.0:8765，打开网页再配置
    python run.py --host 127.0.0.1 --port 9000
    python run.py --room 22748536 --auto-start

Termux 上推荐用 `bash start.sh`，它会自动检查依赖。
配置、数据库、日志都落在 data/ 下，网页里改的配置会写回 data/config.json。
"""
from __future__ import annotations

import argparse
import os

from capture import CaptureManager
from server import Config, LogBuffer, serve
from store import Store

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(
        description="B 站直播弹幕 / SC 采集服务（网页配置 + REST API）")
    ap.add_argument("--data", default=os.path.join(HERE, "data"),
                    help="数据目录，默认 ./data")
    ap.add_argument("--host", default=None, help="监听地址，默认 0.0.0.0")
    ap.add_argument("--port", type=int, default=None, help="端口，默认 8765")
    ap.add_argument("--room", type=int, default=None, help="直播间号（短号也可）")
    ap.add_argument("--token", default=None, help="API 访问口令，空字符串表示清除")
    ap.add_argument("--auto-start", action="store_true",
                    help="服务起来后立刻开始采集")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data)
    os.makedirs(data_dir, exist_ok=True)
    config = Config(os.path.join(data_dir, "config.json"))
    overrides = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.room is not None:
        overrides["room_id"] = args.room
    if args.token is not None:
        overrides["token"] = args.token
    if overrides:
        config.save(overrides)

    current = config.get()
    logs = LogBuffer()

    def log(level, message):
        logs.add(level, message)

    store = Store(os.path.join(data_dir, "capture.db"))
    manager = CaptureManager(store, config.get, log)
    serve(current["host"], int(current["port"]), data_dir,
          os.path.join(HERE, "web"), store, manager, config, logs,
          auto_start=bool(args.auto_start or current.get("auto_start")))


if __name__ == "__main__":
    main()
