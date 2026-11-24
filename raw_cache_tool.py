# -*- coding: utf-8 -*-
"""Search archived raw payloads (both live cache and legacy migration dumps).

Examples:
  python raw_cache_tool.py --day 2026-11-24 --room 22748536 --cmd DANMU_MSG
  python raw_cache_tool.py --day 2026-11-24 --q "ひの雑" --limit 5
  python raw_cache_tool.py --list

Each matching line is printed as pretty JSON with a short prefix
(day, timestamp, room, cmd). Use --full to print the whole message.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _files(cache_dir, day):
    if not os.path.isdir(cache_dir):
        return []
    if day:
        needles = ("raw-%s.jsonl" % day, "legacy-")
        return [os.path.join(cache_dir, n) for n in sorted(os.listdir(cache_dir))
                if n.endswith(".jsonl") and
                (n == "raw-%s.jsonl" % day or n.startswith("legacy-"))]
    return [os.path.join(cache_dir, n) for n in sorted(os.listdir(cache_dir))
            if n.endswith(".jsonl")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--day", default="", help="YYYY-MM-DD")
    ap.add_argument("--room", type=int, default=0)
    ap.add_argument("--cmd", default="")
    ap.add_argument("--q", default="", help="substring in the raw message")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--list", action="store_true", help="list cache files")
    args = ap.parse_args()

    cache_dir = os.path.join(os.path.abspath(args.data), "cache", "raw")
    files = _files(cache_dir, args.day)
    if args.list:
        for p in files:
            n = sum(1 for _ in open(p, encoding="utf-8", errors="replace"))
            print("%s  %d lines  %.1f KB" %
                  (os.path.basename(p), n, os.path.getsize(p) / 1024.0))
        return 0

    shown = 0
    needle = args.q.lower()
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if shown >= args.limit:
                    break
                try:
                    d = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if args.room and d.get("r") != args.room:
                    continue
                if args.cmd and d.get("c") != args.cmd:
                    continue
                if needle and needle not in line.lower():
                    continue
                ts = d.get("t") or 0
                try:
                    stamp = time.strftime("%m-%d %H:%M:%S",
                                          time.localtime(ts / 1000))
                except (ValueError, TypeError, OSError):
                    stamp = "??"
                msg = d.get("m")
                if args.full:
                    body = json.dumps(msg, ensure_ascii=False, indent=1)
                else:
                    body = json.dumps(msg, ensure_ascii=False,
                                      separators=(",", ":"))[:300]
                print("[%s] r=%s c=%s\n%s\n" %
                      (stamp, d.get("r"), d.get("c"), body))
                shown += 1
        if shown >= args.limit:
            break
    if not shown:
        print("没有匹配的记录（缓存目录：%s）" % cache_dir, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
