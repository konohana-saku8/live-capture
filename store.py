# -*- coding: utf-8 -*-
"""SQLite storage for danmaku / Super Chat / room events / capture runs.

Only the Python standard library is used. One connection is shared between the
HTTP thread and the collector thread, guarded by an RLock.
"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS danmaku (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       INTEGER,
  room_id      INTEGER NOT NULL,
  uid          INTEGER,
  uname        TEXT,
  text         TEXT NOT NULL,
  event_time   INTEGER,
  received_at  INTEGER NOT NULL,
  medal_name   TEXT,
  medal_level  INTEGER,
  guard_level  INTEGER,
  is_admin     INTEGER NOT NULL DEFAULT 0,
  is_emoticon  INTEGER NOT NULL DEFAULT 0,
  raw_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_dm_time ON danmaku(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_dm_room ON danmaku(room_id, received_at DESC);

CREATE TABLE IF NOT EXISTS sc_message (
  sc_id         TEXT PRIMARY KEY,
  run_id        INTEGER,
  room_id       INTEGER NOT NULL,
  uid           INTEGER,
  uname         TEXT,
  price         INTEGER,
  message       TEXT,
  message_trans TEXT,
  start_time    INTEGER,
  end_time      INTEGER,
  received_at   INTEGER NOT NULL,
  guard_level   INTEGER,
  medal_name    TEXT,
  medal_level   INTEGER,
  deleted       INTEGER NOT NULL DEFAULT 0,
  raw_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_sc_time ON sc_message(start_time DESC);
CREATE INDEX IF NOT EXISTS idx_sc_room ON sc_message(room_id, start_time DESC);

CREATE TABLE IF NOT EXISTS room_event (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      INTEGER,
  room_id     INTEGER NOT NULL,
  kind        TEXT NOT NULL,
  uid         INTEGER,
  uname       TEXT,
  detail      TEXT,
  event_time  INTEGER,
  received_at INTEGER NOT NULL,
  raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_time ON room_event(received_at DESC);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id     INTEGER NOT NULL,
  started_at  INTEGER NOT NULL,
  stopped_at  INTEGER,
  config_json TEXT
);
"""

TABLE_OF = {"danmaku": "danmaku", "sc": "sc_message", "events": "room_event"}
SEARCH_OF = {
    "danmaku": ("text", "uname"),
    "sc": ("message", "message_trans", "uname"),
    "events": ("detail", "uname"),
}
CSV_COLUMNS = {
    "danmaku": ("seq", "room_id", "received_at", "event_time", "uname", "uid",
                "text", "medal_name", "medal_level", "guard_level", "is_admin"),
    "sc": ("sc_id", "room_id", "received_at", "start_time", "end_time", "uname",
           "uid", "price", "message", "message_trans", "guard_level", "deleted"),
    "events": ("seq", "room_id", "received_at", "event_time", "kind", "uname",
               "uid", "detail"),
}


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Store:
    def __init__(self, path: str):
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.commit()

    def _write(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    # ---------------- writes ----------------

    def insert_danmaku(self, rows):
        if not rows:
            return 0
        sql = """INSERT INTO danmaku
            (run_id, room_id, uid, uname, text, event_time, received_at,
             medal_name, medal_level, guard_level, is_admin, is_emoticon, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        data = [(r.get("run_id"), r.get("room_id", 0), r.get("uid", 0),
                 r.get("uname", ""), str(r.get("text") or ""),
                 r.get("event_time", 0), r.get("received_at", 0),
                 r.get("medal_name", ""), r.get("medal_level", 0),
                 r.get("guard_level", 0), r.get("is_admin", 0),
                 r.get("is_emoticon", 0), r.get("raw_json", "")) for r in rows]
        with self.lock:
            self.db.executemany(sql, data)
            self.db.commit()
        return len(data)

    def insert_sc(self, row):
        sql = """INSERT INTO sc_message
            (sc_id, run_id, room_id, uid, uname, price, message, message_trans,
             start_time, end_time, received_at, guard_level, medal_name,
             medal_level, deleted, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(sc_id) DO NOTHING"""
        args = (str(row["sc_id"]), row.get("run_id"), row.get("room_id", 0),
                row.get("uid", 0), row.get("uname", ""), row.get("price", 0),
                row.get("message", ""), row.get("message_trans"),
                row.get("start_time", 0), row.get("end_time", 0),
                row.get("received_at", 0), row.get("guard_level", 0),
                row.get("medal_name", ""), row.get("medal_level", 0),
                row.get("deleted", 0), row.get("raw_json", ""))
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount > 0

    def update_sc_translation(self, sc_id, text):
        cur = self._write(
            "UPDATE sc_message SET message_trans=? WHERE sc_id=?",
            (text, str(sc_id)))
        return cur.rowcount > 0

    def mark_sc_deleted(self, ids):
        out = 0
        with self.lock:
            for sc_id in ids:
                out += self.db.execute(
                    "UPDATE sc_message SET deleted=1 WHERE sc_id=?",
                    (str(sc_id),)).rowcount
            self.db.commit()
        return out

    def insert_event(self, row):
        sql = """INSERT INTO room_event
            (run_id, room_id, kind, uid, uname, detail, event_time, received_at, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?)"""
        args = (row.get("run_id"), row.get("room_id", 0), row.get("kind", ""),
                row.get("uid", 0), row.get("uname", ""), row.get("detail", ""),
                row.get("event_time", 0), row.get("received_at", 0),
                row.get("raw_json", ""))
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    def start_run(self, room_id, config):
        cur = self._write(
            "INSERT INTO runs(room_id, started_at, config_json) VALUES (?,?,?)",
            (int(room_id), int(time.time() * 1000), _safe_config(config)))
        return cur.lastrowid

    def end_run(self, run_id):
        if run_id:
            self._write("UPDATE runs SET stopped_at=? WHERE id=?",
                        (int(time.time() * 1000), run_id))

    def clear(self):
        with self.lock:
            for table in ("danmaku", "sc_message", "room_event"):
                self.db.execute("DELETE FROM " + table)
            self.db.commit()

    # ---------------- reads ----------------

    def counts(self):
        with self.lock:
            out = {}
            for key, table in TABLE_OF.items():
                out[key] = self.db.execute(
                    "SELECT COUNT(*) FROM " + table).fetchone()[0]
            row = self.db.execute(
                "SELECT MAX(received_at) FROM danmaku").fetchone()
        out["last_received_at"] = row[0] or 0
        return out

    def _where(self, kind, q, room_id, since, run_id):
        clauses, args = [], []
        if q:
            cols = SEARCH_OF[kind]
            clauses.append("(" + " OR ".join(
                "COALESCE(%s,'') LIKE ?" % c for c in cols) + ")")
            args.extend(["%" + q + "%"] * len(cols))
        if room_id:
            clauses.append("room_id=?")
            args.append(_as_int(room_id))
        if since:
            clauses.append("received_at>=?")
            args.append(_as_int(since))
        if run_id:
            clauses.append("run_id=?")
            args.append(_as_int(run_id))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def query(self, kind, limit=100, offset=0, q="", room_id=None,
              since=None, run_id=None):
        kind = kind if kind in TABLE_OF else "danmaku"
        table = TABLE_OF[kind]
        where, args = self._where(kind, q, room_id, since, run_id)
        limit = max(1, min(1000, _as_int(limit, 100)))
        offset = max(0, _as_int(offset))
        second = "sc_id" if kind == "sc" else "seq"
        sql = ("SELECT * FROM " + table + where +
               " ORDER BY received_at DESC, " + second +
               " DESC LIMIT ? OFFSET ?")
        with self.lock:
            total = self.db.execute(
                "SELECT COUNT(*) FROM " + table + where, args).fetchone()[0]
            rows = self.db.execute(sql, args + [limit, offset]).fetchall()
        return total, [dict(r) for r in rows]

    def export(self, kind, fmt="json", q="", room_id=None, since=None):
        kind = kind if kind in TABLE_OF else "danmaku"
        where, args = self._where(kind, q, room_id, since, None)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM " + TABLE_OF[kind] + where +
                " ORDER BY received_at DESC LIMIT 100000", args).fetchall()
        items = [dict(r) for r in rows]
        if fmt == "csv":
            buf = io.StringIO()
            cols = CSV_COLUMNS[kind]
            writer = csv.writer(buf)
            writer.writerow(cols)
            for item in items:
                writer.writerow([item.get(c, "") for c in cols])
            return "%s.csv" % kind, "text/csv; charset=utf-8", \
                "\ufeff" + buf.getvalue()
        body = __import__("json").dumps(
            {"kind": kind, "count": len(items), "items": items},
            ensure_ascii=False, indent=2)
        return "%s.json" % kind, "application/json; charset=utf-8", body


def _safe_config(config):
    """Persist run settings without any credential."""
    import json
    keep = ("room_id", "protover", "heartbeat_seconds", "record_danmaku",
            "record_sc", "record_events")
    return json.dumps({k: config.get(k) for k in keep}, ensure_ascii=False)
