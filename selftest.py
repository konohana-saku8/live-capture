# -*- coding: utf-8 -*-
"""Offline self-check: protocol, multi-room capture, storage, REST API, web page.

    python selftest.py

Never connects to Bilibili. Uses a temporary data dir, a loopback server and a
fake websocket. Room info lookup is patched out as well.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

import capture
import accounts as accounts_mod
import dynamics as dynamics_mod
import server
import sysinfo
from accounts import AccountStore, parse_cookie_csv
from capture import CaptureManager
from images import EmoteProxy, FALLBACK_PNG, ImageError, normalize_image_url
from rooms import RoomStore
from server import Config, LogBuffer, create_server
from store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
PASSED = []


def check(name, condition, extra=""):
    if not condition:
        raise AssertionError("%s %s" % (name, extra))
    PASSED.append(name)
    print("PASS  %s" % name, flush=True)


def main():
    # ---------- protocol ----------
    auth = list(capture.iter_packets(capture.pack_auth(123, "tok", 0, 2)))[0]
    body = json.loads(auth.body)
    check("认证包 op=7", auth.operation == capture.OP_AUTH)
    check("认证字段", body == {"uid": 0, "roomid": 123, "protover": 2,
                              "platform": "web", "type": 2, "key": "tok"}, body)
    hb = list(capture.iter_packets(capture.pack_heartbeat()))[0]
    check("心跳体", hb.operation == 2 and hb.body == b"[object Object]")
    payload = {"cmd": "DANMU_MSG:4:0:2:2:2:0", "info": [["x"], "hi"]}
    inner = capture.pack_packet(capture.OP_MESSAGE,
                                json.dumps(payload).encode("utf-8"))
    outer = capture.pack_packet(capture.OP_MESSAGE, zlib.compress(inner),
                                header_ver=capture.PROTO_ZLIB)
    msgs = list(capture.load_messages(list(capture.iter_packets(outer))[0]))
    check("zlib 嵌套解包", len(msgs) == 1 and msgs[0]["cmd"].startswith("DANMU_MSG"))
    plain = capture.pack_packet(capture.OP_MESSAGE, json.dumps(payload).encode())
    check("明文解包", list(capture.load_messages(
        list(capture.iter_packets(plain))[0]))[0]["cmd"] == payload["cmd"])
    pop = capture.Packet(3, 1, b"\x00\x00\x01\x00")
    check("人气值解析", capture.parse_popularity(pop) == 256)

    # ---------- 头像昵称：公开 card 接口（mock urlopen，零联网） ----------
    old_urlopen = urllib.request.urlopen

    class _CardResp:
        def __init__(self, obj): self._obj = obj
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self._obj).encode("utf-8")

    seen_cards = []
    def card_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen_cards.append(url)
        if "web-interface/card" in url:
            return _CardResp({"code": 0, "data": {"card": {
                "mid": "2094031249", "name": "火羽ひのめ",
                "face": "https://i1.hdslb.com/bfs/face/a.jpg"}}})
        return _CardResp({"code": -404, "data": {}})
    urllib.request.urlopen = card_urlopen
    try:
        ident = capture.fetch_identity(2094031249)
        check("按 uid 从 card 接口取头像昵称",
              ident["uname"] == "火羽ひのめ" and
              ident["face"] == "https://i1.hdslb.com/bfs/face/a.jpg", ident)
        check("card 接口调用地址正确",
              any("web-interface/card?mid=2094031249" in u for u in seen_cards), seen_cards)
        prof = accounts_mod.fetch_profile({"SESSDATA": "x", "DedeUserID": "2094031249"})
        check("账号 profile 用 uid 走 card 接口",
              prof and prof["uid"] == 2094031249 and prof["uname"] == "火羽ひのめ", prof)
    finally:
        urllib.request.urlopen = old_urlopen

    # ---------- sysinfo（纯函数，读不到 /proc 也应有形状） ----------
    snap = sysinfo.snapshot(tempfile.gettempdir(), app_started_at=1)
    check("设备快照结构完整",
          all(k in snap for k in ("cpu", "memory", "disk", "load", "network",
                                  "uptime", "db_size")) and
          isinstance(snap["cpu"]["percent"], float) and
          0 <= snap["cpu"]["percent"] <= 100)
    check("设备快照百分比在合法范围",
          0 <= snap["memory"]["percent"] <= 100 and
          0 <= snap["disk"]["percent"] <= 100)
    check("设备网络字段存在",
          "lan_ip" in snap["network"] and "online" in snap["network"])

    # ---------- dynamics: WBI 签名 + 解析（纯函数，零联网） ----------
    # 已知固定 wts 时，签名结果稳定；这里只验证结构与一致性
    q1 = dynamics_mod.wbi_sign({"host_mid": 1}, "imgKey123", "subKey456", ts=1700000000)
    q2 = dynamics_mod.wbi_sign({"host_mid": 1}, "imgKey123", "subKey456", ts=1700000000)
    check("WBI 签名稳定且带 wts/w_rid",
          q1 == q2 and "wts=1700000000" in q1 and "w_rid=" in q1 and
          "host_mid=1" in q1, q1)
    check("WBI mixin key 长度 32",
          len(dynamics_mod.mixin_key("a" * 32, "b" * 32)) == 32)
    check("WBI 签名随 key 变化",
          dynamics_mod.wbi_sign({"a": 1}, "x", "y", ts=1) !=
          dynamics_mod.wbi_sign({"a": 1}, "x", "z", ts=1))

    # 解析 opus 结构（真实接口的形状）
    frame = {
        "id_str": "999", "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"mid": 9, "pub_ts": 1794672601, "name": "主播"},
            "module_dynamic": {
                "major": {"type": "MAJOR_TYPE_OPUS", "opus": {
                    "summary": {"text": "今日も配信する！"},
                    "pics": [{"url": "https://i0.hdslb.com/p1.jpg"}],
                    "jump_url": "//www.bilibili.com/opus/999"}},
                "desc": None},
        },
    }
    norm = dynamics_mod.normalize(frame, 9)
    check("动态解析：id/时间/正文/图片/链接",
          norm["id"] == "999" and norm["pub_ts"] == 1794672601 and
          norm["text"] == "今日も配信する！" and
          norm["pics"] == ["https://i0.hdslb.com/p1.jpg"] and
          norm["jump_url"].endswith("/opus/999"), norm)
    # 无 major 时退回 desc 文本
    frame2 = {"id_str": "888", "type": "DYNAMIC_TYPE_WORD",
              "modules": {"module_author": {"mid": 9, "pub_ts": 1},
                          "module_dynamic": {"desc": {"text": "纯文字动态"},
                                             "major": None}}}
    check("动态解析：纯文字回退 desc",
          dynamics_mod.normalize(frame2, 9)["text"] == "纯文字动态")

    # ---------- cookie CSV ----------
    netscape = "\n".join([
        "# Netscape HTTP Cookie File",
        "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1893456000\tSESSDATA\tsecret-token",
        ".bilibili.com\tTRUE\t/\tFALSE\t1893456000\tDedeUserID\t31373462",
        ".bilibili.com\tTRUE\t/\tFALSE\t1893456000\tbuvid3\tBUVID-XYZ",
    ])
    c1 = parse_cookie_csv(netscape)
    check("Netscape cookie 解析",
          c1.get("SESSDATA") == "secret-token" and
          c1.get("buvid3") == "BUVID-XYZ", c1)
    c2 = parse_cookie_csv(
        "SESSDATA\tchrome-token\t.bilibili.com\t/\t1893456000\t0\t0\t0\t0\t0\t0\t0\t0\n"
        "DedeUserID\t42\t.bilibili.com\t/\t1893456000\n")
    check("Chrome 13 列 cookie 解析",
          c2.get("SESSDATA") == "chrome-token" and c2.get("DedeUserID") == "42", c2)
    c3 = parse_cookie_csv("name,value\nSESSDATA,csv-token\nbuvid3,AAA\n")
    check("逗号两列 cookie 解析", c3.get("SESSDATA") == "csv-token", c3)
    try:
        parse_cookie_csv("")
        check("空 CSV 被拒", False)
    except ValueError:
        check("空 CSV 被拒", True)

    # ---------- field mapping + emoticon ----------
    now = 1794672000000
    sc = capture.extract_sc(22748536, {
        "id": 991, "uid": 42, "price": 30, "message": "こんにちは",
        "start_time": 1794672000, "end_time": 1794672060,
        "user_info": {"uname": "观众A", "guard_level": 2},
        "medal_info": {"medal_name": "牌子", "medal_level": 12},
        "message_trans": "",
    }, now)
    check("SC 字段映射", sc and sc["sc_id"] == "991" and
          sc["price"] == 30 and sc["message"] == "こんにちは" and
          sc["uname"] == "观众A", sc)

    head = [0] * 16
    head[4] = 1794672000000
    head[13] = {"url": "http://i0.hdslb.com/bfs/emote/big.png", "height": 162,
                "emots": {"[大哭]": {"url": "https://i0.hdslb.com/bfs/emote/cry.png",
                                     "height": 20}}}
    head[15] = {"user": {"base": {"name": "小明"}, "uid": 123,
                         "medal": {"name": "牌子", "level": 5}}}
    dm = capture.build_danmaku(22748536, [head, "[大哭] 你好", [123, "小明", 0]], now)
    check("弹幕字段映射", dm and dm["uid"] == 123 and dm["uname"] == "小明" and
          dm["text"] == "[大哭] 你好", dm)
    check("表情包解析（正文占位）",
          dm["emote_map"].get("大哭") ==
          ["https://i0.hdslb.com/bfs/emote/cry.png", 20], dm["emote_map"])
    check("整条表情升级 https 并按原图定高",
          dm["emote_url"] == "https://i0.hdslb.com/bfs/emote/big.png" and
          dm["emote_px"] == 40 and dm["is_emoticon"] == 1, dm)
    check("缺字段不崩", capture.build_danmaku(1, None, now) is None)
    ev = capture.event_row(22748536, "gift", {"uid": 7, "uname": "B",
                                              "gift": {"giftName": "辣条", "num": 3}}, now)
    check("事件摘要", "辣条" in ev["detail"], ev["detail"])

    # ---------- rooms store ----------
    tmp = tempfile.mkdtemp(prefix="lc-selftest-")
    try:
        room_path = os.path.join(tmp, "rooms.json")
        rooms = RoomStore(room_path)
        r1, created = rooms.add(22748536, "", {"title": "深夜杂谈", "uid": 9,
                                               "live_status": 1, "online": 123,
                                               "live_time": 1794600000})
        check("添加直播间并自动取标题", created and r1["label"] == "深夜杂谈" and
              r1["live_status"] == 1, r1)
        r2, created2 = rooms.add(1001, "备用房间")
        check("添加第二个直播间", created2 and r2["label"] == "备用房间")
        _, created3 = rooms.add(22748536, "改名")
        check("同名房间号重复添加为更新", not created3 and
              rooms.get(22748536)["label"] == "改名")
        check("采集器默认同时采直播间与动态",
              r1["collect_live"] is True and r1["collect_dynamics"] is True, r1)
        check("按 uid 找采集器", (rooms.find_by_uid(9) or {}).get("id") == 22748536)
        rooms.settings(22748536, {"collect_dynamics": False})
        check("采集器开关可单独关闭",
              rooms.get(22748536)["collect_dynamics"] is False and
              rooms.get(22748536)["collect_live"] is True)
        check("采集动态的目标列表随开关变化",
              [x["id"] for x in rooms.collect_targets("collect_dynamics")]
              == [1001])
        rooms.settings(22748536, {"collect_dynamics": True})
        check("直播间持久化", len(RoomStore(room_path).list()["items"]) == 2)
        check("刷新直播间信息", rooms.refresh(1001, {"live_status": 2})["live_status"] == 2)
        check("删除直播间", rooms.remove(1001) is True and
              len(rooms.list()["items"]) == 1)
        rooms.add(1001, "备用房间")

        # ---------- storage ----------
        st = Store(os.path.join(tmp, "capture.db"))
        run_id = st.start_run(22748536, {"room_id": 22748536, "protover": 2})
        dm["run_id"] = run_id
        dm["raw_json"] = "{}"
        check("弹幕入库（含表情字段）", st.insert_danmaku([dm]) == 1)
        total, rows = st.query("danmaku", limit=5)
        check("表情字段可回读并可 JSON 解析",
              total == 1 and json.loads(rows[0]["emote_map"])["大哭"][0].endswith("cry.png") and
              rows[0]["emote_px"] == 40, rows)
        sc["run_id"] = run_id
        check("SC 入库", st.insert_sc(sc) is True)
        check("SC 按 id 去重", st.insert_sc(sc) is False)
        check("译文回填", st.update_sc_translation("991", "你好") is True)
        ev["run_id"] = run_id
        st.insert_event(ev)
        check("事件计数", st.counts()["events"] == 1)
        # 第二个房间的事件：按房间区分
        ev2 = capture.event_row(1001, "guard", {"uid": 8, "uname": "C",
                                                "guard_level": 3}, now + 1000)
        ev2["run_id"] = run_id
        st.insert_event(ev2)
        t1, rows1 = st.query("events", room_id=22748536)
        t2, rows2 = st.query("events", room_id=1001)
        check("事件按直播间区分", t1 == 1 and t2 == 1 and
              rows1[0]["room_id"] == 22748536 and rows2[0]["room_id"] == 1001,
              (t1, t2))
        dash = st.dashboard()
        check("仪表盘总数与 SC 金额", dash["totals"]["danmaku"] == 1 and
              dash["totals"]["sc"] == 1 and dash["totals"]["events"] == 2 and
              dash["totals"]["sc_amount"] == 30, dash["totals"])
        check("仪表盘按房间拆分", len(dash["by_room"]) == 2)
        check("仪表盘高额 SC / 时间轴（最近动态已移除）",
              len(dash["top_sc"]) == 1 and dash["recent"] == [] and
              len(dash["timeline"]["labels"]) == 12)
        check("按房间聚合开播次数/观众/互动",
              all(k in dash["by_room"][0] for k in
                  ("sessions", "viewers", "interactions", "sc_peak")))
        # ---------- 直播场次分段 ----------
        # 这段额外插入 2 条开播事件 + 3 条弹幕（主库，供后续 HTTP 接口验证）
        sst = st
        base = now + 2000
        t2ms = now + 5000
        for st_ms in (base, t2ms):
            row = capture.event_row(22748536, "live", {"live": 1}, st_ms)
            row["event_time"] = st_ms // 1000
            sst.insert_event(row)
        for uid, uname, text, ts in (
                (501, "一期A", "第一场-1", base + 400),
                (502, "一期B", "第一场-2", base + 900),
                (601, "二期A", "第二场-1", t2ms + 300)):
            sst.insert_danmaku([{"room_id": 22748536, "uid": uid, "uname": uname,
                                 "text": text, "received_at": ts,
                                 "is_emoticon": 0}])
        sess = sst.live_segments(22748536)
        check("按开播事件切分场次", len(sess) == 2 and
              sess[0]["start"] == base and sess[1]["start"] == t2ms, sess)
        check("首场以下一场开播为终点", sess[0]["end"] == t2ms)
        check("最后一场至今（end=0）", sess[1]["end"] == 0)
        check("每场弹幕数正确", sess[0]["danmaku"] == 2 and
              sess[1]["danmaku"] == 1, sess)
        check("场次去重观众数", sess[0]["viewers"] == 2 and
              sess[1]["viewers"] == 1, sess)
        check("没有下播事件时不产生场间段",
              [s["kind"] for s in sess] == ["live", "live"], sess)
        rng = sst.range_stats(22748536, base, t2ms)
        check("区间统计含互动总数", rng["danmaku"] == 2 and
              rng["interactions"] >= 2, rng)
        allrows = sst.query_all("danmaku", room_id=22748536)
        # 主库此前已有 1 条 storage 段弹幕 + 本段 3 条 = 4
        check("query_all 按时间升序返回", len(allrows) == 4 and
              allrows[-1]["received_at"] >= allrows[0]["received_at"])
        page = sst.query_page_before("danmaku", t2ms + 1000, count=2,
                                     room_id=22748536)
        check("向前翻页取更早数据", len(page) == 2 and
              all(x["received_at"] < t2ms + 1000 for x in page))

        # 老数据无 LIVE 事件 → 按采集批次（runs）兜底分段
        rstore = Store(os.path.join(tmp, "runs-only.db"))
        rr = rstore.start_run(22748536, {"room_id": 22748536})
        run_start = rstore.db.execute(
            "SELECT started_at FROM runs WHERE id=?", (rr,)).fetchone()[0]
        rstore.insert_danmaku([{"room_id": 22748536, "uid": 9, "uname": "老观众",
                                "text": "老数据", "received_at": run_start + 500,
                                "is_emoticon": 0}])
        rstore.end_run(rr)
        # 第二个批次：把 started_at 往后挪 1 小时，避免与上一批重叠
        rr2 = rstore.start_run(22748536, {"room_id": 22748536})
        base2 = run_start + 3600 * 1000
        rstore.db.execute("UPDATE runs SET started_at=? WHERE id=?", (base2, rr2))
        rstore.db.commit()
        rstore.insert_danmaku([{"room_id": 22748536, "uid": 10, "uname": "新观众",
                                "text": "新数据", "received_at": base2 + 500,
                                "is_emoticon": 0}])
        rstore.end_run(rr2)
        rsess = rstore.live_segments(22748536)
        check("无开播事件时按采集批次兜底分段", len(rsess) == 2 and
              rsess[0].get("approx") is True and
              rsess[0]["danmaku"] == 1 and rsess[1]["danmaku"] == 1, rsess)

        # ---------- 场 / 场间 交替分段（独立库） ----------
        gst = Store(os.path.join(tmp, "segments.db"))
        gt0 = now - 3 * 3600 * 1000
        hour = 3600 * 1000

        def live_ev(detail, ts):
            row = capture.event_row(22748536, "live",
                                    {"live": 1 if detail == "开播" else 0}, ts)
            row["event_time"] = ts // 1000
            gst.insert_event(row)

        live_ev("开播", gt0)
        live_ev("下播", gt0 + hour)
        live_ev("开播", gt0 + 2 * hour)
        for uid, text, ts, amount in (
                (701, "场内1", gt0 + 600, 0),
                (702, "场内2", gt0 + 900, 30),
                (703, "场间1", gt0 + hour + 300, 0),
                (704, "场内3", gt0 + 2 * hour + 600, 0)):
            gst.insert_danmaku([{"room_id": 22748536, "uid": uid, "uname": text,
                                 "text": text, "received_at": ts,
                                 "is_emoticon": 0}])
            if amount:
                gst.insert_sc({"sc_id": "g%d" % uid, "room_id": 22748536,
                               "uid": uid, "uname": text, "price": amount,
                               "message": text, "received_at": ts})
        gsegs = gst.live_segments(22748536)
        check("场 / 场间 交替分段",
              [s["kind"] for s in gsegs] == ["live", "gap", "live"], gsegs)
        check("场 = 开播到下播", gsegs[0]["end"] == gt0 + hour and
              gsegs[0]["danmaku"] == 2 and gsegs[0]["sc_amount"] == 30)
        check("场间 = 上一次下播到这次开播",
              gsegs[1]["start"] == gt0 + hour and
              gsegs[1]["end"] == gt0 + 2 * hour and gsegs[1]["danmaku"] == 1)
        check("最后一场至今且带观众数",
              gsegs[2]["end"] == 0 and gsegs[2]["viewers"] == 1)
        check("每段都有流水/观众/弹幕字段",
              all(k in gsegs[0] for k in ("sc_amount", "viewers", "danmaku")),
              gsegs[0])
        # 下播之后再收到的弹幕归到尾部场间
        gst.insert_danmaku([{"room_id": 22748536, "uid": 705, "uname": "下播后",
                             "text": "下播后还在聊", "received_at": now + 90000,
                             "is_emoticon": 0}])
        live_ev("下播", now + 80000)
        gsegs = gst.live_segments(22748536)
        check("下播后的弹幕进入尾部场间",
              gsegs[-1]["kind"] == "gap" and gsegs[-1]["danmaku"] == 1, gsegs[-1])

        # ---------- 动态存储（独立库） ----------
        dyn_rows = [
            {"id": "d1", "mid": 9, "pub_ts": now // 1000, "type": "DYNAMIC_TYPE_DRAW",
             "text": "今日も配信する", "pics": ["https://i0.hdslb.com/a.png"],
             "jump_url": "//www.bilibili.com/opus/d1"},
            {"id": "d2", "mid": 9, "pub_ts": now // 1000 - 86400,
             "type": "DYNAMIC_TYPE_DRAW", "text": "昨日の話", "pics": [],
             "jump_url": ""},
        ]
        check("动态入库", sst.upsert_dynamics(dyn_rows) == 2)
        dtotal, ditems = sst.list_dynamics()
        check("动态按时间倒序", dtotal == 2 and ditems[0]["dyn_id"] == "d1")
        check("动态图片 JSON 回读", ditems[0]["pics"] ==
              ["https://i0.hdslb.com/a.png"])
        check("动态译文保存", sst.update_dyn_translation("d1", "今天也直播") is True)
        dtotal, ditems = sst.list_dynamics()
        check("译文可回读且重拉不覆盖", ditems[0]["text_trans"] == "今天也直播" and
              sst.upsert_dynamics(dyn_rows) == 2 and
              sst.list_dynamics()[1][0]["text_trans"] == "今天也直播")
        check("动态搜索命中译文", sst.list_dynamics(q="今天")[0] == 1)
        check("动态可按 up 主 uid 过滤",
              sst.list_dynamics(mid=9)[0] == 2 and
              sst.list_dynamics(mid=12345)[0] == 0)
        check("动态删除", sst.delete_dynamic("d2") is True and
              sst.list_dynamics()[0] == 1)

        name, ctype, csv_body = st.export("danmaku", "csv", room_id=22748536)
        check("CSV 导出带 BOM 且含表情地址",
              csv_body.startswith("\ufeff") and "emote_url" in csv_body and
              "big.png" in csv_body)
        st.end_run(run_id)

        # 旧库迁移：没有表情列也能打开并补列
        old_db = os.path.join(tmp, "old.db")
        conn = sqlite3.connect(old_db)
        conn.execute("CREATE TABLE danmaku (seq INTEGER PRIMARY KEY, room_id INTEGER, "
                     "text TEXT, received_at INTEGER)")
        conn.execute("INSERT INTO danmaku(room_id,text,received_at) VALUES (1,'旧',1)")
        conn.commit()
        conn.close()
        old = Store(old_db)
        cols = {r[1] for r in old.db.execute("PRAGMA table_info(danmaku)")}
        check("旧库自动补表情列", {"emote_url", "emote_px", "emote_map"} <= cols)
        check("旧库旧数据保留", old.query("danmaku")[1][0]["text"] == "旧")

        # ---------- 表情图片代理（纯离线，不联网） ----------
        good = ("https://i0.hdslb.com/bfs/garb/item/"
                "dce31c5d95b632f6a0bd0d4e5159bdc532fb31b4.png")
        check("CDN 地址白名单放行",
              normalize_image_url(good) == good and
              normalize_image_url("//i0.hdslb.com/bfs/emote/a.png") ==
              "https://i0.hdslb.com/bfs/emote/a.png" and
              normalize_image_url("http://static.hdslb.com/x.png@100w_100h_1c.webp")
              is not None)
        bad_urls = ["https://example.com/a.png", "file:///etc/passwd",
                    "http://127.0.0.1:8765/a.png",
                    "https://i0.hdslb.com.evil.com/a.png",
                    "https://user@i0.hdslb.com/a.png",
                    "https://i0.hdslb.com:8080/a.png",
                    "javascript:alert(1)", ""]
        check("拒绝非 CDN / 内网 / 伪装域名地址",
              all(normalize_image_url(u) is None for u in bad_urls), bad_urls)
        check("占位图是合法 PNG",
              FALLBACK_PNG.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND" in FALLBACK_PNG)

        class FakeResp:
            def __init__(self, body, ctype="image/png"):
                self.body = body
                self.headers = {"Content-Type": ctype}

            def read(self, size=-1):
                return self.body[:size] if size and size > 0 else self.body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def req_headers(req):
            return {k.lower(): v for k, v in req.header_items()}

        calls = []

        def fake_opener(req, timeout=None):
            calls.append(req_headers(req))
            return FakeResp(b"PNGDATA")

        proxy = EmoteProxy(opener=fake_opener)
        ctype, body = proxy.get(good)
        check("代理取图并带官方 Referer",
              ctype == "image/png" and body == b"PNGDATA" and
              calls[0].get("referer") == "https://live.bilibili.com/", calls[0])
        proxy.get(good)
        check("同一图片命中缓存不再联网",
              len(calls) == 1 and proxy.stats()["bytes"] == len(b"PNGDATA"),
              proxy.stats())

        def reject_first(req, timeout=None):
            if req_headers(req).get("referer"):
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
            return FakeResp(b"OK2")

        check("CDN 返回 403 时去掉 Referer 重试",
              EmoteProxy(opener=reject_first).get(good)[1] == b"OK2")
        for label, resp in [
            ("非图片响应被拒", FakeResp(b"<html>", "text/html")),
            ("SVG 被拒（同源可执行脚本）", FakeResp(b"<svg/>", "image/svg+xml")),
        ]:
            p = EmoteProxy(opener=lambda req, timeout=None, r=resp: r)
            try:
                p.get(good)
                check(label, False)
            except ImageError:
                check(label, True)
        try:
            EmoteProxy(opener=fake_opener).get("https://example.com/a.png")
            check("代理拒绝非法地址", False)
        except ImageError:
            check("代理拒绝非法地址", True)

        # ---------- HTTP API ----------
        # 账号昵称/头像走真实 B 站接口，自测里换成假实现保持零联网
        old_profile = accounts_mod.fetch_profile
        accounts_mod.fetch_profile = lambda cookies, timeout=6: {
            "uid": 31373462, "uname": "测试用户",
            "face": "https://i0.hdslb.com/bfs/face/test.png"}
        cfg = Config(os.path.join(tmp, "config.json"))
        logs = LogBuffer()
        logs.add("info", "selftest")
        manager = CaptureManager(st, cfg.get,
                                 lambda level, msg: logs.add(level, msg),
                                 rooms=rooms)
        old_brief = server.fetch_room_brief
        server.fetch_room_brief = lambda rid, timeout=3: {
            "title": "房间 %s" % rid, "uid": 1, "short_id": 0,
            "live_status": 1, "live_time": 1794600000, "online": 10,
            "uname": "主播%s" % rid, "face": "https://i0.hdslb.com/bfs/face/%s.png" % rid}
        old_by_uid = server.fetch_room_by_uid
        server.fetch_room_by_uid = lambda uid, timeout=4: dict(
            server.fetch_room_brief(7788, timeout), room_id=7788, uid=uid)
        srv = create_server("127.0.0.1", 0, tmp, os.path.join(HERE, "web"),
                            st, manager, cfg, logs, rooms=rooms)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%s" % srv.server_address[1]

        def get(path, headers=None):
            req = urllib.request.Request(base + path, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as err:
                return err.code, err.read()

        def post(path, obj, headers=None):
            hdr = {"Content-Type": "application/json"}
            hdr.update(headers or {})
            req = urllib.request.Request(base + path,
                                         data=json.dumps(obj).encode("utf-8"),
                                         headers=hdr, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as err:
                return err.code, err.read()

        status, html = get("/")
        check("网页可访问", status == 200 and "直播采集" in html.decode("utf-8", "replace"))
        check("健康检查", json.loads(get("/api/health")[1])["ok"])
        gate = json.loads(get("/api/gate")[1])
        check("门禁探测接口公开且初始无需密码", gate.get("ok") and gate.get("need") is False, gate)

        # 图片代理走 HTTP 路由（mock opener，全程零联网）
        png = b"PNGDATA-HTTP"
        srv.images.opener = lambda req, timeout=None: FakeResp(png)
        status, raw = get("/api/emote?u=" + urllib.parse.quote(good, safe=""))
        check("同源表情代理返回图片", status == 200 and raw == png)
        status, raw = get("/api/emote?u=" +
                          urllib.parse.quote("https://example.com/a.png", safe=""))
        check("非白名单地址返回 502 占位图",
              status == 502 and raw == FALLBACK_PNG)
        check("缺参数不会 500", get("/api/emote")[0] == 502)
        status, raw = get("/api/rooms")
        j = json.loads(raw)
        check("直播间列表含两间且都未采集",
              status == 200 and len(j["items"]) == 2 and j["active"] == 0, j)
        check("采集器列表带按房间统计",
              any(x["totals"]["events"] == 1 for x in j["items"]))
        check("采集器列表带 up 主信息与采集开关",
              all(k in j["items"][0] for k in
                  ("face", "uname", "uid", "live_time", "params", "live_stats",
                   "collect_live", "collect_dynamics", "dyns")))
        check("开播中的房间有本场统计",
              j["items"][0]["live_stats"] is not None and
              j["items"][0]["live_stats"]["since"] == 1794600000000,
              j["items"][0].get("live_stats"))
        status, raw = get("/api/dashboard")
        j = json.loads(raw)
        check("仪表盘接口", status == 200 and j["totals"]["sc"] == 1 and
              "timeline" in j and "top_sc" in j)
        status, raw = get("/api/system")
        js = json.loads(raw)
        check("设备状态接口：CPU/内存/存储/网络",
              status == 200 and all(k in js["system"] for k in
                                    ("cpu", "memory", "disk", "network", "load")))
        status, raw = get("/api/sessions?room_id=22748536")
        jj = json.loads(raw)
        check("场次接口返回场 / 场间分段",
              status == 200 and jj["total"] == 2 and
              jj["segments"][0]["kind"] == "live" and
              jj["segments"][0]["danmaku"] == 2, jj.get("segments"))
        status, raw = get("/api/feed?room_id=22748536&types=danmaku&all=1")
        jall = json.loads(raw)
        check("全量接口不截断（all=1）",
              status == 200 and jall["all"] is True and
              len(jall["items"]["danmaku"]) == 4, len(jall["items"]["danmaku"]))
        status, raw = get("/api/feed?room_id=22748536&types=danmaku&all=1&since=%d&until=%d"
                          % (now + 2000, now + 5000))
        jseg = json.loads(raw)
        check("按场次区间过滤弹幕",
              len(jseg["items"]["danmaku"]) == 2, len(jseg["items"]["danmaku"]))
        status, raw = get("/api/dynamics")
        jd = json.loads(raw)
        check("动态接口返回列表与定时配置",
              status == 200 and "items" in jd and "status" in jd and
              "dyn_hour" in jd["config"] and "dyn_minute" in jd["config"])
        status, raw = post("/api/dynamics/trans", {"dyn_id": "d1", "text": "接口译文"})
        check("动态翻译写入接口", status == 200 and
              json.loads(raw)["ok"] is True)
        status, raw = post("/api/dynamics/config",
                           {"dyn_auto": True, "dyn_hour": 9, "dyn_minute": 25})
        check("动态定时精确到分钟",
              status == 200 and
              json.loads(raw)["config"]["dyn_hour"] == 9 and
              json.loads(raw)["config"]["dyn_minute"] == 25 and
              cfg.get()["dyn_auto"] is True and cfg.get()["dyn_minute"] == 25)
        status, raw = get("/api/dynamics?mid=9")
        check("动态接口按 up 主 uid 过滤",
              status == 200 and json.loads(raw)["total"] == 1)
        status, raw = get("/api/feed?room_id=22748536&types=danmaku,sc")
        j = json.loads(raw)
        check("内容接口按房间返回分栏",
              len(j["items"]["danmaku"]) >= 1 and len(j["items"]["sc"]) == 1 and
              "events" not in j["items"], j["items"].keys())
        status, raw = get("/api/feed?room_id=1001&types=events")
        check("另一个房间的事件单独可见",
              len(json.loads(raw)["items"]["events"]) == 1)
        status, raw = get("/api/feed?room_id=1001&types=sc")
        check("另一个房间没有 SC", json.loads(raw)["items"]["sc"] == [])
        status, raw = get("/api/config")
        check("配置不再含房间号与手填凭证",
              all(k not in json.loads(raw)["config"]
                  for k in ("room_id", "sessdata", "dede_user_id", "buvid3")))
        status, raw = post("/api/config", {"protover": 3})
        check("保存配置", status == 200 and cfg.get()["protover"] == 3)
        post("/api/config", {"protover": 2})
        status, raw = post("/api/rooms", {"uid": 12345})
        ju = json.loads(raw)
        check("按 up 主 uid 添加采集器（自动解析房间号）",
              status == 200 and ju["room"]["id"] == 7788 and
              ju["room"]["uid"] == 12345 and len(ju["rooms"]["items"]) == 3, ju)
        post("/api/rooms/remove", {"room_id": 7788})
        check("缺 uid 时添加被拒",
              post("/api/rooms", {"room_id": 0})[0] == 400)
        status, raw = post("/api/rooms", {"room_id": 3003, "label": "第三间"})
        check("网页添加直播间（不联网）",
              status == 200 and len(json.loads(raw)["rooms"]["items"]) == 3)
        status, raw = post("/api/rooms/refresh", {})
        check("刷新直播间信息接口", json.loads(raw)["updated"] == 3)
        status, raw = post("/api/rooms/settings",
                           {"room_id": 22748536,
                            "params": {"collect_dynamics": False}})
        j = json.loads(raw)
        check("采集器开关接口：关掉动态采集",
              status == 200 and j["room"]["collect_dynamics"] is False and
              j["room"]["collect_live"] is True, j.get("room"))
        post("/api/rooms/settings", {"room_id": 22748536,
                                     "params": {"collect_dynamics": True}})
        status, raw = post("/api/rooms/settings", {"room_id": 22748536,
                                                    "params": {"protover": 3,
                                                               "heartbeat_seconds": 45}})
        j = json.loads(raw)
        check("按房间保存采集参数",
              status == 200 and j["room"]["params"]["protover"] == 3 and
              j["room"]["params"]["heartbeat_seconds"] == 45, j.get("room"))
        check("房间参数持久化",
              RoomStore(room_path).get(22748536)["params"]["protover"] == 3)
        check("参数接口对不存在房间 404",
              post("/api/rooms/settings", {"room_id": 999, "params": {}})[0] == 404)
        check("参数越界被拒",
              post("/api/rooms/settings", {"room_id": 22748536,
                                           "params": {"protover": 9}})[0] == 400)
        status, raw = post("/api/rooms/remove", {"room_id": 3003})
        check("网页删除直播间", len(json.loads(raw)["rooms"]["items"]) == 2)
        status, raw = get("/api/logs?limit=500")
        check("日志接口", status == 200 and len(json.loads(raw)["items"]) >= 1)

        # ---------- 账号 API（CSV 唯一凭证入口） ----------
        csv_text = ("SESSDATA\ttest-sessdata\t.bilibili.com\t/\t1893456000\n"
                    "DedeUserID\t31373462\t.bilibili.com\t/\t1893456000\n")
        j = json.loads(get("/api/accounts")[1])
        check("账号接口初始为空", j["items"] == [] and not j["active_id"], j)
        status, raw = post("/api/accounts", {"name": "main", "csv": csv_text})
        j = json.loads(raw)
        check("上传 CSV 新增账号并自动启用",
              status == 200 and len(j["accounts"]["items"]) == 1 and
              j["accounts"]["items"][0]["active"], j)
        check("账号带昵称头像（header 展示用）",
              (j["accounts"]["items"][0].get("profile") or {}).get("uname")
              == "测试用户", j["accounts"]["items"][0].get("profile"))
        check("上传的 CSV 按 uid 落盘为 <uid>.csv",
              j["accounts"]["items"][0].get("file_name") == "31373462.csv" and
              os.path.isfile(os.path.join(tmp, "accounts", "31373462.csv")),
              j["accounts"]["items"][0].get("file_name"))
        check("账号列表不泄露 cookie 明文",
              b"test-sessdata" not in raw and
              bool(j["accounts"]["items"][0]["sessdata_mask"]), raw[:200])
        aid = j["accounts"]["items"][0]["id"]
        j = json.loads(post("/api/accounts", {"name": "bad",
                                             "csv": "foo,bar\nbuvid3,AAA"})[1])
        check("缺 SESSDATA 仍可保存但标错",
              bool(j["item"]["error"]) and not j["item"]["active"], j["item"])
        check("停用账号", json.loads(post("/api/accounts/activate",
                                         {"id": ""})[1])["accounts"]["active_id"] == "")
        check("启用账号", json.loads(post("/api/accounts/activate",
                                         {"id": aid})[1])["accounts"]["active_id"] == aid)
        check("删除账号", len(json.loads(post("/api/accounts/remove",
                                              {"id": aid})[1])["accounts"]["items"]) == 1)
        acc_path = os.path.join(tmp, "acc-persist.json")
        AccountStore(acc_path).add("persist", csv_text)
        check("账号持久化", AccountStore(acc_path).active()["cookies"]["SESSDATA"] ==
              "test-sessdata")

        post("/api/config", {"token": "abc"})
        check("口令拦截", get("/api/status")[0] == 401)
        check("设置口令后门禁探测报告需要密码",
              json.loads(get("/api/gate")[1]).get("need") is True)
        check("门禁探测接口无需口令即可访问", get("/api/gate")[0] == 200)
        check("图片代理不受口令影响（<img> 无法带请求头）",
              get("/api/emote?u=" + urllib.parse.quote(good, safe=""))[0] == 200)
        check("口令放行", json.loads(get("/api/status",
                                        {"X-Access-Token": "abc"})[1])["ok"])
        post("/api/config", {"token": ""}, {"X-Access-Token": "abc"})

        # ---------- 多房间并发采集（mock websocket，零联网） ----------
        class FakeWS:
            def __init__(self, frames):
                self.frames = list(frames)
                self.sent = []
                self.closed = False

            async def send(self, data):
                self.sent.append(data)

            async def recv(self):
                if self.frames:
                    return self.frames.pop(0)
                await asyncio.sleep(0.3)
                return b""

            async def close(self):
                self.closed = True

        class FakeApi:
            warning = ""
            user_agent = "selftest-ua"
            seen = []

            def __init__(self, cfg):
                FakeApi.seen.append(cfg)

            def cookies(self):
                return {}

            def cookie_header(self):
                return ""

            def resolve_room(self, room):
                return {"real_room_id": room, "title": "测试房间",
                        "uname": "主播", "live_status": 1, "online": 10}

            def get_danmu_info(self, room):
                return "tok", [{"host": "fake", "wss_port": 443}]

            @staticmethod
            def websocket_urls(hosts):
                return ["wss://fake/sub"]

        def frames_for(room_id, tag):
            business = [{"cmd": "LIVE"}]
            business += [{"cmd": "DANMU_MSG",
                          "info": [head, "%s 弹幕%d" % (tag, i), [123, "小明", 0]]}
                         for i in range(51)]
            business.append({"cmd": "SUPER_CHAT_MESSAGE", "data": {
                "id": "m-%s" % room_id, "uid": 9, "price": 50,
                "message": "%s SC" % tag, "start_time": 1794672000,
                "user_info": {"uname": "观众B"}, "medal_info": {}}})
            inner = b"".join(capture.pack_packet(
                capture.OP_MESSAGE, json.dumps(m, ensure_ascii=False).encode("utf-8"))
                for m in business)
            return [capture.pack_packet(capture.OP_AUTH_REPLY, b'{"code":0}'),
                    capture.pack_packet(capture.OP_MESSAGE, zlib.compress(inner),
                                        header_ver=capture.PROTO_ZLIB)]

        frame_pool = [frames_for(22748536, "A"), frames_for(1001, "B")]

        async def fake_connect(url, **kwargs):
            return FakeWS(frame_pool.pop(0) if frame_pool
                          else frames_for(0, "X"))

        manager.accounts.add("main", csv_text)
        old_api, old_connect = capture.BiliApi, capture.connect
        capture.BiliApi, capture.connect = FakeApi, fake_connect
        try:
            ok, message = manager.start(22748536)
            check("房间 A 启动", ok, message)
            ok, message = manager.start(1001)
            check("房间 B 同时启动（多房间并发）", ok, message)
            snap = manager.snapshot()
            check("快照里两间都在采集", snap["active"] == 2 and snap["running"] is True,
                  snap["active"])
            for _ in range(40):
                time.sleep(0.1)
                snap = manager.snapshot()
                if snap["counts"]["danmaku"] >= 100 and snap["counts"]["sc"] >= 2:
                    break
            # FakeApi 在各 worker 线程里异步构造，按 room_id 找对应配置
            cfg_a = next((c for c in FakeApi.seen
                          if c.get("room_id") == 22748536), None)
            cfg_b = next((c for c in FakeApi.seen
                          if c.get("room_id") == 1001), None)
            check("房间 A 使用本房间参数（protover=3）",
                  cfg_a and cfg_a.get("protover") == 3 and
                  cfg_a.get("heartbeat_seconds") == 45, cfg_a)
            check("房间 B 沿用全局默认（protover=2）",
                  cfg_b and cfg_b.get("protover") == 2, cfg_b)
            check("两间弹幕都经采集链路入库",
                  snap["counts"]["danmaku"] >= 100, snap["counts"])
            check("两间 SC 都入库并区分房间",
                  len(st.query("sc", room_id=22748536)[1]) == 2 and
                  len(st.query("sc", room_id=1001)[1]) == 1,
                  (st.query("sc", room_id=22748536)[0],
                   st.query("sc", room_id=1001)[0]))
            states = {s["room_id"]: s for s in snap["rooms"]}
            check("LIVE 消息记录本场开播时间",
                  states[22748536]["live_started_at"] > 0 and
                  states[1001]["live_started_at"] > 0,
                  {k: v.get("live_started_at") for k, v in states.items()})
            _, live_rows = st.query("events", room_id=22748536)
            check("开播事件入库（区分未开播/开播数据）",
                  any(r["kind"] == "live" and "开播" in (r["detail"] or "")
                      for r in live_rows), [r["kind"] for r in live_rows])
            since_ms = int(time.time() * 1000) - 60000
            sess = st.session_stats(22748536, since_ms)
            # 第 51 条弹幕还留在写缓冲里，停止时才冲刷，所以是 >=50
            check("本场统计只数开播后的数据",
                  sess and sess["danmaku"] >= 50 and sess["sc"] >= 1, sess)
            check("since 无效时不给本场统计", st.session_stats(22748536, 0) is None)
            status, raw = get("/api/feed?room_id=1001&types=events&since=%d"
                              % (int(time.time() * 1000) + 3600000))
            check("feed 支持本场过滤（since）",
                  json.loads(raw)["items"]["events"] == [])
            check("启用账号的 cookie 注入采集配置",
                  bool(FakeApi.seen) and
                  FakeApi.seen[-1].get("_cookies", {}).get("SESSDATA") ==
                  "test-sessdata", FakeApi.seen[-1] if FakeApi.seen else {})
            status, raw = post("/api/capture/stop", {"room_id": 22748536})
            j = json.loads(raw)
            check("停止单间后状态立即为未运行",
                  j["ok"] is True and j["capture"]["active"] == 1, j.get("capture", {}).get("active"))
            ok, message = manager.stop_all()
            check("全部停止", ok and manager.snapshot()["active"] == 0, message)
            # 103 = 采集链路产生的弹幕；+3 为场次分段测试预置的弹幕
            expect_dm = 103 + 3
            for _ in range(40):
                if st.counts()["danmaku"] == expect_dm:
                    break
                time.sleep(0.1)
            check("停止时冲刷剩余缓冲", st.counts()["danmaku"] == expect_dm, st.counts())
        finally:
            capture.BiliApi, capture.connect = old_api, old_connect

        check("未知接口 404", get("/api/nope")[0] == 404)
        events_1001 = st.db.execute(
            "SELECT COUNT(*) FROM room_event WHERE room_id=1001").fetchone()[0]
        status, raw = post("/api/clear", {"confirm": "yes", "room_id": 1001})
        # 1001 的事件被清空，22748536 的保留（gift + 采集 LIVE + 场次测试 2 条）
        check("按房间清空数据", status == 200 and events_1001 >= 1 and
              st.db.execute("SELECT COUNT(*) FROM room_event "
                            "WHERE room_id=1001").fetchone()[0] == 0,
              st.counts())
        status, _ = post("/api/clear", {})
        check("清空需确认", status == 400)

        srv.shutdown()
        srv.server_close()

        # ---------- 网页结构 ----------
        with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as fh:
            page = fh.read()
        check("四个页面：仪表盘 / 采集器 / 配置 / 日志（不再有独立动态页）",
              all(k in page for k in ('id="page-home"', 'id="page-rooms"',
                                      'id="page-config"', 'id="page-logs"',
                                      'data-page="home"', 'data-page="rooms"',
                                      "loadDashboard")) and
              'id="page-dynamics"' not in page and
              'data-page="dynamics"' not in page)
        check("采集器页按 up 主 uid 添加并自动带出直播间",
              all(k in page for k in ('id="newUid"', "up 主 uid", "UID ",
                                      "addModal", "btnAddRoom", "/api/rooms")))
        check("采集器页可选采哪些数据（直播间 / 动态）",
              all(k in page for k in ("collect-row", 'id="colLive"', 'id="colDyn"',
                                      "btnSaveCollect", "saveCollect")))
        check("采集器页浏览器标签式：状态点 / ✕ 删除 / ＋ 弹窗添加",
              all(k in page for k in ("roomTabs", "tabAdd", 'data-x="',
                                      "addModal", "btnAddRoom", "/api/rooms",
                                      "/api/capture/", "btnStopAll")))
        check("采集参数全局统一（配置页），采集器页不再有每房间参数卡",
              all(k in page for k in ("f_proto", "f_heartbeat", "f_stale",
                                      "f_dm", "f_sc", "f_ev")) and
              all(k not in page for k in ("roomParamsCard", "btnSaveParams",
                                          "r_proto", "r_hb")))
        check("直播数据按「场 / 场间」分段：左侧列表 + 全量不截断",
              all(k in page for k in ("sessionPane", "sessionList", "loadSessions",
                                      "selectSession", "data-sess=", "/api/sessions",
                                      "all=1", "segRange", "segByKey",
                                      "场间", "sc_amount")) and
              "S.limit" not in page and "id=\"limit\"" not in page)
        check("旧 chips 与独立删除按钮已移除",
              all(k not in page for k in ("roomChips", "btnDelRoom",
                                          "btnAddToggle", "addForm")))
        check("header 账号栏 + 主播基本信息紧凑化",
              all(k in page for k in ("acctChip", "anchorFace", "linkRoom",
                                      "linkSpace", "anchor-meta", "curStats")))
        check("直播间具体数据合并为一个完整块",
              all(k in page for k in ('id="roomPanel"', "room-block",
                                      'id="contentCard"')) and
              "id=\"roomParamsCard\"" not in page)
        check("首页按直播间为单位：开播场次/流水/互动/观众，且无最近动态",
              all(k in page for k in ("开播场次", "累计流水", "互动总数", "观众数",
                                      "metrics")) and
              "最近动态" not in page and "recentList" not in page)
        check("首页设备与服务在最上面：局域网/监听/时长/websockets/数据目录/口令",
              all(k in page for k in ("devGrid", "loadSystem", "renderSystem",
                                      "/api/system", "CPU 占用", "内存占用", "存储占用",
                                      "局域网", "监听", "服务已运行", "最后收包",
                                      "数据目录", "访问口令", "kv-grid wide")) and
              page.index('id="devGrid"') < page.index('id="roomGrid"'))
        check("内容纵向分区 + 只看某类勾选",
              all(k in page for k in ('class="feed"', "data-k=\"danmaku\"",
                                      "data-k=\"sc\"", "data-k=\"events\"",
                                      "lc-filters", "/api/feed")))
        check("无表格、无分页、无手填凭证",
              all(k not in page for k in ("<table", "btnPrev", "btnNext",
                                          "btnViewTable", "f_room", "f_sessdata",
                                          "f_dede", "f_buvid", "pasteCsv")))
        check("表情直接渲染为图片",
              "emoteImg" in page and 'class="emote"' in page and
              "emote_map" in page and "is_emoticon" in page)
        check("表情图不发 Referer 且失败自动走同源代理",
              all(k in page for k in ('referrerpolicy="no-referrer"',
                                      "data-src=", "/api/emote?u=",
                                      "dataset.fallback", "emote-fallback")))
        check("沿用 live-archive 配色与字体变量",
              all(k in page for k in ("--fire:#ff7a45", "--jp:", "--bg:#0a0807",
                                      "--gold:#e8c489")))
        check("保留日志页与 CSV 账号上传",
              all(k in page for k in ("logBox", "acctFile", "btnUpload",
                                      "/api/export", "btnLogs")))
        check("进站门禁锁屏：探测/验证/设置/解锁",
              all(k in page for k in ('class="gated"', 'id="gate"', "bootGate",
                                      "submitGate", "unlockApp", "/api/gate",
                                      "gatePass2", "gateForm", "initApp")))
        check("动态并入采集器页：按天分组 + 译文 + 定时拉取",
              all(k in page for k in ("dynList", "renderDynamics", "loadDynamics",
                                      "fetchDynamics", "dynDayKey", "dyn-trans",
                                      "/api/dynamics", "/api/dynamics/fetch",
                                      "/api/dynamics/trans", "dynAuto", "dynHour",
                                      "dynMinute")) and
              'id="page-dynamics"' not in page)
        check("译文编辑框默认收起（hidden 不被 flex 覆盖）",
              ".dyn-edit[hidden]" in page and "display:none!important" in page)
        check("取消译文编辑后按钮回到可见（dyn-tools 单独隐藏）",
              'class="dyn-actions dyn-tools"' in page and
              ".dyn-tools[hidden]" in page and
              "card.querySelector('.dyn-tools').hidden=true" in page and
              ".querySelector('.dyn-tools').hidden=false" in page)
        check("账号头像点击进配置页（不再跳个人空间）",
              '$(\'acctChip\').onclick' in page and
              "showPage('config')" in page and
              "space.bilibili.com'+uid" not in page)
        check("账号卡显示头像与文件名",
              "acct-face" in page and "file_name" in page)
    finally:
        if "old_profile" in locals():
            accounts_mod.fetch_profile = old_profile
        if "old_brief" in locals():
            server.fetch_room_brief = old_brief
        if "old_by_uid" in locals():
            server.fetch_room_by_uid = old_by_uid
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d 项全部通过" % len(PASSED), flush=True)


if __name__ == "__main__":
    main()
