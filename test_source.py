# -*- coding: utf-8 -*-
"""把「本地视频 + 一场已有直播」做成 uid=0 的测试源采集器。

测试源不连接 B 站，也不参与自动采集；它只是把一段本地 MP4 挂成录像，
并把 source DB 里某一场的弹幕 / SC / 事件按视频时长缩放后复制过来，
用来在「直播数据」页验证播放、消息同步、滚动加载和场次管理。

用法（在 host 上，Windows 路径照写）：

    .venv-host/Scripts/python.exe test_source.py \
        --video "D:\\Repository\\ai-corver\\MSST-GUI-1.4.0\\input\\96570601-1-208.mp4" \
        --source-room 22748536 --source-index 1

参数：
- ``--data``        目标数据目录（默认 .state/uitest，与正式库隔离；
                    要写进正式 data/ 必须显式传 --data data）
- ``--video``       本地视频路径（必填）
- ``--source-db``   复用数据的 DB（默认正式库 data/capture.db）
- ``--source-room`` 源房间号（省略时取源库里有开播记录的第一个房间）
- ``--source-index`` 第几场（从 1 开始；0 或负数 = 最后一场）
- ``--room-id``     测试源的合成房间号（默认 999999999）
- ``--uid``         测试源 uid（默认 0）
- ``--cleanup``     只删掉测试源（配置 + 该房间数据 + 录像目录），不造数据
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import struct
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from capture import event_row          # noqa: E402
from rooms import RoomStore            # noqa: E402
from store import Store                # noqa: E402

TEST_ROOM_ID = 999999999
# 测试源默认落在 .state/uitest，正式 data/ 里不该有它：服务只认 data/，
# 写进去就会被当成真采集器（连 B 站探测、进推送、进自动采集）。
TEST_DATA_DIR = os.path.join(ROOT, ".state", "uitest")
PROD_DATA_DIR = os.path.join(ROOT, "data")


def cleanup(data_dir, room_id):
    """删掉测试源：配置条目、该房间的弹幕/SC/事件/录像记录、录像目录。"""
    data_dir = os.path.abspath(data_dir)
    rid = int(room_id)
    removed = []

    rooms_path = os.path.join(data_dir, "rooms.json")
    if os.path.isfile(rooms_path):
        if RoomStore(rooms_path).remove(rid):
            removed.append("rooms.json")

    db_path = os.path.join(data_dir, "capture.db")
    if os.path.isfile(db_path):
        store = Store(db_path)
        store.clear(rid)
        with store.lock:
            store.db.execute("DELETE FROM recordings WHERE room_id=?", (rid,))
            store.db.execute("DELETE FROM runs WHERE room_id=?", (rid,))
            store.db.commit()
        removed.append("capture.db")

    rec_dir = os.path.join(data_dir, "recordings", str(rid))
    if os.path.isdir(rec_dir):
        shutil.rmtree(rec_dir, ignore_errors=True)
        removed.append("recordings/%s" % rid)
    return removed


def _mvhd_duration_ms(moov):
    index = 0
    while index + 8 <= len(moov):
        size, kind = struct.unpack_from(">I4s", moov, index)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", moov, index + 8)[0]
            header = 16
        if size < header:
            break
        if kind == b"mvhd":
            body = moov[index + header:index + size]
            if not body:
                return 0
            if body[0] == 1:
                timescale = struct.unpack_from(">I", body, 20)[0]
                duration = struct.unpack_from(">Q", body, 24)[0]
            else:
                timescale = struct.unpack_from(">I", body, 12)[0]
                duration = struct.unpack_from(">I", body, 16)[0]
            return int(duration * 1000 / timescale) if timescale else 0
        index += size
    return 0


def mp4_duration_ms(path):
    """纯标准库读 mvhd；moov 在文件头或文件尾都能找到。"""
    total = os.path.getsize(path)
    try:
        with open(path, "rb") as handle:
            while handle.tell() + 8 <= total:
                start = handle.tell()
                head = handle.read(8)
                if len(head) < 8:
                    break
                size, kind = struct.unpack(">I4s", head)
                header = 8
                if size == 1:
                    ext = handle.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                    header = 16
                elif size == 0:
                    size = total - start
                if size < header:
                    break
                if kind == b"moov":
                    return _mvhd_duration_ms(handle.read(size - header))
                handle.seek(start + size)
    except (OSError, struct.error):
        return 0
    return 0


def _read_source(db_path, room_id, index):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not room_id:
            row = conn.execute(
                "SELECT room_id FROM room_event WHERE kind='live' "
                "ORDER BY received_at ASC LIMIT 1").fetchone()
            if row:
                room_id = int(row[0])
        if not room_id:
            raise SystemExit("源库里没有直播房间；请用 --source-room 指定")

        marks = [(int(r[0]), str(r[1])) for r in conn.execute(
            "SELECT received_at, detail FROM room_event "
            "WHERE room_id=? AND kind='live' "
            "ORDER BY received_at ASC, seq ASC", (room_id,))]
        shows, opened = [], 0
        for ts, detail in marks:
            if detail == "开播":
                if opened and ts > opened:
                    shows.append((opened, ts))
                opened = ts
            elif detail == "下播" and opened:
                shows.append((opened, max(ts, opened) + 1))
                opened = 0
        if opened:
            shows.append((opened, 0))

        if shows:
            seg = shows[min(index, len(shows)) - 1] if index > 0 else shows[-1]
            start, end = seg
            if not end:
                row = conn.execute(
                    "SELECT MAX(t) FROM ("
                    "SELECT received_at AS t FROM danmaku WHERE room_id=? "
                    "UNION ALL SELECT received_at FROM sc_message WHERE room_id=? "
                    "UNION ALL SELECT received_at FROM room_event "
                    "WHERE room_id=? AND kind<>'live')",
                    (room_id, room_id, room_id)).fetchone()
                end = int(row[0] or start) + 1
        else:
            row = conn.execute(
                "SELECT MIN(t), MAX(t) FROM ("
                "SELECT received_at AS t FROM danmaku WHERE room_id=? "
                "UNION ALL SELECT received_at FROM sc_message WHERE room_id=? "
                "UNION ALL SELECT received_at FROM room_event "
                "WHERE room_id=? AND kind<>'live')",
                (room_id, room_id, room_id)).fetchone()
            if not row or row[0] is None:
                raise SystemExit("源房间没有任何可复用的数据")
            start, end = int(row[0]), int(row[1]) + 1

        danmaku = [dict(r) for r in conn.execute(
            "SELECT * FROM danmaku WHERE room_id=? AND received_at>=? "
            "AND received_at<? ORDER BY received_at ASC, seq ASC",
            (room_id, start, end))]
        scs = [dict(r) for r in conn.execute(
            "SELECT * FROM sc_message WHERE room_id=? AND received_at>=? "
            "AND received_at<? ORDER BY received_at ASC",
            (room_id, start, end))]
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM room_event WHERE room_id=? AND received_at>=? "
            "AND received_at<? AND kind<>'live' ORDER BY received_at ASC, seq ASC",
            (room_id, start, end))]
        return room_id, start, end, danmaku, scs, events
    finally:
        conn.close()


def _link_or_copy(src, dest):
    """优先硬链接，其次符号链接，最后复制。

    Android/Termux 的 os 模块没有 os.link；Windows 的 os.symlink 可能需要
    开发者模式，所以两种链接都要能优雅退化。
    """
    for name, func in (("hardlink", getattr(os, "link", None)),
                       ("symlink", getattr(os, "symlink", None))):
        if func is None:
            continue
        try:
            func(src, dest)
            return name
        except (OSError, AttributeError):
            try:
                os.unlink(dest)
            except OSError:
                pass
    shutil.copy2(src, dest)
    return "copy"


def build(args):
    data_dir = os.path.abspath(args.data)
    video = os.path.abspath(args.video)
    if not os.path.isfile(video):
        raise SystemExit("视频不存在：%s" % video)
    os.makedirs(data_dir, exist_ok=True)

    # 复用数据的来源默认是正式库；测试源自己写到 --data（默认 .state/uitest）
    source_db = os.path.abspath(args.source_db or
                               os.path.join(PROD_DATA_DIR, "capture.db"))
    if not os.path.isfile(source_db):
        raise SystemExit("源库不存在：%s" % source_db)
    if source_db == os.path.join(data_dir, "capture.db") and \
            int(args.room_id) == int(args.source_room or 0):
        raise SystemExit("源房间不能与目标测试源房间相同")

    src_room, src_start, src_end, danmaku, scs, events = _read_source(
        source_db, int(args.source_room or 0), int(args.source_index or 0))
    if not danmaku and not scs and not events:
        raise SystemExit("选中的源场次没有数据")
    src_span = max(1, src_end - src_start)

    duration = mp4_duration_ms(video) or min(src_span, 3600 * 1000)
    duration = max(1000, duration)
    now = int(time.time() * 1000)
    test_start = now - duration - 5 * 60 * 1000
    test_end = test_start + duration - 1
    scale = duration / src_span

    def mapped(ts):
        return test_start + max(
            0, min(duration - 1, int((int(ts) - src_start) * scale)))

    # 目标库 / 采集器
    store = Store(os.path.join(data_dir, "capture.db"))
    room_id = int(args.room_id)
    if room_id == src_room and source_db == os.path.join(data_dir, "capture.db"):
        raise SystemExit("不能用同一个房间号原地复用")

    config_path = os.path.join(data_dir, "config.json")
    if not os.path.exists(config_path):
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"auto_start": False}, handle, ensure_ascii=False,
                      indent=2)

    rooms = RoomStore(os.path.join(data_dir, "rooms.json"))
    items, _ = rooms.add(room_id, args.label, {
        "uid": int(args.uid), "uname": args.label,
        "title": "本地视频 · 复用直播数据",
        "live_status": 0, "online": 0, "live_time": 0})
    rooms.settings(room_id, {
        "collect_live": False, "collect_dynamics": False,
        "live_only": False, "auto_record": False})

    # 清掉上一次生成的测试数据（只清测试源房间）
    store.clear(room_id)
    with store.lock:
        store.db.execute("DELETE FROM recordings WHERE room_id=?", (room_id,))
        store.db.execute("DELETE FROM runs WHERE room_id=?", (room_id,))
        store.db.commit()
    rec_dir = os.path.join(data_dir, "recordings", str(room_id))
    shutil.rmtree(rec_dir, ignore_errors=True)
    os.makedirs(rec_dir, exist_ok=True)

    dest = os.path.join(rec_dir, os.path.basename(video))
    mode = _link_or_copy(video, dest)
    size = os.path.getsize(dest)
    rel = os.path.relpath(dest, data_dir).replace(os.sep, "/")

    for row in danmaku:
        row.update({"room_id": room_id, "run_id": None, "import_key": None})
        row["received_at"] = mapped(row.get("received_at") or src_start)
        row["event_time"] = row["received_at"] // 1000
    store.insert_danmaku(danmaku)

    for row in scs:
        stamp = mapped(row.get("received_at") or src_start)
        row.update({
            "room_id": room_id, "run_id": None,
            "sc_id": "test-%s-%s" % (room_id, row.get("sc_id")),
            "received_at": stamp, "start_time": stamp // 1000,
            "end_time": stamp // 1000,
        })
        store.insert_sc(row)

    event_rows = []
    for row in events:
        stamp = mapped(row.get("received_at") or src_start)
        event_rows.append({
            "room_id": room_id, "kind": row.get("kind"),
            "uid": row.get("uid"), "uname": row.get("uname"),
            "detail": row.get("detail"), "event_time": stamp // 1000,
            "received_at": stamp, "raw_json": row.get("raw_json"),
            "import_key": None,
        })
    store.insert_events(event_rows)

    # 测试场次：开播 → 下播，和视频时长严格对齐
    store.insert_event(event_row(
        room_id, "live", {"live": 1, "start_time": test_start // 1000},
        test_start))
    store.insert_event(event_row(
        room_id, "live", {"live": 0, "start_time": test_end // 1000},
        test_end))

    rec_id = store.create_recording(
        room_id, "%s · %s" % (args.label, os.path.basename(video)),
        live_started_at=test_start, requested_qn=10000, actual_qn=10000,
        started_at=test_start)
    store.update_recording(
        rec_id, stopped_at=test_start + duration, status="ready", bytes=size,
        duration_ms=duration, error="", mp4_parts=[{
            "path": rel, "started_at": test_start, "bytes": size,
            "duration_ms": duration,
        }])

    return {
        "ok": True, "data": data_dir, "room_id": room_id,
        "uid": int(args.uid), "label": items.get("label"),
        "video": video, "video_mode": mode, "video_bytes": size,
        "duration_ms": duration,
        "source": {"db": source_db, "room_id": src_room,
                   "start": src_start, "end": src_end},
        "test_segment": {"start": test_start, "end": test_end + 1},
        "copied": {"danmaku": len(danmaku), "sc": len(scs),
                   "events": len(event_rows)},
        "recording_id": rec_id,
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        description="生成本地视频 + 复用直播数据的 uid=0 测试源")
    parser.add_argument("--data", default=TEST_DATA_DIR)
    parser.add_argument("--video", default=None)
    parser.add_argument("--source-db", default=None)
    parser.add_argument("--source-room", type=int, default=0)
    parser.add_argument("--source-index", type=int, default=0,
                        help="第几场（从 1 开始；0/负数 = 最后一场）")
    parser.add_argument("--room-id", type=int, default=TEST_ROOM_ID)
    parser.add_argument("--uid", type=int, default=0)
    parser.add_argument("--label", default="测试源")
    parser.add_argument("--cleanup", action="store_true",
                        help="只删掉测试源（配置 + 数据 + 录像目录）")
    args = parser.parse_args()
    if args.cleanup:
        print(json.dumps({"removed": cleanup(args.data, args.room_id)},
                         ensure_ascii=False, indent=2))
        return
    if not args.video:
        parser.error("--video 必填（或用 --cleanup 只删测试源）")
    print(json.dumps(build(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
