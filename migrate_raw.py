# -*- coding: utf-8 -*-
"""One-off migration: archive raw_json to files, backfill columns, free space.

Run while the service is stopped (VACUUM needs an exclusive-ish lock and the
backfill touches every row). Safe to interrupt: each table is committed in
batches, and already-migrated rows have NULL raw_json so a re-run skips them.

Usage:  python migrate_raw.py [--data data] [--no-vacuum] [--batch 2000]

For each table:
  1. stream rows with non-empty raw_json to data/cache/raw/legacy-<table>.jsonl
  2. extract the informative fields into the new flat columns
  3. set raw_json = NULL
Then VACUUM to return the freed pages to the filesystem.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import raw_fields  # noqa: E402

# table -> (raw column, time column, room column, extract target, id column)
TABLES = {
    "danmaku": ("raw_json", "received_at", "room_id", "danmaku", "seq"),
    "sc_message": ("raw_json", "received_at", "room_id", "sc", "sc_id"),
    "room_event": ("raw_json", "received_at", "room_id", "events", "seq"),
    "dynamics": ("raw_json", "pub_ts", "mid", "dynamics", "dyn_id"),
}


def _fields(table, names, row):
    """Build a SET clause and args for the flat columns of one row."""
    values = raw_fields.extract(table, row["_payload"])
    cols = []
    args = []
    for key in names:
        cols.append("%s=?" % key)
        if key in values:
            args.append(values[key])
        else:
            args.append(0 if raw_fields.FIELDS[table][key][1] == "int" else "")
    return cols, args


def migrate_table(db, table, cache_dir, batch):
    rawcol, ts_col, room_col, target, id_col = TABLES[table]
    cols = [r[1] for r in db.execute("PRAGMA table_info(%s)" % table)]
    missing = [k for k in raw_fields.FIELDS[target] if k not in cols]
    if missing:
        print("  %s: 缺少列 %s，请先启动一次服务让其自动迁移" %
              (table, missing[:5]), flush=True)
        return 0, 0

    names = list(raw_fields.FIELDS[target])
    where = "%s IS NOT NULL AND %s != ''" % (rawcol, rawcol)
    total = db.execute("SELECT COUNT(*) FROM %s WHERE %s" % (table, where)).fetchone()[0]
    if not total:
        print("  %s: 无待迁移记录" % table, flush=True)
        return 0, 0

    path = os.path.join(cache_dir, "legacy-%s.jsonl" % table)
    done = 0
    t0 = time.time()
    while True:
        rows = db.execute(
            "SELECT %s AS id, %s AS ts, %s AS rid, %s AS raw FROM %s "
            "WHERE %s LIMIT %d" % (id_col, ts_col, room_col, rawcol, table, where, batch)
        ).fetchall()
        if not rows:
            break
        lines = []
        updates = []
        for r in rows:
            raw = r["raw"] or ""
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                payload = {}
            lines.append(json.dumps({
                "t": r["ts"] or 0, "r": r["rid"] or 0,
                "c": {"danmaku": "DANMU_MSG", "sc_message": "SUPER_CHAT_MESSAGE",
                      "room_event": "EVENT", "dynamics": "DYNAMIC"}[table],
                "id": str(r["id"]), "m": payload,
            }, ensure_ascii=False, separators=(",", ":")))
            setcols, args = _fields(target, names, {"_payload": payload})
            updates.append((setcols, args, r["id"]))

        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        # dynamics.raw_json 是 NOT NULL，只能置空串；其它表置 NULL
        cleared = "''" if table == "dynamics" else "NULL"
        for setcols, args, ident in updates:
            db.execute("UPDATE %s SET %s, %s=%s WHERE %s=?" %
                       (table, ",".join(setcols), rawcol, cleared, id_col),
                       args + [ident])
        db.commit()
        done += len(rows)
        print("  %s: %d/%d (%.0f%%, %.0fs)" %
              (table, done, total, 100.0 * done / total, time.time() - t0),
              flush=True)
    return done, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--no-vacuum", action="store_true")
    ap.add_argument("--batch", type=int, default=2000)
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data)
    db_path = os.path.join(data_dir, "capture.db")
    if not os.path.exists(db_path):
        print("找不到数据库：%s" % db_path, file=sys.stderr)
        return 1

    cache_dir = os.path.join(data_dir, "cache", "raw")
    os.makedirs(cache_dir, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    grand = 0
    try:
        for table in TABLES:
            print("== %s" % table, flush=True)
            n, _ = migrate_table(db, table, cache_dir, args.batch)
            grand += n
    finally:
        db.close()

    if not args.no_vacuum and grand:
        print("== VACUUM（可能需要几分钟）", flush=True)
        db = sqlite3.connect(db_path)
        try:
            db.execute("VACUUM")
            db.commit()
        finally:
            db.close()
    print("完成：迁移 %d 条，缓存目录 %s" % (grand, cache_dir), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
