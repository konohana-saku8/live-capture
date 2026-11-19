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
  raw_json     TEXT,
  import_key   TEXT
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
  raw_json    TEXT,
  import_key  TEXT
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

CREATE TABLE IF NOT EXISTS dynamics (
  dyn_id      TEXT PRIMARY KEY,
  mid         INTEGER NOT NULL DEFAULT 0,
  pub_ts      INTEGER NOT NULL DEFAULT 0,
  dyn_type    TEXT NOT NULL DEFAULT '',
  text        TEXT NOT NULL DEFAULT '',
  text_trans  TEXT NOT NULL DEFAULT '',
  pics        TEXT NOT NULL DEFAULT '[]',
  jump_url    TEXT NOT NULL DEFAULT '',
  raw_json    TEXT NOT NULL DEFAULT '',
  fetched_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dyn_time ON dynamics(pub_ts DESC);

CREATE TABLE IF NOT EXISTS recordings (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id         INTEGER NOT NULL,
  label           TEXT,
  live_started_at INTEGER,
  started_at      INTEGER NOT NULL,
  stopped_at      INTEGER,
  status          TEXT NOT NULL DEFAULT 'recording',
  requested_qn    INTEGER,
  actual_qn       INTEGER,
  flv_parts       TEXT NOT NULL DEFAULT '[]',
  mp4_parts       TEXT NOT NULL DEFAULT '[]',
  bytes           INTEGER NOT NULL DEFAULT 0,
  duration_ms     INTEGER NOT NULL DEFAULT 0,
  error           TEXT NOT NULL DEFAULT '',
  created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rec_room ON recordings(room_id, started_at DESC);
"""

# 升级旧库：早期版本没有表情图片字段，历史导入也需要一个去重键
MIGRATIONS = {
    "danmaku": {"emote_url": "TEXT NOT NULL DEFAULT ''",
                "emote_px": "INTEGER NOT NULL DEFAULT 20",
                "emote_map": "TEXT NOT NULL DEFAULT '{}'",
                "import_key": "TEXT"},
    "room_event": {"import_key": "TEXT"},
    # 多分片自动合并成单 MP4：状态与产物落库，进程重启不丢
    "recordings": {"merge_status": "TEXT NOT NULL DEFAULT ''",
                   "merged_path": "TEXT NOT NULL DEFAULT ''",
                   "merged_bytes": "INTEGER NOT NULL DEFAULT 0",
                   "merged_duration_ms": "INTEGER NOT NULL DEFAULT 0",
                   "merge_error": "TEXT NOT NULL DEFAULT ''",
                   "merge_seconds": "REAL NOT NULL DEFAULT 0"},
}

# 历史导入去重：同一份历史库反复导入不会重复写（NULL 不参与唯一约束）
POST_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_dm_import
  ON danmaku(import_key) WHERE import_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ev_import
  ON room_event(import_key) WHERE import_key IS NOT NULL;
"""

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


def _json_list(value):
    try:
        data = json.loads(value or "[]")
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def _rec_row(row):
    item = dict(row)
    item["flv_parts"] = _json_list(item.get("flv_parts"))
    item["mp4_parts"] = _json_list(item.get("mp4_parts"))
    return item


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
        self.db.executescript(POST_SCHEMA)
        self.db.commit()

    def _write(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    # ---------------- writes ----------------

    def insert_danmaku(self, rows):
        """批量写弹幕；带 import_key 的行按去重键幂等（历史导入反复跑不重复）。"""
        if not rows:
            return 0
        sql = """INSERT OR IGNORE INTO danmaku
            (run_id, room_id, uid, uname, text, event_time, received_at,
             medal_name, medal_level, guard_level, is_admin, is_emoticon,
             emote_url, emote_px, emote_map, raw_json, import_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
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
                         r.get("raw_json", ""), r.get("import_key")))
        with self.lock:
            before = self.db.total_changes
            self.db.executemany(sql, data)
            self.db.commit()
            added = self.db.total_changes - before
            # 把自增 seq 回填给调用方：SSE 推送和轮询结果要用同一个主键去重
            # （executemany 的 cursor.lastrowid 在部分 SQLite 上是 None）
            last = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            if added == len(rows) and last:
                first = last - len(rows) + 1
                for i, row in enumerate(rows):
                    row.setdefault("seq", first + i)
            return added

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
        sql = """INSERT OR IGNORE INTO room_event
            (run_id, room_id, kind, uid, uname, detail, event_time, received_at, raw_json, import_key)
            VALUES (?,?,?,?,?,?,?,?,?,?)"""
        args = (row.get("run_id"), row.get("room_id", 0), row.get("kind", ""),
                row.get("uid", 0), row.get("uname", ""), row.get("detail", ""),
                row.get("event_time", 0), row.get("received_at", 0),
                row.get("raw_json", ""), row.get("import_key"))
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            if cur.rowcount > 0 and cur.lastrowid:
                row["seq"] = cur.lastrowid
            return cur.rowcount > 0

    def insert_events(self, rows):
        """批量写事件（礼物 / 上舰 / 开播下播），返回真正新增的条数。"""
        if not rows:
            return 0
        sql = """INSERT OR IGNORE INTO room_event
            (run_id, room_id, kind, uid, uname, detail, event_time, received_at, raw_json, import_key)
            VALUES (?,?,?,?,?,?,?,?,?,?)"""
        data = [(r.get("run_id"), r.get("room_id", 0), r.get("kind", ""),
                 r.get("uid", 0), r.get("uname", ""), r.get("detail", ""),
                 r.get("event_time", 0), r.get("received_at", 0),
                 r.get("raw_json", ""), r.get("import_key")) for r in rows]
        with self.lock:
            before = self.db.total_changes
            self.db.executemany(sql, data)
            self.db.commit()
            return self.db.total_changes - before

    def start_run(self, room_id, config):
        cur = self._write(
            "INSERT INTO runs(room_id, started_at, config_json) VALUES (?,?,?)",
            (int(room_id), int(time.time() * 1000), _safe_config(config)))
        return cur.lastrowid

    def end_run(self, run_id):
        if run_id:
            self._write("UPDATE runs SET stopped_at=? WHERE id=?",
                        (int(time.time() * 1000), run_id))

    def clear(self, room_id=None, mid=0):
        rid = _as_int(room_id)
        mid = _as_int(mid)
        with self.lock:
            for table in ("danmaku", "sc_message", "room_event"):
                if rid:
                    self.db.execute("DELETE FROM %s WHERE room_id=?" % table,
                                    (rid,))
                else:
                    self.db.execute("DELETE FROM " + table)
            if mid:
                self.db.execute("DELETE FROM dynamics WHERE mid=?", (mid,))
            elif not rid:
                self.db.execute("DELETE FROM dynamics")
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

    def _where(self, kind, q, room_id, since, run_id, until=None):
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
        if until:
            clauses.append("received_at<?")
            args.append(_as_int(until))
        if run_id:
            clauses.append("run_id=?")
            args.append(_as_int(run_id))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def query(self, kind, limit=100, offset=0, q="", room_id=None,
              since=None, run_id=None, until=None):
        kind = kind if kind in TABLE_OF else "danmaku"
        table = TABLE_OF[kind]
        where, args = self._where(kind, q, room_id, since, run_id, until)
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

    def query_all(self, kind, q="", room_id=None, since=None, until=None,
                  cap=100000):
        """Fetch up to ``cap`` rows in chronological order.

        数据页按选中段一次性加载整段内容，不再分页；这个上限只是防止误传
        房间号时把整个库拉出来，正常单场直播（几万条）不会被截断。
        """
        kind = kind if kind in TABLE_OF else "danmaku"
        where, args = self._where(kind, q, room_id, since, None, until)
        second = "sc_id" if kind == "sc" else "seq"
        sql = ("SELECT * FROM " + TABLE_OF[kind] + where +
               " ORDER BY received_at ASC, " + second + " ASC LIMIT ?")
        with self.lock:
            rows = self.db.execute(sql, args + [cap]).fetchall()
        return [dict(r) for r in rows]

    def count_range(self, kind, q="", room_id=None, since=None, until=None):
        """某一类数据在给定范围内的总条数（分页时用来显示「共 N 条」）。"""
        kind = kind if kind in TABLE_OF else "danmaku"
        where, args = self._where(kind, q, room_id, since, None, until)
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM " + TABLE_OF[kind] + where,
                args).fetchone()[0]

    def query_page_before(self, kind, before_ms, count=200, room_id=None,
                          since=None, until=None, inclusive=False, q=""):
        """Return the newest ``count`` rows older than ``before_ms``.

        Used by progressive rendering: start with the tail, page backwards on
        scroll so a long session never freezes the browser with one huge DOM
        write. Rows come back chronological (old → new).

        ``before_ms`` 为 0 时不设上限（取最新的 ``count`` 条）。往前翻页时传
        ``inclusive=True`` 让游标那一毫秒的行也再取一遍，前端按主键去重，
        避免同一毫秒里的几条被漏掉。
        """
        kind = kind if kind in TABLE_OF else "danmaku"
        where, args = self._where(kind, q, room_id, since, None, until)
        if _as_int(before_ms) > 0:
            clause = "received_at%s?" % ("<=" if inclusive else "<")
            where = (where + " AND " + clause) if where else (" WHERE " + clause)
            args.append(_as_int(before_ms))
        second = "sc_id" if kind == "sc" else "seq"
        sql = ("SELECT * FROM " + TABLE_OF[kind] + where +
               " ORDER BY received_at DESC, " + second + " DESC LIMIT ?")
        with self.lock:
            rows = self.db.execute(sql, args + [count]).fetchall()
        return [dict(r) for r in reversed(rows)]

    def query_page_after(self, kind, after_ms, count=200, room_id=None,
                         since=None, until=None, inclusive=False, q=""):
        """Return the oldest ``count`` rows newer than ``after_ms``.

        Used by the studio player: start at the recording's first message and
        page forward as playback advances, without loading the whole session.
        """
        kind = kind if kind in TABLE_OF else "danmaku"
        where, args = self._where(kind, q, room_id, since, None, until)
        if _as_int(after_ms) > 0:
            clause = "received_at%s?" % (">=" if inclusive else ">")
            where = (where + " AND " + clause) if where else (" WHERE " + clause)
            args.append(_as_int(after_ms))
        second = "sc_id" if kind == "sc" else "seq"
        sql = ("SELECT * FROM " + TABLE_OF[kind] + where +
               " ORDER BY received_at ASC, " + second + " ASC LIMIT ?")
        with self.lock:
            rows = self.db.execute(sql, args + [count]).fetchall()
        return [dict(r) for r in rows]

    # ---------------- recordings ----------------

    def create_recording(self, room_id, label, live_started_at, requested_qn,
                         actual_qn=0, started_at=None):
        now = _as_int(started_at) or int(time.time() * 1000)
        cur = self._write(
            "INSERT INTO recordings (room_id, label, live_started_at, started_at,"
            " status, requested_qn, actual_qn, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (_as_int(room_id), str(label or ""), _as_int(live_started_at),
             now, "recording", _as_int(requested_qn), _as_int(actual_qn), now))
        return int(cur.lastrowid)

    def update_recording(self, rec_id, **fields):
        allowed = ("live_started_at", "started_at", "stopped_at", "status",
                   "requested_qn", "actual_qn", "flv_parts", "mp4_parts",
                   "bytes", "duration_ms", "error",
                   "merge_status", "merged_path", "merged_bytes",
                   "merged_duration_ms", "merge_error", "merge_seconds")
        sets, args = [], []
        for key in allowed:
            if key not in fields:
                continue
            value = fields[key]
            if key in ("flv_parts", "mp4_parts") and not isinstance(value, str):
                value = json.dumps(value or [], ensure_ascii=False)
            sets.append(key + "=?")
            args.append(value)
        if not sets:
            return 0
        args.append(_as_int(rec_id))
        cur = self._write("UPDATE recordings SET " + ", ".join(sets) +
                          " WHERE id=?", args)
        return cur.rowcount

    def get_recording(self, rec_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM recordings WHERE id=?",
                                  (_as_int(rec_id),)).fetchone()
        return _rec_row(row) if row else None

    def list_recordings(self, room_id=None, since=None, until=None, status=None):
        where, args = [], []
        if _as_int(room_id):
            where.append("room_id=?")
            args.append(_as_int(room_id))
        if _as_int(since):
            where.append("started_at>=?")
            args.append(_as_int(since))
        if _as_int(until):
            where.append("started_at<=?")
            args.append(_as_int(until))
        if status:
            where.append("status=?")
            args.append(str(status))
        sql = "SELECT * FROM recordings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC"
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        return [_rec_row(row) for row in rows]

    def export(self, kind, fmt="json", q="", room_id=None, since=None,
               until=None):
        kind = kind if kind in TABLE_OF else "danmaku"
        where, args = self._where(kind, q, room_id, since, None, until)
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

    def session_stats(self, room_id, since_ms):
        """本场直播（received_at >= since_ms）的按房间统计；用于区分开播前后。"""
        rid = _as_int(room_id)
        since = _as_int(since_ms)
        if not rid or since <= 0:
            return None
        with self.lock:
            dm = self.db.execute(
                "SELECT COUNT(*) FROM danmaku "
                "WHERE room_id=? AND received_at>=?", (rid, since)).fetchone()[0]
            sc = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(price),0) FROM sc_message "
                "WHERE room_id=? AND received_at>=? AND deleted=0",
                (rid, since)).fetchone()
            ev = self.db.execute(
                "SELECT COUNT(*) FROM room_event "
                "WHERE room_id=? AND received_at>=? AND kind<>'live'",
                (rid, since)).fetchone()[0]
        return {"danmaku": dm, "sc": sc[0], "events": ev,
                "sc_amount": sc[1], "since": since}

    def range_stats(self, room_id, since_ms, until_ms=0):
        """区间统计（左闭右开）[since, until)；until=0 表示至今。含去重观众数。"""
        rid = _as_int(room_id)
        since = _as_int(since_ms)
        until = _as_int(until_ms)
        if not rid or since <= 0:
            return None
        with self.lock:
            dm = self.db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT uid) FROM danmaku "
                "WHERE room_id=? AND received_at>=?" +
                (" AND received_at<?" if until else ""),
                ((rid, since, until) if until else (rid, since))).fetchone()
            sc = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(price),0), COUNT(DISTINCT uid) "
                "FROM sc_message WHERE room_id=? AND received_at>=? AND deleted=0" +
                (" AND received_at<?" if until else ""),
                ((rid, since, until) if until else (rid, since))).fetchone()
            ev = self.db.execute(
                "SELECT COUNT(*) FROM room_event "
                "WHERE room_id=? AND received_at>=? AND kind<>'live'" +
                (" AND received_at<?" if until else ""),
                ((rid, since, until) if until else (rid, since))).fetchone()[0]
            # 观众数：弹幕与 SC 发送者 uid 的并集（SQL 去重）
            extra = " AND received_at<?" if until else ""
            vargs = (rid, since, until) if until else (rid, since)
            viewers = self.db.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT uid FROM danmaku WHERE uid>0 AND room_id=? "
                "AND received_at>=?" + extra + " UNION "
                "SELECT uid FROM sc_message WHERE uid>0 AND room_id=? "
                "AND received_at>=?" + extra + ")",
                vargs + vargs).fetchone()[0]
        return {"danmaku": dm[0], "sc": sc[0], "events": ev,
                "sc_amount": sc[1], "viewers": viewers,
                "interactions": dm[0] + sc[0] + ev,
                "since": since, "until": until}

    def live_segments(self, room_id):
        """把一条直播间的数据切成「场」与「场间」，带每段统计。

        - 场：开播 → 下播（右开区间，下播事件本身算在这一场里，用 ``end+1ms``
          的边界表达）。没有下播记录（强杀 / 只开到一半）时，一直算到下一次
          开播或至今，避免尾巴数据落在两段之间的缝里。
        - 场间：上一场下播之后 → 这一场开播，也就是「上一次直播结束到这次
          开播前」收到的弹幕；最后一场之后到至今的场间同样会给出（只要有数据）。
        每段都返回时间边界与弹幕数 / SC 流水 / 去重观众数，前端列表直接展示。

        一次开播记录都没有时（服务是开播中途才启动的、或旧版本数据），不去猜
        批次：所有采集到的内容归成一个「未分段」，等下一次开播才切开。
        """
        rid = _as_int(room_id)
        if not rid:
            return []
        with self.lock:
            marks = [(r[0], r[1]) for r in self.db.execute(
                "SELECT received_at, detail FROM room_event "
                "WHERE room_id=? AND kind='live' "
                "ORDER BY received_at ASC, seq ASC", (rid,))]
        if not marks:
            return self._segment_unsegmented(rid)

        shows = []          # [(start, end)]，end=0 表示还没下播
        open_at = 0
        for ts, detail in marks:
            if detail == "开播":
                if open_at and ts > open_at:
                    shows.append((open_at, ts))
                open_at = ts
            elif detail == "下播" and open_at:
                # +1ms：所有区间都是左闭右开 [start, end)，让下播事件本身
                # 落在这一场而不是随后的场间里。
                shows.append((open_at, max(ts, open_at) + 1))
                open_at = 0
        if open_at:
            shows.append((open_at, 0))
        if not shows:
            return self._segment_unsegmented(rid)

        segs = []           # [(kind, start, end, lead)]
        last_end = 0
        # 第一次开播之前采集到的数据（服务中途启动）单独给一段，避免只能从
        # 「全部数据」里翻——那段时间同样属于「上一次直播结束之后」。
        first_ts = self.first_data_at(rid)
        if first_ts and first_ts < shows[0][0]:
            segs.append(("gap", first_ts, shows[0][0], True))
        for start, end in shows:
            # 上一场下播到这一场开播之间就是场间
            if last_end and start > last_end:
                segs.append(("gap", last_end, start, False))
            segs.append(("live", start, end, False))
            if end:
                last_end = end
        if last_end and shows[-1][1]:
            segs.append(("gap", last_end, 0, False))   # 下播之后到现在（还在收弹幕）

        out, seq_live, seq_gap = [], 0, 0
        for kind, start, end, lead in segs:
            stat = self.range_stats(rid, start, end) or {}
            if kind == "live":
                seq_live += 1
                index = seq_live
            else:
                seq_gap += 1
                index = seq_gap
            # 只保留确实有数据的场间，避免空行噪声
            if kind == "gap" and not stat.get("interactions"):
                continue
            out.append({"kind": kind, "index": index, "start": start,
                        "end": end, "approx": False, "lead": bool(lead),
                        "danmaku": stat.get("danmaku", 0),
                        "sc": stat.get("sc", 0),
                        "sc_amount": stat.get("sc_amount", 0),
                        "viewers": stat.get("viewers", 0),
                        "interactions": stat.get("interactions", 0)})
        return out

    def first_data_at(self, rid):
        """这条采集器最早一条数据的 received_at（不含开播/下播事件本身）。"""
        with self.lock:
            return self.db.execute(
                "SELECT MIN(t) FROM (SELECT MIN(received_at) AS t FROM danmaku "
                "WHERE room_id=? UNION ALL SELECT MIN(received_at) FROM sc_message "
                "WHERE room_id=? UNION ALL SELECT MIN(received_at) FROM room_event "
                "WHERE room_id=? AND kind<>'live')", (rid, rid, rid)).fetchone()[0]

    def last_live_mark(self, rid):
        """最近一条开播 / 下播记录：(received_at, detail)，没有则 None。"""
        with self.lock:
            row = self.db.execute(
                "SELECT received_at, detail FROM room_event "
                "WHERE room_id=? AND kind='live' "
                "ORDER BY received_at DESC, seq DESC LIMIT 1", (rid,)).fetchone()
        return (row[0], row[1]) if row else None

    def delete_session(self, room_id, start, end, with_marks=False):
        """删掉一个场次（或场间）时间段内的全部数据，不可恢复。

        - ``end=0`` 表示这段还没有结束（进行中的场 / 最后一段场间），一直删到
          最新；``end`` 毫秒以内含端点（下播那一条标记恰好等于 ``end``）。
        - ``with_marks=True`` 连同该时间段的「开播 / 下播」标记一起删，删整场
          用；场间不能动标记，否则相邻两场会被并到一起。
        返回各类删除条数，供前端提示。
        """
        rid = _as_int(room_id)
        start = _as_int(start)
        end = _as_int(end)
        if not rid or start <= 0:
            raise ValueError("room_id/start 必填")
        if end and end < start:
            raise ValueError("end 不能早于 start")
        out = {"danmaku": 0, "sc": 0, "events": 0}
        with self.lock:
            for table, key in (("danmaku", "danmaku"), ("sc_message", "sc")):
                sql = "DELETE FROM %s WHERE room_id=? AND received_at>=?" % table
                args = [rid, start]
                if end:
                    sql += " AND received_at<=?"
                    args.append(end)
                out[key] = max(0, self.db.execute(sql, args).rowcount)
            # 场间：内容全删（礼物 / 进入等事件都算内容），但开播/下播标记保留；
            # 整场：room_event 连标记一起删。
            sql = ("DELETE FROM room_event WHERE room_id=? AND received_at>=?"
                   + (" AND received_at<=?" if end else ""))
            if not with_marks:
                sql += " AND kind<>'live'"
            args = [rid, start] + ([end] if end else [])
            out["events"] = max(0, self.db.execute(sql, args).rowcount)
            self.db.commit()
        return out

    def _segment_unsegmented(self, rid):
        """一次开播都还没记录：所有采集到的内容归为一段，等下次开播再切开。

        不按采集批次去猜——批次是服务重启的时间，不是直播的边界。这一段通常
        是「首次开播之前」，也可能是服务在开播中途才启动的那一场。
        """
        start = self.first_data_at(rid)
        stat = self.range_stats(rid, start, 0) if start else None
        if not start or not stat or not stat.get("interactions"):
            return []
        return [{"kind": "gap", "index": 1, "start": start, "end": 0,
                 "approx": True, "lead": True, "single": True,
                 "danmaku": stat.get("danmaku", 0),
                 "sc": stat.get("sc", 0),
                 "sc_amount": stat.get("sc_amount", 0),
                 "viewers": stat.get("viewers", 0),
                 "interactions": stat.get("interactions", 0)}]

    # ---------------- dynamics ----------------

    def upsert_dynamics(self, rows):
        """Insert or refresh dynamics; translation is never overwritten."""
        if not rows:
            return 0
        sql = """INSERT INTO dynamics
            (dyn_id, mid, pub_ts, dyn_type, text, pics, jump_url, raw_json, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(dyn_id) DO UPDATE SET
              mid=excluded.mid, pub_ts=excluded.pub_ts, dyn_type=excluded.dyn_type,
              text=excluded.text, pics=excluded.pics, jump_url=excluded.jump_url,
              raw_json=excluded.raw_json, fetched_at=excluded.fetched_at"""
        data = [(str(r.get("id") or r.get("dyn_id") or ""),
                 _as_int(r.get("mid")), _as_int(r.get("pub_ts")),
                 str(r.get("type") or ""), str(r.get("text") or ""),
                 json.dumps(r.get("pics") or [], ensure_ascii=False),
                 str(r.get("jump_url") or ""),
                 json.dumps(r.get("raw_json") or r, ensure_ascii=False)[:8000],
                 int(time.time() * 1000))
                for r in rows if (r.get("id") or r.get("dyn_id"))]
        with self.lock:
            self.db.executemany(sql, data)
            self.db.commit()
        return len(data)

    def last_dyn_fetch(self, mid=0):
        """某位 up 主最近一次拉到动态的时间（老数据没有 dyn_last_ts 时兜底）。"""
        sql = "SELECT COALESCE(MAX(fetched_at),0) FROM dynamics"
        args = []
        if _as_int(mid) > 0:
            sql += " WHERE mid=?"
            args.append(_as_int(mid))
        with self.lock:
            return self.db.execute(sql, args).fetchone()[0] or 0

    def list_dynamics(self, limit=200, offset=0, q="", mid=0):
        """按发布时间倒序取一页动态；返回 (总数, 这一页的条目)。"""
        limit = max(1, min(1000, _as_int(limit, 200)))
        offset = max(0, _as_int(offset))
        clauses, args = [], []
        if _as_int(mid) > 0:
            clauses.append("mid=?")
            args.append(_as_int(mid))
        if q:
            clauses.append("(text LIKE ? OR text_trans LIKE ?)")
            args.extend(["%" + q + "%", "%" + q + "%"])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM dynamics" + where +
                " ORDER BY pub_ts DESC, dyn_id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
            total = self.db.execute(
                "SELECT COUNT(*) FROM dynamics" + where, args).fetchone()[0]
        items = []
        for r in rows:
            d = dict(r)
            try:
                d["pics"] = json.loads(d.get("pics") or "[]")
            except (ValueError, TypeError):
                d["pics"] = []
            items.append(d)
        return total, items

    def update_dyn_translation(self, dyn_id, text):
        cur = self._write(
            "UPDATE dynamics SET text_trans=? WHERE dyn_id=?",
            (str(text or ""), str(dyn_id)))
        return cur.rowcount > 0

    def delete_dynamic(self, dyn_id):
        cur = self._write("DELETE FROM dynamics WHERE dyn_id=?", (str(dyn_id),))
        return cur.rowcount > 0

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
            totals["dynamics"] = self.db.execute(
                "SELECT COUNT(*) FROM dynamics").fetchone()[0]

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

            # 以直播间为单位的聚合：开播次数、去重观众数、互动总量、场次流水峰值
            for item in by_room.values():
                rid = item["room_id"]
                live_row = self.db.execute(
                    "SELECT COUNT(*), COALESCE(MAX(received_at),0) FROM room_event "
                    "WHERE room_id=? AND kind='live' AND detail='开播'",
                    (rid,)).fetchone()
                item["sessions"] = live_row[0]
                item["last_live_at"] = live_row[1]
                # 没有开播事件就是「还没记录过开播」，不拿采集批次冒充场次
                item["sessions_approx"] = False
                item["viewers"] = self.db.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT uid FROM danmaku WHERE uid>0 AND room_id=? UNION "
                    "SELECT uid FROM sc_message WHERE uid>0 AND room_id=?)",
                    (rid, rid)).fetchone()[0]
                item["interactions"] = (item["danmaku"] + item["sc"] +
                                        item["events"])
                item["sc_peak"] = self.db.execute(
                    "SELECT COALESCE(MAX(price),0) FROM sc_message "
                    "WHERE room_id=? AND deleted=0", (rid,)).fetchone()[0]

            top_sc = [dict(r) for r in self.db.execute(
                "SELECT room_id, uname, price, message, message_trans, received_at "
                "FROM sc_message WHERE deleted=0 ORDER BY price DESC, received_at DESC "
                "LIMIT 5")]

            # 动态按 up 主 uid 聚合：条数 + 最近一次拉到的时间
            dyn_by_mid = {}
            for row in self.db.execute(
                    "SELECT mid, COUNT(*), COALESCE(MAX(fetched_at),0) "
                    "FROM dynamics GROUP BY mid"):
                dyn_by_mid[row[0]] = {"n": row[1], "last_at": row[2] or 0}

            recent = []  # 最近动态不再在首页展示（接口保留字段兼容）

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
                "top_sc": top_sc, "recent": recent, "dyn_by_mid": dyn_by_mid,
                "timeline": {"labels": labels, **timeline},
                "last_received_at": max(
                    [x["last_at"] for x in by_room.values()] + [0])}


def _safe_config(config):
    """Persist run settings without any credential."""
    keep = ("room_id", "protover", "heartbeat_seconds", "record_danmaku",
            "record_sc", "record_events")
    return json.dumps({k: config.get(k) for k in keep}, ensure_ascii=False)
