# -*- coding: utf-8 -*-
"""Live danmaku / Super Chat capture service.

    python run.py                          # 0.0.0.0:8765，打开网页再配置
    python run.py --host 127.0.0.1 --port 9000
    python run.py --room 22748536 --auto-start

Termux 上推荐用 `bash start.sh`，它会用 `.venv` 并检查依赖。
直播间列表、配置、数据库、日志都落在 data/ 下。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from app import bootstrap, create_app, shutdown          # noqa: E402
from app.settings import Config, NoiseFilter            # noqa: E402

BANNER = "=" * 52


def _web_dir() -> str:
    """优先用 Vite 构建产物；没构建过就退回旧版单文件页面。"""
    dist = os.path.join(HERE, "web", "dist")
    return dist if os.path.isdir(dist) else os.path.join(HERE, "web")


def _serve(app, host: str, port: int) -> None:
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        print("  提示：没装 waitress，改用 Werkzeug 内置服务器", flush=True)
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
        return
    waitress_serve(app, host=host, port=port, threads=16, channel_timeout=120)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="B 站直播弹幕 / SC 采集服务（网页配置 + REST API）")
    ap.add_argument("--data", default=os.path.join(HERE, "data"),
                    help="数据目录，默认 ./data")
    ap.add_argument("--host", default=None, help="监听地址，默认读配置或 0.0.0.0")
    ap.add_argument("--port", type=int, default=None, help="端口，默认读配置或 8765")
    ap.add_argument("--room", type=int, default=None,
                    help="启动时把该直播间号加入列表（配合 --auto-start 立刻采集）")
    ap.add_argument("--token", default=None, help="API 访问口令，空字符串表示清除")
    ap.add_argument("--auto-start", action="store_true",
                    help="服务起来后立刻采集列表里所有直播间")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s:%(lineno)d %(message)s",
        datefmt="%H:%M:%S")

    data_dir = os.path.abspath(args.data)
    os.makedirs(data_dir, exist_ok=True)

    # 应用自己也写一份日志文件（按大小轮转），不依赖 shell 的重定向；
    # 页面 /api/logs/file 可以直接下载这份完整日志。
    log_dir = os.path.join(data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    try:
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "service.log"),
            maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s:%(lineno)d %(message)s",
            "%H:%M:%S"))
        # 播放器每次拖动进度条都会断一次连接，这行会刷满整个文件
        file_handler.addFilter(NoiseFilter())
        logging.getLogger().addHandler(file_handler)
    except OSError as exc:
        print("  日志文件写入失败：%s" % exc, flush=True)

    # 先读一次配置拿到监听地址，命令行参数优先并回写
    probe = Config(os.path.join(data_dir, "config.json"))
    current = probe.get()
    overrides = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.token is not None:
        overrides["token"] = args.token
    if overrides:
        current = probe.save(overrides)

    host = str(current.get("host") or "0.0.0.0")
    port = int(current.get("port") or 8765)

    app = create_app(data_dir=data_dir, web_dir=_web_dir(), host=host, port=port)
    r = app.extensions["live"]

    # sqlite-web 数据管理页挂到 /sqlite，共享本端口；不可用时自动降级。
    # 注意：包装发生在 serve 时，保留 app 为 Flask 对象供 bootstrap/shutdown 用。
    from app.sqlite_admin import mount_sqlite_admin

    if args.room:
        from capture import fetch_room_brief
        try:
            brief = fetch_room_brief(args.room, timeout=3)
        except Exception:
            brief = {}
        r.rooms.add(args.room, "", brief)

    auto = bool(args.auto_start or current.get("auto_start"))
    bootstrap(app, auto_start=auto)
    r.logs.add("info", "服务启动：本机 http://127.0.0.1:%s/ · 局域网 http://%s:%s/ · "
                       "数据 %s · 页面 %s · 采集器 %s 个" %
               (port, r.lan_ip, port, os.path.abspath(data_dir),
                r.web_dir, len(r.rooms.list().get("items") or [])))

    print(BANNER, flush=True)
    print("  直播弹幕 / SC 采集服务已启动（Flask）", flush=True)
    print("  本机：http://127.0.0.1:%s/" % port, flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("  局域网：http://%s:%s/" % (r.lan_ip, port), flush=True)
    print("  数据：%s" % os.path.abspath(data_dir), flush=True)
    print("  页面：%s" % r.web_dir, flush=True)
    print("  直播间：%s 个" % len(r.rooms.list().get("items") or []), flush=True)
    print(BANNER, flush=True)

    try:
        _serve(mount_sqlite_admin(app, data_dir, str(current.get("token") or "")),
               host, port)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown(app)


if __name__ == "__main__":
    main()
