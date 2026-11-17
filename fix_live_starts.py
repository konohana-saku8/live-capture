# -*- coding: utf-8 -*-
"""修正历史导入场次的开播入口：优先最早 SC / 上舰，回退首条弹幕。

旧版 import_history.py 把「开播」放在数据簇首条数据前 1 秒。主播没开播时
也可能有弹幕，所以这里按约定改为：

1. 簇内最早的 SC / 上舰（事件时间与 received_at 取更早，保证该条记录
   落在场内）作为开播；
2. 两者都没有时，回退到首条弹幕前 1 秒。

只改 `import_key` 非空的导入开播标记；实时采集的真实开播不动。
开播标记的 import_key 不变，所以之后重新导入不会多插一条。

用法：
    python fix_live_starts.py --data data --before 2026-11-16 --dry-run
    python fix_live_starts.py --data data --before 2026-11-16
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

from import_history import _paid_time


def _cutoff_ms(day: str) -> int:
    date = datetime.datetime.strptime(day, "%Y-%m-%d").date()
    return int(datetime.datetime.combine(
        date, datetime.time.min).timestamp() * 1000)


def _paid_start(db, rid, start, end):
    found = []
    row = db.execute(
        "SELECT start_time, received_at FROM sc_message"
        " WHERE room_id=? AND received_at>=? AND received_at<=?"
        " ORDER BY COALESCE(NULLIF(start_time,0)*1000, received_at)"
        " ASC LIMIT 1", (rid, start, end)).fetchone()
    if row:
        found.append(_paid_time(row[0], row[1]))
    row = db.execute(
        "SELECT event_time, received_at FROM room_event"
        " WHERE room_id=? AND kind='guard'"
        " AND received_at>=? AND received_at<=?"
        " ORDER BY COALESCE(NULLIF(event_time,0)*1000, received_at)"
        " ASC LIMIT 1", (rid, start, end)).fetchone()
    if row:
        found.append(_paid_time(row[0], row[1]))
    return min(found) if found else 0


def _first_danmaku(db, rid, start, end):
    row = db.execute(
        "SELECT MIN(received_at) FROM danmaku"
        " WHERE room_id=? AND received_at>=? AND received_at<=?",
        (rid, start, end)).fetchone()
    return row[0] if row and row[0] else 0


def _fmt(ms):
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ms / 1000.0)) if ms else "-"


def fix(data_dir: str, cutoff: int, room_id: int = 0,
        dry_run: bool = False, backup: bool = True):
    db_path = os.path.join(os.path.abspath(data_dir), "capture.db")
    if not os.path.isfile(db_path):
        raise SystemExit("找不到数据库：%s" % db_path)

    if backup and not dry_run:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = db_path + ".bak-live-start-" + stamp
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(bak)
        src.backup(dst)
        dst.close()
        src.close()
        print("已备份：%s" % bak)

    db = sqlite3.connect(db_path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=20000")
    sql = ("SELECT seq, room_id, received_at, event_time, raw_json"
           " FROM room_event"
           " WHERE kind='live' AND detail='开播' AND import_key IS NOT NULL"
           " AND received_at < ?")
    args = [cutoff]
    if room_id:
        sql += " AND room_id=?"
        args.append(int(room_id))
    sql += " ORDER BY room_id, received_at"
    rows = db.execute(sql, args).fetchall()
    changed = 0
    for row in rows:
        rid = int(row["room_id"])
        old_open = int(row["received_at"])
        close = db.execute(
            "SELECT received_at FROM room_event"
            " WHERE room_id=? AND kind='live' AND detail='下播'"
            " AND received_at > ? ORDER BY received_at LIMIT 1",
            (rid, old_open)).fetchone()
        if not close:
            print("跳过 %s 房间 %s：找不到配对的「下播」" %
                  (_fmt(old_open), rid))
            continue
        close_at = int(close["received_at"])
        paid = _paid_start(db, rid, old_open, close_at)
        if paid:
            new_open = paid
            basis = "first_sc_guard"
        else:
            first_dm = _first_danmaku(db, rid, old_open, close_at)
            new_open = max(0, first_dm - 1000) if first_dm else old_open
            basis = "first_danmaku"
        if not new_open:
            print("跳过 %s 房间 %s：没有可用的开播依据" %
                  (_fmt(old_open), rid))
            continue
        raw = {}
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except ValueError:
            raw = {}
        raw.update({"imported": True, "approx": True, "basis": basis})
        raw_json = json.dumps(raw, ensure_ascii=False)
        delta = round((new_open - old_open) / 1000.0, 1)
        print("房间 %s：%s -> %s（%+.1fs，%s）" %
              (rid, _fmt(old_open), _fmt(new_open), delta, basis))
        if not dry_run:
            db.execute(
                "UPDATE room_event SET received_at=?, event_time=?, raw_json=?"
                " WHERE seq=?",
                (new_open, new_open // 1000, raw_json, row["seq"]))
            changed += 1
    if not dry_run:
        db.commit()
    db.close()
    print("完成：%s %d 条开播标记" %
          ("将修改" if dry_run else "已修改", len(rows) if dry_run else changed))
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="按最早 SC/上舰修正历史导入场次的开播入口")
    parser.add_argument("--data", default="data")
    parser.add_argument("--before", default="2026-11-16",
                        help="只改这个日期之前的导入开播标记，默认 2026-11-16")
    parser.add_argument("--room", type=int, default=0,
                        help="只改这个房间号；0 表示所有导入房间")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    fix(args.data, _cutoff_ms(args.before), args.room, args.dry_run,
        backup=not args.no_backup)


if __name__ == "__main__":
    main()
