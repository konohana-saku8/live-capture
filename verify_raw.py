# -*- coding: utf-8 -*-
"""Verify the raw_json migration by re-deriving every row from the cache.

The migration replaced a big ``raw_json`` column with (a) a legacy jsonl
archive of the original payload and (b) a set of flat columns.  Two things
can go wrong, and both are checked here without trusting the migration:

* the original columns (text / uid / uname / ...) got touched
* the new columns hold values from the wrong path (off-by-one in a batch
  INSERT, a shifted mapping, ...)

For each archived row we look the row up by primary key and re-run the
production parsers (``capture.build_danmaku`` / ``extract_sc`` /
``event_row`` / ``raw_fields.extract``) against the archived payload.  Any
disagreement with what SQLite currently holds is reported.

Usage:  python verify_raw.py [--data data] [--limit N] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import raw_fields  # noqa: E402

# table -> primary key, extract target, rebuild kind, original columns
SPEC = {
    "danmaku": {
        "id": "seq", "ts": "received_at", "room": "room_id", "target": "danmaku",
        "rebuild": "danmaku", "need": "info",
        "base": ("uid", "uname", "text", "event_time", "medal_name",
                 "medal_level", "guard_level", "is_admin", "is_emoticon"),
        # 身份与正文：不依赖解析器版本，必须零差异
        "key": ("uid", "uname", "text"),
    },
    "sc_message": {
        "id": "sc_id", "ts": "received_at", "room": "room_id", "target": "sc",
        "rebuild": "sc", "need": "id",
        "base": ("uid", "uname", "price", "message", "start_time", "end_time",
                 "guard_level", "medal_name", "medal_level"),
        "key": ("uid", "uname", "price", "message"),
    },
    "room_event": {
        "id": "seq", "ts": "received_at", "room": "room_id", "target": "events",
        "rebuild": "event", "need": None,
        "base": ("uid", "uname", "detail", "event_time"),
        # detail 是解析器生成的展示文案，不同版本措辞会变，不算关键列
        "key": ("uid", "uname"),
    },
    "dynamics": {
        "id": "dyn_id", "ts": "pub_ts", "room": "mid", "target": "dynamics",
        "rebuild": None, "need": None,
        "base": ("text", "dyn_type", "pub_ts"),
        "key": ("text", "dyn_type"),
    },
}

EMPTY = (None, "", 0)

# 抽样看新列到底填进去多少，避免"整列恒为默认值"这种静默失败
FILL = {
    "danmaku": (("dm_id", "str"), ("dm_color", "int"), ("font_size", "int"),
                ("user_level", "str"), ("medal_id", "int"), ("reply_mid", "int")),
    "sc_message": (("gift_id", "int"), ("rate", "int"), ("duration", "int"),
                   ("face", "str")),
    "room_event": (("gift_id", "int"), ("price", "int"), ("coin_type", "str")),
    "dynamics": (("comment_id", "str"), ("author_name", "str"),
                 ("pub_action", "str")),
}


def _same(db_val, want):
    """Compare a stored value with a re-derived one (None == '' == 0 here)."""
    if db_val in EMPTY and want in EMPTY:
        return True
    if want is None:
        return db_val in EMPTY
    try:
        return str(db_val) == str(want)
    except Exception:
        return False


def _rebuild(kind, payload, room_id, ts, dbrow):
    """Re-derive the original columns from the archived payload."""
    import capture  # lazily imported: only this verifier needs the parser
    if kind == "danmaku":
        info = payload.get("info")
        if not isinstance(info, list) or not info:
            return None
        return capture.build_danmaku(room_id, info, ts)
    if kind == "sc":
        if not isinstance(payload, dict) or payload.get("id") is None:
            return None
        return capture.extract_sc(room_id, payload, ts)
    if kind == "event":
        return capture.event_row(room_id, dbrow["kind"] or "", payload, ts)
    return None


def check_table(db, table, spec, cache_path, limit=None):
    """Stream the archive, batch-read DB rows, compare field by field."""
    st = Counter()
    examples = []
    cols = [r[1] for r in db.execute("PRAGMA table_info(%s)" % table)]
    new_cols = [k for k in raw_fields.FIELDS[spec["target"]]
                if k in cols and k != "raw_json"]
    base_cols = [c for c in spec["base"] if c in cols]
    key_cols = set(spec.get("key") or ())
    has_kind = "kind" in cols
    id_col = spec["id"]

    room_col = spec["room"]
    select = ",".join([id_col, room_col] + (["kind"] if has_kind else [])
                      + base_cols + new_cols)
    seen = set()

    def flush(buf):
        if not buf:
            return
        marks = ",".join("?" * len(buf))
        rows = {}
        for r in db.execute("SELECT %s FROM %s WHERE %s IN (%s)"
                            % (select, table, id_col, marks),
                            [b[0] for b in buf]):
            rows[str(r[id_col])] = r
        for ident, payload, ts in buf:
            key = str(ident)
            if key in seen:
                st["duplicate_id"] += 1
                continue
            seen.add(key)
            row = rows.get(key)
            if row is None:
                st["missing_in_db"] += 1
                continue
            st["rows"] += 1

            # payload lost at write time (old raw_json was truncated to 4 KB)?
            if not isinstance(payload, (dict, list)) or not payload:
                st["payload_empty"] += 1
                continue
            if spec["need"] and spec["need"] not in payload:
                st["payload_unexpected_shape"] += 1
                continue

            got = _rebuild(spec["rebuild"], payload, row[room_col] or 0,
                           ts or 0, row) if spec["rebuild"] else None
            if spec["rebuild"] and got is None:
                st["rebuild_failed"] += 1
                continue

            if got is not None:
                for c in base_cols:
                    if not _same(row[c], got.get(c)):
                        st["base_mismatch"] += 1
                        st["col:" + c] += 1
                        if c in key_cols:
                            st["key_mismatch"] += 1
                        if len(examples) < 8:
                            examples.append({"side": "original", "id": key,
                                             "col": c, "db": row[c],
                                             "rebuilt": got.get(c)})

            vals = raw_fields.extract(spec["target"], payload)
            for c in new_cols:
                if not _same(row[c], vals.get(c)):
                    st["new_mismatch"] += 1
                    if len(examples) < 8:
                        examples.append({"side": "new", "id": key, "col": c,
                                         "db": row[c], "rebuilt": vals.get(c)})

    buf = []
    if not os.path.exists(cache_path):
        st["no_archive"] = 1
        for k in ("lines", "rows", "base_mismatch", "new_mismatch",
                  "payload_empty", "missing_in_db", "rebuild_failed",
                  "duplicate_id", "bad_line", "key_mismatch"):
            st.setdefault(k, 0)
        return st, examples
    with open(cache_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            st["lines"] += 1
            try:
                rec = json.loads(line)
            except ValueError:
                st["bad_line"] += 1
                continue
            buf.append((rec.get("id"), rec.get("m") or {}, rec.get("t") or 0))
            if len(buf) >= 500:
                flush(buf)
                buf = []
            if limit and st["lines"] >= limit:
                break
    flush(buf)
    for k in ("lines", "rows", "base_mismatch", "new_mismatch", "payload_empty",
              "missing_in_db", "rebuild_failed", "duplicate_id", "bad_line",
              "key_mismatch"):
        st.setdefault(k, 0)
    return st, examples


def sanity(db, table, ts_col):
    """Cheap invariants that do not depend on the archive."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(%s)" % table)]
    row = db.execute(
        "SELECT COUNT(*), MIN(%s), MAX(%s), "
        "SUM(CASE WHEN %s IS NULL OR %s <= 0 THEN 1 ELSE 0 END) FROM %s"
        % (ts_col, ts_col, ts_col, ts_col, table)).fetchone()
    out = {"rows": row[0], "ts_min": row[1], "ts_max": row[2],
           "ts_bad": row[3] or 0}
    out["raw_remaining"] = (db.execute(
        "SELECT COUNT(*) FROM %s WHERE raw_json IS NOT NULL AND raw_json != ''"
        % table).fetchone()[0] if "raw_json" in cols else -1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data)
    db_path = os.path.join(data_dir, "capture.db")
    cache_dir = os.path.join(data_dir, "cache", "raw")
    if not os.path.exists(db_path):
        print("找不到数据库：%s" % db_path, file=sys.stderr)
        return 1

    # read-only: never disturb the running capture service
    db = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    db.row_factory = sqlite3.Row
    report, failed = {}, []

    for table, spec in SPEC.items():
        print("== %s" % table, flush=True)
        st, examples = check_table(db, table, spec,
                                   os.path.join(cache_dir, "legacy-%s.jsonl" % table),
                                   args.limit or None)
        report[table] = {"verify": dict(st), "sanity": sanity(db, table, spec["ts"]),
                         "examples": examples}
        hard = (st["missing_in_db"] or st["new_mismatch"]
                or st["key_mismatch"] or st["rebuild_failed"])
        if hard:
            failed.append(table)
        print("   归档 %d · 比对 %d · 关键列不一致 %d · 其他原有列不一致 %d"
              " · 新列不一致 %d · 空payload %d · 不在库 %d · 重建失败 %d"
              % (st["lines"], st["rows"], st["key_mismatch"],
                 st["base_mismatch"] - st["key_mismatch"], st["new_mismatch"],
                 st["payload_empty"], st["missing_in_db"], st["rebuild_failed"]),
              flush=True)
        for ex in examples[:3]:
            print("     ! %s id=%s %s: db=%r rebuilt=%r"
                  % (ex["side"], ex["id"], ex["col"], ex["db"], ex["rebuilt"]),
                  flush=True)

    print("== 覆盖检查", flush=True)
    for table in SPEC:
        st, san = report[table]["verify"], report[table]["sanity"]
        print("   %-11s 库内 %6d 行 · raw_json 残留 %d · 归档 %6d 行 · 时间 %s ~ %s"
              % (table, san["rows"], san["raw_remaining"], st["lines"],
                 san["ts_min"], san["ts_max"]), flush=True)

    print("== 新列填充率（避免整列为默认值）", flush=True)
    for table, cols in FILL.items():
        have = [r[1] for r in db.execute("PRAGMA table_info(%s)" % table)]
        cols = [(c, k) for c, k in cols if c in have]
        if not cols:
            continue
        total = db.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] or 1
        parts = []
        for col, kind in cols:
            if kind == "int":
                n = db.execute("SELECT COUNT(*) FROM %s WHERE %s != 0"
                               % (table, col)).fetchone()[0]
            else:
                n = db.execute("SELECT COUNT(*) FROM %s WHERE %s != '' "
                               "AND %s IS NOT NULL" % (table, col, col)).fetchone()[0]
            parts.append("%s %d%%" % (col, 100 * n // total))
        print("   %-11s %s" % (table, " · ".join(parts)), flush=True)

    report["verdict"] = "FAIL" if failed else "PASS"
    print("\n结论：%s%s" % (report["verdict"],
                          ("（%s）" % ",".join(failed)) if failed else ""), flush=True)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
