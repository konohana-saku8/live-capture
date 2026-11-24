# -*- coding: utf-8 -*-
"""无界面入口：``python -m pctools.cli --scan`` / ``--run``。

图形界面跑不起来时（没装 PySide6、或想挂在计划任务里）用它。
"""
from __future__ import annotations

import argparse
import sys

from .core import api, config, job


def _fmt_row(part: job.Part) -> str:
    secs = int(part.duration_ms / 1000)
    size = "%.1f MB" % (part.bytes / 1048576.0)
    flag = {"skip": "跳过", "done": "完成", "failed": "失败",
            "pending": "待处理"}.get(part.status, part.status)
    return "  %-28s p%-2d %5d:%02d %6d kbps %9s  %-6s %s" % (
        (part.label or str(part.rec_id))[:28], part.idx + 1,
        secs // 60, secs % 60, part.kbps, size, flag, part.message)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pctools",
                                 description="在 PC 上重压录像并回传手机")
    ap.add_argument("--host", default="", help="服务地址，默认读配置")
    ap.add_argument("--token", default=None, help="访问口令")
    ap.add_argument("--scan", action="store_true", help="只列出分片")
    ap.add_argument("--dry", action="store_true", help="试算但不压全片")
    ap.add_argument("--run", action="store_true", help="执行压缩并回传")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几片")
    ap.add_argument("--rec", type=int, default=0, help="只处理这一场录像")
    ap.add_argument("--encoder", default="", help="nvenc/qsv/amf/x264")
    args = ap.parse_args(argv)

    cfg = config.load()
    host = args.host or str(cfg.get("host") or "")
    token = cfg.get("token") if args.token is None else args.token
    if args.encoder:
        cfg["encoder"] = args.encoder

    try:
        client, link = api.connect(host, str(cfg.get("fallback_host") or ""), token)
    except api.ApiError as exc:
        print("连接失败：%s" % exc)
        return 1
    print("已连接：%s（%s）" % (client.host, link))

    live, names = client.live()
    if live:
        print("当前有直播在进行：%s" % "、".join(names))

    parts = job.scan(client, int(cfg.get("min_bitrate") or 0),
                     int(cfg.get("min_part_mb") or 0) * 1024 * 1024)
    if args.rec:
        parts = [p for p in parts if p.rec_id == args.rec]
    if not parts:
        print("没有可处理的分片")
        return 0

    if args.scan or not (args.run or args.dry):
        total = sum(p.bytes for p in parts)
        print("%d 片，共 %.1f MB" % (len(parts), total / 1048576.0))
        for p in parts:
            print(_fmt_row(p))
        return 0

    session = job.Session(cfg, client,
                          log=lambda lv, msg: print("[%s] %s" % (lv, msg)))
    print("编码器：%s" % session.describe())
    if not session.ready:
        return 2

    if cfg.get("pause_when_live") and live:
        print("直播中不做高 IO 任务，结束后再跑")
        return 3

    todo = [p for p in parts if p.status != "skip"]
    if args.limit:
        todo = todo[:args.limit]
    if args.dry:
        for p in todo:
            p.status = "pending"
        print("试算 %d 片" % len(todo))

    saved = 0
    for i, part in enumerate(todo, 1):
        print("\n[%d/%d] %s" % (i, len(todo), part.title))
        session.run(part, on_step=lambda p, name, r: print(
            "  %s %s" % (name, "" if r < 0 else "%d%%" % (r * 100)), end="\r"))
        print(_fmt_row(part))
        saved += part.saved
        if session.stopped:
            break
    print("\n合计省下 %.1f MB" % (saved / 1048576.0))
    session.prune_archive()
    return 0


if __name__ == "__main__":
    sys.exit(main())
