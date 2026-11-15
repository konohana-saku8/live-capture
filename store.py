# -*- coding: utf-8 -*-
"""SQLite storage for danmaku / Super Chat / room events / capture runs.

Only the Python standard library is used. One connection is shared between the
HTTP threads and the collector threads, guarded by an RLock. Every table has a
room_id so a room's feed and events stay separate.
"""
from __future__ import annotations

import csv
import io
import json
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
  emote_url    TEXT NOT NULL DEFAULT '',
  emote_px     INTEGER NOT NULL DEFAULT 20,
  emote_map    TEXT NOT NULL DEFAULT '{}',
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
CREATE INDEX IF NOT EXISTS idx_ev_room ON room_event(room_id, received_at DESC);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id     INTEGER NOT NULL,
  started_at  INTEGER NOT NULL,
  stopped_at  INTEGER,
  config_json TEXT
);
"""

# 升级旧库：早期版本没有表情图片字段
MIGRATIONS = {
    "danmaku": {"emote_url": "TEXT NOT NULL DEFAULT ''",
                "emote_px": "INTEGER NOT NULL DEFAULT 20",
                "emote_map": "TEXT NOT NULL DEFAULT '{}'"},
}

TABLE_OF = {"danmaku": "danmaku", "sc": "sc_message", "events": "room_event"}
SEARCH_OF = {
    "danmaku": ("text", "uname"),
    "sc": ("message", "message_trans", "uname"),
    "events": ("detail", "uname"),
}
CSV_COLUMNS = {
    "danmaku": ("seq", "room_id", "received_at", "event_time", "uname", "uid",
                "text", "is_emoticon", "emote_url", "medal_name", "medal_level",
                "guard_level", "is_admin"),
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
        for table, columns in MIGRATIONS.items():
            existing = {row[1] for row in
                        self.db.execute("PRAGMA table_info(%s)" % table)}
            for name, decl in columns.items():
                if name not in existing:
                    self.db.execute("ALTER TABLE %s ADD COLUMN %s %s" %
                                    (table, name, decl))
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
             medal_name, medal_level, guard_level, is_admin, is_emoticon,
             emote_url, emote_px, emote_map, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        data = []
        for r in rows:
            emote_map = r.get("emote_map") or {}
            if not isinstance(emote_map, str):
                emote_map = json.dumps(emote_map, ensure_ascii=False)
            data.append((r.get("run_id"), r.get("room_id", 0), r.get("uid", 0),
                         r.get("uname", ""), str(r.get("text") or ""),
                         r.get("event_time", 0), r.get("received_at", 0),
                         r.get("medal_name", ""), r.get("medal_level", 0),
                         r.get("guard_level", 0), r.get("is_admin", 0),
                         r.get("is_emoticon", 0), r.get("emote_url", ""),
                         _as_int(r.get("emote_px"), 20), emote_map,
                         r.get("raw_json", "")))
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

    def clear(self, room_id=None):
        rid = _as_int(room_id)
        with self.lock:
            for table in ("danmaku", "sc_message", "room_event"):
                if rid:
                    self.db.execute("DELETE FROM %s WHERE room_id=?" % table,
                                    (rid,))
                else:
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
        body = json.dumps(
            {"kind": kind, "count": len(items), "items": items},
            ensure_ascii=False, indent=2)
        return "%s.json" % kind, "application/json; charset=utf-8", body

    # ---------------- dashboard ----------------

    def dashboard(self):
        """Aggregates used by the home page: totals, per-room, recent, timeline."""
        with self.lock:
            totals = {}
            for key, table in TABLE_OF.items():
                totals[key] = self.db.execute(
                    "SELECT COUNT(*) FROM " + table).fetchone()[0]
            sc = self.db.execute(
                "SELECT COALESCE(SUM(price),0), COALESCE(MAX(price),0) "
                "FROM sc_message WHERE deleted=0").fetchone()
            totals["sc_amount"] = sc[0]
            totals["sc_max"] = sc[1]
            totals["rooms"] = self.db.execute(
                "SELECT COUNT(DISTINCT room_id) FROM ("
                "SELECT room_id FROM danmaku UNION "
                "SELECT room_id FROM sc_message UNION "
                "SELECT room_id FROM room_event)").fetchone()[0]

            by_room = {}
            for key, table in TABLE_OF.items():
                for row in self.db.execute(
                        "SELECT room_id, COUNT(*) AS n FROM %s GROUP BY room_id"
                        % table):
                    item = by_room.setdefault(row["room_id"], {"room_id": row["room_id"],
                                                               "danmaku": 0, "sc": 0,
                                                               "events": 0,
                                                               "sc_amount": 0,
                                                               "last_at": 0})
                    item[key] = row["n"]
                for row in self.db.execute(
                        "SELECT room_id, MAX(received_at) AS t FROM %s GROUP BY room_id"
                        % table):
                    item = by_room.setdefault(row["room_id"], {"room_id": row["room_id"],
                                                               "danmaku": 0, "sc": 0,
                                                               "events": 0,
                                                               "sc_amount": 0,
                                                               "last_at": 0})
                    item["last_at"] = max(item["last_at"], row["t"] or 0)
            for row in self.db.execute(
                    "SELECT room_id, COALESCE(SUM(price),0) AS s FROM sc_message "
                    "WHERE deleted=0 GROUP BY room_id"):
                item = by_room.setdefault(row["room_id"], {"room_id": row["room_id"],
                                                           "danmaku": 0, "sc": 0,
                                                           "events": 0,
                                                           "sc_amount": 0,
                                                           "last_at": 0})
                item["sc_amount"] = row["s"]

            top_sc = [dict(r) for r in self.db.execute(
                "SELECT room_id, uname, price, message, message_trans, received_at "
                "FROM sc_message WHERE deleted=0 ORDER BY price DESC, received_at DESC "
                "LIMIT 5")]

            recent = []
            for kind, sql in (
                ("sc", "SELECT 'sc' AS kind, room_id, received_at, uname, "
                       "price, message AS text FROM sc_message WHERE deleted=0 "
                       "ORDER BY received_at DESC LIMIT 8"),
                ("events", "SELECT 'events' AS kind, room_id, received_at, uname, "
                           "0 AS price, detail AS text FROM room_event "
                           "ORDER BY received_at DESC LIMIT 8"),
                ("danmaku", "SELECT 'danmaku' AS kind, room_id, received_at, uname, "
                            "0 AS price, text FROM danmaku "
                            "ORDER BY received_at DESC LIMIT 8")):
                recent.extend(dict(r) for r in self.db.execute(sql))
            recent.sort(key=lambda x: x["received_at"] or 0, reverse=True)
            recent = recent[:15]

            now = int(time.time() * 1000)
            span = 3600 * 1000
            start = now - 12 * span
            timeline = {"danmaku": [0] * 12, "sc": [0] * 12, "events": [0] * 12}
            for key, table in TABLE_OF.items():
                for row in self.db.execute(
                        "SELECT received_at FROM %s WHERE received_at>=?" % table,
                        (start,)):
                    idx = int((row[0] - start) / span)
                    if 0 <= idx < 12:
                        timeline[key][idx] += 1
            labels = []
            for i in range(12):
                d = time.localtime((start + i * span) / 1000)
                labels.append("%02d:00" % d.tm_hour)
        return {"totals": totals,
                "by_room": sorted(by_room.values(), key=lambda x: x["room_id"]),
                "top_sc": top_sc, "recent": recent,
                "timeline": {"labels": labels, **timeline},
                "last_received_at": max(
                    [x["last_at"] for x in by_room.values()] + [0])}


def _safe_config(config):
    """Persist run settings without any credential."""
    keep = ("room_id", "protover", "heartbeat_seconds", "record_danmaku",
            "record_sc", "record_events")
    return json.dumps({k: config.get(k) for k in keep}, ensure_ascii=False)
