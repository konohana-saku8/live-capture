# -*- coding: utf-8 -*-
"""把旧采集库（translate_history.db）里的弹幕 / SC / 礼物 / 上舰导入本服务。

旧库是 LiveTranslate 那套采集器写的：表名与字段大体相同，但没有 run_id、
没有表情图片字段，也没有开播 / 下播事件。导入时：

- 每条写一个 import_key（内容哈希），重复导入不会写重；
- 按「相邻两条数据间隔超过 --gap 分钟」推出一场直播的起止，补写一条
  「开播」「下播」事件，这样时间轴能切出场 / 场间，而不是所有数据糊成一段；
- 旧库里出现过的直播间会补进采集器列表（能联网就顺手取 up 主 uid / 头像）。

用法：
    python import_history.py --src ../live-archive/.data/translation_history.db
    python import_history.py --src x.db --data data --gap 30 --no-sessions
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

import rooms as rooms_mod
from store import Store

SOURCES = ("danmaku", "sc_message", "room_event")


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _key(*parts):
    raw = "|".join("" if p is None else str(p) for p in parts)
    return "th:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _table(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return bool(row)


def _columns(conn, name):
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % name)}


def _fmt(ms):
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000.0))


def _clusters(times, gap_ms):
    """把一串升序时间戳按「间隔 > gap」切成若干段，返回 [(start, end)]。"""
    out, cur = [], None
    for ts in times:
        if cur is None or ts - cur[1] > gap_ms:
            if cur:
                out.append((cur[0], cur[1]))
            cur = [ts, ts]
        else:
            cur[1] = ts
    if cur:
        out.append((cur[0], cur[1]))
    return out


def import_room(conn, store, rid, gap_ms, sessions=True, verbose=True,
                min_ms=5 * 60 * 1000):
    out = {"room_id": rid, "danmaku": 0, "danmaku_seen": 0, "sc": 0,
           "events": 0, "sessions": 0, "skipped": 0}

    # ---------- 弹幕 ----------
    sql = ("SELECT seq, uid, uname, text, event_time, received_at, medal_name,"
           " medal_level, guard_level, is_admin, is_emoticon FROM danmaku"
           " WHERE room_id=? ORDER BY received_at ASC, seq ASC")
    cur = conn.execute(sql, (rid,))
    batch, batch_count = [], 0
    while True:
        rows = cur.fetchmany(4000)
        if not rows:
            break
        for r in rows:
            out["danmaku_seen"] += 1
            batch.append({
                "room_id": rid, "uid": r[1], "uname": r[2] or "",
                "text": r[3] or "", "event_time": r[4],
                "received_at": r[5], "medal_name": r[6] or "",
                "medal_level": r[7] or 0, "guard_level": r[8] or 0,
                "is_admin": r[9] or 0, "is_emoticon": r[10] or 0,
                "import_key": _key("dm", rid, r[1], r[5], r[3]),
            })
        batch_count += store.insert_danmaku(batch)
        batch = []
    out["danmaku"] = batch_count

    # ---------- SC ----------
    if _table(conn, "sc_message"):
        has_trans = "message_trans" in _columns(conn, "sc_message")
        sql = ("SELECT sc_id, uid, uname, price, message, start_time, end_time,"
               " received_at, guard_level, medal_name, medal_level, raw_json"
               + (", message_trans" if has_trans else "") +
               " FROM sc_message WHERE room_id=? ORDER BY received_at ASC")
        for r in conn.execute(sql, (rid,)):
            row = {"sc_id": r[0], "room_id": rid, "uid": r[1], "uname": r[2] or "",
                   "price": r[3] or 0, "message": r[4] or "",
                   "message_trans": (r[12] if has_trans else "") or "",
                   "start_time": r[5] or 0, "end_time": r[6] or 0,
                   "received_at": r[7] or 0, "guard_level": r[8] or 0,
                   "medal_name": r[9] or "", "medal_level": r[10] or 0,
                   "deleted": 0, "raw_json": r[11] or ""}
            if store.insert_sc(row):
                out["sc"] += 1

    # ---------- 礼物 / 上舰 ----------
    if _table(conn, "room_event"):
        ev_rows = []
        sql = ("SELECT event_type, event_key, uid, uname, gift_name, gift_num,"
               " price, coin_type, event_time, received_at, raw_json"
               " FROM room_event WHERE room_id=? ORDER BY received_at ASC")
        for r in conn.execute(sql, (rid,)):
            kind = r[0] or "event"
            num = r[5] or 1
            if kind == "guard":
                level = 0
                try:
                    level = _as_int(json.loads(r[10] or "{}").get("guard_level"))
                except ValueError:
                    level = 0
                detail = "舰长 %s 级 ×%s" % (level or "?", num)
            elif kind == "gift":
                detail = "%s ×%s" % (r[4] or "礼物", num)
            else:
                detail = "%s ×%s" % (r[4] or kind, num)
            ev_rows.append({
                "room_id": rid, "kind": kind, "uid": r[2], "uname": r[3] or "",
                "detail": detail, "event_time": r[8] or 0,
                "received_at": r[9] or 0, "raw_json": r[10] or "",
                "import_key": _key("ev", rid, r[0], r[1]),
            })
        out["events"] = store.insert_events(ev_rows)

    # ---------- 补开播 / 下播：按数据空档推场次 ----------
    if sessions:
        times = []
        for table in ("danmaku", "sc_message", "room_event"):
            if not _table(conn, table):
                continue
            col = "received_at"
            extra = ""
            if table == "room_event":
                extra = " AND event_type NOT IN ('live')"
            times.extend(r[0] for r in conn.execute(
                "SELECT %s FROM %s WHERE room_id=?%s ORDER BY 1" %
                (col, table, extra), (rid,)) if r[0])
        marks = []
        kept = []
        for start, end in _clusters(sorted(set(times)), gap_ms):
            # 太短的一段不算一场：多半是零星几条路过弹幕，切出来只会多出
            # 一串一分钟的假场次，让它们并进前后的「场间」更实在。
            if end - start < min_ms:
                out["skipped"] += 1
                continue
            kept.append((start, end))
            # 开播落在第一条数据之前一点点，确保这条数据算进这一场
            marks.append({"room_id": rid, "kind": "live", "detail": "开播",
                          "event_time": start // 1000,
                          "received_at": max(0, start - 1000),
                          "raw_json": json.dumps({"imported": True,
                                                  "approx": True},
                                                 ensure_ascii=False),
                          "import_key": _key("live", rid, start, "open")})
            marks.append({"room_id": rid, "kind": "live", "detail": "下播",
                          "event_time": end // 1000,
                          "received_at": end + 1000,
                          "raw_json": json.dumps({"imported": True,
                                                  "approx": True},
                                                 ensure_ascii=False),
                          "import_key": _key("live", rid, end, "close")})
        out["sessions"] = store.insert_events(marks)
        out["clusters"] = kept
    if verbose:
        print("  房间 %s：弹幕 %d（读到 %d）· SC %d · 事件 %d · 场次标记 %d"
              "（跳过 %d 段过短的）" %
              (rid, out["danmaku"], out["danmaku_seen"], out["sc"],
               out["events"], out["sessions"], out["skipped"]))
    return out


def _room_info(conn, rid):
    """旧库里的房间标题 / 主播昵称（有就带上）。"""
    brief = {}
    if _table(conn, "collect_room"):
        row = conn.execute(
            "SELECT title, uname FROM collect_room WHERE room_id=?",
            (rid,)).fetchone()
        if row:
            brief = {"title": row[0] or "", "uname": row[1] or ""}
    return brief


def _enrich(brief, rid, timeout=6):
    """联网补 uid / 头像；失败就算了，别把导入卡住。"""
    try:
        import capture
        info = capture.fetch_room_brief(rid, timeout=timeout)
    except Exception as exc:                  # 离线 / 接口抽风都不影响导入
        print("    （联网取直播间信息失败，先只存标题：%s）" % exc)
        return brief
    out = dict(brief)
    for key in ("uid", "short_id", "live_status", "live_time", "online",
                "uname", "face", "title"):
        value = info.get(key)
        if value:
            out[key] = value
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="导入旧采集库的弹幕 / SC / 事件")
    parser.add_argument("--src", required=True, help="旧库路径（translate_history.db）")
    parser.add_argument("--data", default="data", help="本服务数据目录（含 capture.db）")
    parser.add_argument("--gap", type=int, default=30,
                        help="推场次用的空档分钟数，默认 30")
    parser.add_argument("--min-minutes", type=int, default=5,
                        help="短于这个分钟数的数据段不算一场（并进前后场间），默认 5")
    parser.add_argument("--no-sessions", action="store_true",
                        help="不补开播 / 下播事件（数据会归成「未分段」）")
    parser.add_argument("--no-rooms", action="store_true",
                        help="不往采集器列表里补房间")
    parser.add_argument("--offline", action="store_true",
                        help="不联网取 uid / 头像")
    parser.add_argument("--report", default="", help="把结果另存一份到这个文件")
    args = parser.parse_args(argv)

    if not os.path.exists(args.src):
        print("找不到旧库：%s" % args.src)
        return 2
    data_dir = os.path.abspath(args.data)
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "capture.db")
    print("旧库：%s" % os.path.abspath(args.src))
    print("目标：%s" % db_path)

    conn = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(args.src).replace("\\", "/"),
                           uri=True)
    store = Store(db_path)
    room_store = rooms_mod.RoomStore(os.path.join(data_dir, "rooms.json"))
    known = {x["id"] for x in room_store.list()["items"]}

    rids = set()
    for table in SOURCES:
        if _table(conn, table):
            rids.update(r[0] for r in conn.execute(
                "SELECT DISTINCT room_id FROM %s" % table) if r[0])
    if _table(conn, "collect_room"):
        rids.update(r[0] for r in conn.execute("SELECT room_id FROM collect_room")
                    if r[0])

    lines, totals = [], {"danmaku": 0, "sc": 0, "events": 0, "sessions": 0,
                         "rooms": 0}
    gap_ms = max(1, args.gap) * 60 * 1000
    min_ms = max(0, args.min_minutes) * 60 * 1000
    for rid in sorted(rids):
        print("→ 房间 %s" % rid)
        res = import_room(conn, store, rid, gap_ms,
                          sessions=not args.no_sessions, min_ms=min_ms)
        for key in ("danmaku", "sc", "events", "sessions"):
            totals[key] += res[key]
        for start, end in res.get("clusters") or []:
            lines.append("session %s %s - %s" % (rid, _fmt(start), _fmt(end)))
        brief = _room_info(conn, rid)
        if not args.no_rooms and rid not in known:
            if not args.offline:
                brief = _enrich(brief, rid)
            item, created = room_store.add(rid, "", brief)
            if created:
                totals["rooms"] += 1
                print("  已加入采集器：%s" % item["label"])
                lines.append("room %s %s" % (rid, item["label"]))
    conn.close()

    summary = ("导入完成：弹幕 +%d · SC +%d · 事件 +%d · 场次标记 +%d · 新采集器 +%d"
               % (totals["danmaku"], totals["sc"], totals["events"],
                  totals["sessions"], totals["rooms"]))
    print(summary)
    if args.report:
        try:
            with open(args.report, "w", encoding="utf-8") as handle:
                handle.write(summary + "\n" + "\n".join(lines) + "\n")
        except OSError as exc:
            print("报告写入失败：%s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
