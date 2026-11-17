# -*- coding: utf-8 -*-
"""Offline self-check: protocol, multi-room capture, storage, REST API, web page.

    python selftest.py

Never connects to Bilibili. Uses a temporary data dir, a loopback server and a
fake websocket. Room info lookup is patched out as well.
"""
from __future__ import annotations

import asyncio
import base64
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
import import_history
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

    def _varint(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def _fld(num, wire, payload):
        return _varint((num << 3) | wire) + payload

    def _fint(num, value):
        return _fld(num, 0, _varint(value))

    def _fstr(num, text):
        raw = text.encode("utf-8")
        return _fld(num, 2, _varint(len(raw)) + raw)

    iw = (_fint(1, 123) + _fstr(2, "入场者") + _fint(5, 1)
          + _fint(6, 22748536) + _fint(7, 1794700000))
    iw = capture.parse_interact_word_v2(base64.b64encode(iw).decode())
    check("INTERACT_WORD_V2 解出 uid/msg_type",
          iw and iw["uid"] == 123 and iw["uname"] == "入场者"
          and iw["msg_type"] == 1, iw)
    gift = (_fint(1, 9) + _fstr(2, "辣条") + _fint(3, 3)
            + _fint(5, 100) + _fstr(8, "gold") + _fint(10, 1794700000))
    base_text = _fstr(1, "送礼者")
    user = _fint(1, 456) + _fld(2, 2, _varint(len(base_text)) + base_text)
    gift_pb = (_fint(1, 456) + _fld(10, 2, _varint(len(gift)) + gift)
               + _fld(15, 2, _varint(len(user)) + user))
    gift_pb = capture.parse_send_gift_v2(base64.b64encode(gift_pb).decode())
    check("SEND_GIFT_V2 解出礼物 / 数量 / 用户",
          gift_pb and gift_pb["uid"] == 456 and gift_pb["uname"] == "送礼者"
          and gift_pb["giftName"] == "辣条" and gift_pb["num"] == 3, gift_pb)

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
    check("CPU 占用标明口径（全机 / 本服务）",
          snap["cpu"].get("scope") in ("system", "process"), snap["cpu"])
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

    # 动态抓取失败必须抛错，不能被吞成空结果——否则调度器当成功写
    # dyn_last_ts，一次抖动就变成整天不补抓（11-17 00:15 的真实事故）。
    _real_sleep = dynamics_mod.time.sleep
    dynamics_mod.time.sleep = lambda _s: None   # 跳过空结果重试的 1.2 秒等待
    try:
        f = dynamics_mod.DynamicsFetcher()
        f._warmup = lambda: None                 # 绝不能真的联网
        f.wbi_keys = lambda: ("img", "sub")
        f._fetch_json = lambda url, referer=None: {"code": 0, "data": {"items": []}}
        check("动态抓取：干净空结果返回空列表不抛错",
              f.fetch(9) == ([], False))

        f._fetch_json = lambda url, referer=None: {
            "code": -412, "message": "请求被拦截"}
        try:
            f.fetch(9)
            raised = False
        except dynamics_mod.DynamicsFetchError:
            raised = True
        check("动态抓取：code!=0 抛 DynamicsFetchError", raised)

        def _boom(url, referer=None):
            raise OSError("timeout")
        f._fetch_json = _boom
        try:
            f.fetch(9)
            raised = False
        except dynamics_mod.DynamicsFetchError:
            raised = True
        check("动态抓取：请求异常抛 DynamicsFetchError", raised)

        f.wbi_keys = lambda: ("", "")
        try:
            f.fetch(9)
            raised = False
        except dynamics_mod.DynamicsFetchError:
            raised = True
        check("动态抓取：WBI key 为空抛 DynamicsFetchError", raised)

        # 登录态注入：只带白名单 cookie，装进请求头；诊断标注「有登录」
        f2 = dynamics_mod.DynamicsFetcher(cookies={
            "SESSDATA": "secret", "DedeUserID": "123", "buvid3": "B3",
            "无关项": "drop-me"})
        check("动态抓取：只带白名单 cookie 且标记已登录",
              f2.logged_in is True and
              set(f2.cookies) == {"SESSDATA", "DedeUserID", "buvid3"} and
              "无关项" not in f2.cookies, f2.cookies)
        h2 = f2._headers("https://x/")
        check("动态抓取：登录 cookie 写进请求头",
              "SESSDATA=secret" in h2.get("Cookie", "") and
              "无关项" not in h2.get("Cookie", ""), h2)
        f3 = dynamics_mod.DynamicsFetcher(cookies={"buvid3": "B3"})
        check("动态抓取：无 SESSDATA 不算登录",
              f3.logged_in is False and "buvid3" in f3.cookies)
        # 412 异常信息带风控提示与身份，便于日志区分匿名拦截
        f2._warmup = lambda: None
        f2.wbi_keys = lambda: ("img", "sub")
        def _boom412(url, referer=None):
            raise urllib.error.HTTPError(url, 412, "Precondition Failed", {}, None)
        f2._fetch_json = _boom412
        try:
            f2.fetch(9)
            raised = False
            err = ""
        except dynamics_mod.DynamicsFetchError as exc:
            raised, err = True, str(exc)
        check("动态抓取：412 诊断含风控与登录态",
              raised and "412" in err and "风控" in err and "有" in err, err)
    finally:
        dynamics_mod.time.sleep = _real_sleep

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
    flat = capture.event_row(22748536, "gift", {
        "uid": 8, "uname": "C", "giftName": "辣条", "num": 3,
        "action": "投喂"}, now)
    check("平铺礼物摘要（SEND_GIFT）", flat["detail"] == "投喂 辣条 ×3",
          flat["detail"])
    red = capture.event_row(22748536, "red_pocket", {
        "uid": 9, "uname": "D", "gift_name": "红包", "gift_price": 5000}, now)
    check("红包事件摘要", "人气红包" in red["detail"] and "¥5" in red["detail"],
          red["detail"])
    enter = capture.event_row(22748536, "enter", {"uid": 10, "uname": "E"}, now)
    check("入场事件摘要", enter["detail"] == "进入直播间", enter["detail"])
    follow = capture.event_row(22748536, "follow", {"uid": 11, "uname": "F"}, now)
    check("互动事件摘要", follow["detail"] == "关注了主播", follow["detail"])

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
              r1["collect_live"] is True and r1["collect_dynamics"] is True and
              r1["live_only"] is False, r1)
        check("定时抓取默认关闭、默认 08:00",
              r1["dyn_auto"] is False and r1["dyn_hour"] == 8 and
              r1["dyn_minute"] == 0, r1)
        check("按 uid 找采集器", (rooms.find_by_uid(9) or {}).get("id") == 22748536)
        rooms.settings(22748536, {"collect_dynamics": False})
        check("采集器开关可单独关闭",
              rooms.get(22748536)["collect_dynamics"] is False and
              rooms.get(22748536)["collect_live"] is True)
        rooms.settings(22748536, {"live_only": True})
        check("可以只选「仅在开播时采集」",
              rooms.get(22748536)["live_only"] is True and
              RoomStore(room_path).get(22748536)["live_only"] is True)
        rooms.settings(22748536, {"live_only": False})
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
        lives = [x for x in sess if x["kind"] == "live"]
        check("首次开播前的数据单独成段（更早）",
              sess[0]["kind"] == "gap" and sess[0].get("lead") is True and
              sess[0]["danmaku"] == 1, sess[0])
        check("按开播事件切分场次", len(lives) == 2 and
              lives[0]["start"] == base and lives[1]["start"] == t2ms, sess)
        check("首场以下一场开播为终点", lives[0]["end"] == t2ms)
        check("最后一场至今（end=0）", lives[1]["end"] == 0)
        check("每场弹幕数正确", lives[0]["danmaku"] == 2 and
              lives[1]["danmaku"] == 1, sess)
        check("场次去重观众数", lives[0]["viewers"] == 2 and
              lives[1]["viewers"] == 1, sess)
        check("没有下播事件时不产生场间段",
              [x["kind"] for x in sess if not x.get("lead")] == ["live", "live"],
              sess)
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

        # 没有任何开播记录 → 所有采集到的内容归成一段，不按批次猜
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
        check("无开播记录时所有数据归为一段",
              len(rsess) == 1 and rsess[0].get("single") is True and
              rsess[0]["kind"] == "gap" and rsess[0]["start"] == run_start + 500 and
              rsess[0]["danmaku"] == 2, rsess)
        rby = {x["room_id"]: x for x in rstore.dashboard()["by_room"]}
        check("无开播记录时开播次数为 0（不拿采集批次冒充）",
              rby[22748536]["sessions"] == 0 and
              rby[22748536]["sessions_approx"] is False and
              rby[22748536]["last_live_at"] == 0, rby[22748536])
        # 之后真的开播了 → 那一段被切开，之前的全部归「更早」
        live_ev = capture.event_row(22748536, "live", {"live": 1},
                                    base2 + 7200 * 1000)
        live_ev["event_time"] = (base2 + 7200 * 1000) // 1000
        rstore.insert_event(live_ev)
        rsplit = rstore.live_segments(22748536)
        check("下一次开播把未分段切开",
              [s["kind"] for s in rsplit] == ["gap", "live"] and
              rsplit[0].get("lead") is True and rsplit[0]["danmaku"] == 2 and
              rsplit[0].get("single") is None, rsplit)

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
        # 区间左闭右开，下播事件本身用 end=下播时刻+1ms 归在场内
        check("场 = 开播到下播（含下播事件）", gsegs[0]["end"] == gt0 + hour + 1 and
              gsegs[0]["danmaku"] == 2 and gsegs[0]["sc_amount"] == 30)
        check("场间 = 上一次下播到这次开播",
              gsegs[1]["start"] == gt0 + hour + 1 and
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

        # 动态分页：offset 生效，越界给空页
        page_rows = [{"id": "p%02d" % i, "mid": 77, "pub_ts": now // 1000 - i,
                      "type": "DYNAMIC_TYPE_WORD", "text": "分页 %d" % i,
                      "pics": [], "jump_url": ""} for i in range(25)]
        sst.upsert_dynamics(page_rows)
        p_total, p1 = sst.list_dynamics(limit=10, offset=0, mid=77)
        _, p2 = sst.list_dynamics(limit=10, offset=10, mid=77)
        _, p3 = sst.list_dynamics(limit=10, offset=20, mid=77)
        _, p9 = sst.list_dynamics(limit=10, offset=90, mid=77)
        check("动态分页：总数与首页条数",
              p_total == 25 and len(p1) == 10 and len(p2) == 10 and len(p3) == 5,
              (p_total, len(p1), len(p2), len(p3)))
        check("动态分页：页间不重叠且按时间倒序",
              not ({x["dyn_id"] for x in p1} & {x["dyn_id"] for x in p2}) and
              p1[0]["dyn_id"] == "p00" and p2[0]["dyn_id"] == "p10" and
              p3[-1]["dyn_id"] == "p24", (p1[0]["dyn_id"], p2[0]["dyn_id"]))
        check("动态分页：翻过头给空页", p9 == [])

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
        check("旧库也补上导入去重列", "import_key" in cols)

        # ---------- 历史库导入（translate_history.db 那种结构） ----------
        src_path = os.path.join(tmp, "history_src.db")
        src = sqlite3.connect(src_path)
        src.execute("CREATE TABLE danmaku (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " room_id INTEGER NOT NULL, uid INTEGER, uname TEXT,"
                    " text TEXT NOT NULL, event_time INTEGER,"
                    " received_at INTEGER NOT NULL, medal_name TEXT,"
                    " medal_level INTEGER, guard_level INTEGER,"
                    " is_admin INTEGER DEFAULT 0, is_emoticon INTEGER DEFAULT 0)")
        src.execute("CREATE TABLE sc_message (sc_id TEXT PRIMARY KEY,"
                    " room_id INTEGER NOT NULL, uid INTEGER, uname TEXT,"
                    " price INTEGER, message TEXT, start_time INTEGER,"
                    " end_time INTEGER, received_at INTEGER NOT NULL,"
                    " guard_level INTEGER, medal_name TEXT, medal_level INTEGER,"
                    " raw_json TEXT NOT NULL, message_trans TEXT)")
        src.execute("CREATE TABLE room_event (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " room_id INTEGER NOT NULL, event_type TEXT NOT NULL,"
                    " event_key TEXT NOT NULL, uid INTEGER, uname TEXT,"
                    " gift_name TEXT, gift_num INTEGER, price INTEGER,"
                    " coin_type TEXT, event_time INTEGER,"
                    " received_at INTEGER NOT NULL, raw_json TEXT NOT NULL)")
        base = 1790000000000
        rows = []
        # 两场：中间隔 2 小时（> 30 分钟，应当切成两段）
        for i in range(3):
            rows.append((555, 1, "甲", "第一场 %d" % i, 0, base + i * 1000,
                         "", 0, 0, 0, 0))
        for i in range(2):
            rows.append((555, 2, "乙", "第二场 %d" % i, 0, base + 7200000 + i * 1000,
                         "", 0, 0, 0, 0))
        src.executemany("INSERT INTO danmaku(room_id,uid,uname,text,event_time,"
                        "received_at,medal_name,medal_level,guard_level,is_admin,"
                        "is_emoticon) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        src.execute("INSERT INTO sc_message(sc_id,room_id,uid,uname,price,message,"
                    "start_time,end_time,received_at,guard_level,medal_name,"
                    "medal_level,raw_json,message_trans) VALUES "
                    "('s1',555,1,'甲',30,'打钱',1,60,?,3,'牌',7,'{}','打钱啦')",
                    (base + 500,))
        src.execute("INSERT INTO room_event(room_id,event_type,event_key,uid,uname,"
                    "gift_name,gift_num,price,coin_type,event_time,received_at,"
                    "raw_json) VALUES (555,'gift','k1',1,'甲','辣条',3,300,'gold',"
                    "1,?,'{\"guard_level\":0}')", (base + 800,))
        src.commit()
        src.close()

        imp_path = os.path.join(tmp, "import_target.db")
        imp = Store(imp_path)
        src_conn = sqlite3.connect(src_path)
        # 测试数据的两段各只有几秒，先关掉「最小时长」门槛，专门验切场逻辑
        first = import_history.import_room(src_conn, imp, 555, 30 * 60 * 1000,
                                           min_ms=0)
        check("导入历史弹幕 / SC / 事件",
              first["danmaku"] == 5 and first["sc"] == 1 and first["events"] == 1,
              first)
        check("导入按空档补出两场开播记录", first["sessions"] == 4, first)
        imp_segs = imp.live_segments(555)
        check("导入后切出两场 + 开播前场间",
              len(imp_segs) == 3 and imp_segs[0]["kind"] == "gap" and
              imp_segs[0].get("lead") is True and imp_segs[0]["danmaku"] == 1 and
              imp_segs[1]["kind"] == "live" and imp_segs[1]["danmaku"] == 2 and
              imp_segs[2]["kind"] == "live" and imp_segs[2]["danmaku"] == 2,
              imp_segs)
        opens = sorted([r for r in imp.query("events", room_id=555)[1]
                        if r["kind"] == "live" and r["detail"] == "开播"],
                       key=lambda r: r["received_at"])
        check("开播优先取最早 SC / 上舰",
              len(opens) == 2 and opens[0]["received_at"] == base + 500 and
              json.loads(opens[0]["raw_json"]).get("basis") == "first_sc_guard",
              [(r["received_at"], r["raw_json"]) for r in opens])
        check("没有 SC / 上舰时回退首条弹幕",
              opens[1]["received_at"] == base + 7200000 - 1000 and
              json.loads(opens[1]["raw_json"]).get("basis") == "first_danmaku",
              [(r["received_at"], r["raw_json"]) for r in opens])
        check("导入保留 SC 译文与金额",
              imp.query("sc")[1][0]["message_trans"] == "打钱啦" and
              imp.range_stats(555, 1)["sc_amount"] >= 30)
        again = import_history.import_room(src_conn, imp, 555, 30 * 60 * 1000,
                                           min_ms=0)
        check("同一份历史库重复导入不写重",
              again["danmaku"] == 0 and again["sc"] == 0 and
              again["events"] == 0 and again["sessions"] == 0, again)
        check("重复导入后总数不变", imp.counts()["danmaku"] == 5,
              imp.counts())

        # 太短的一段（零星路过弹幕）不该切出一场一分钟的假场次
        src_conn.execute(
            "INSERT INTO danmaku(room_id,uid,uname,text,event_time,received_at,"
            "medal_name,medal_level,guard_level,is_admin,is_emoticon) VALUES "
            "(555,3,'丙','路过',0,?,'',0,0,0,0)", (base + 10800000,))
        src_conn.commit()
        blip = import_history.import_room(src_conn, imp, 555, 30 * 60 * 1000,
                                          min_ms=5 * 60 * 1000)
        check("过短的一段不算一场，只计入跳过数",
              blip["sessions"] == 0 and blip["skipped"] >= 1 and
              blip["danmaku"] == 1, blip)
        check("被跳过的段仍能按「场间」翻到",
              len(imp.live_segments(555)) >= 3 and
              sum(x["danmaku"] for x in imp.live_segments(555)) == 6,
              [(x["kind"], x["danmaku"]) for x in imp.live_segments(555)])
        src_conn.close()

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
        nx = srv.dynamics._next_run()
        check("动态定时器算得出下一个时间点（mktime 要 9 元组）",
              isinstance(nx, float) and 0 < nx - time.time() <= 86400 + 2,
              (nx, time.time()))
        pulled = []
        real_pull_one = srv.dynamics.pull_one
        srv.dynamics.pull_one = (lambda mid, reason="手动", limit=20:
                                 pulled.append(mid) or (1, 1))
        try:
            rooms.settings(22748536, {"dyn_auto": False})
            srv.dynamics._tick()
            check("定时抓取默认关闭，没开就不拉", not pulled, pulled)
            # 00:00 今天早就过了，且 last_ts 为空 → 该补一次
            rooms.settings(22748536, {"dyn_auto": True, "dyn_hour": 0,
                                       "dyn_minute": 0, "dyn_last_ts": 0})
            srv.dynamics._tick()
            uid_now = rooms.get(22748536)["uid"]
            check("只拉打开了定时的那一位 up 主", pulled == [uid_now], pulled)
            rooms.settings(22748536, {"dyn_last_ts": int(time.time() * 1000)})
            srv.dynamics._tick()
            check("今天这个时间点拉过就不再重复", len(pulled) == 1, pulled)
            rooms.settings(22748536, {"dyn_auto": False, "dyn_last_ts": 0})
            srv.dynamics._tick()
            check("关掉这一位的定时抓取后不再拉", len(pulled) == 1, pulled)
        finally:
            srv.dynamics.pull_one = real_pull_one
            rooms.settings(22748536, {"dyn_auto": False, "dyn_last_ts": 0})

        # 空结果 / 失败都不能写 dyn_last_ts，否则当天不再补抓
        srv.dynamics._fail.clear()
        empty_pulls = []
        outcomes = [(0, 0), (0, 0), (2, 2)]
        def flaky_pull(mid, reason="手动", limit=20):
            empty_pulls.append(mid)
            return outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        srv.dynamics.pull_one = flaky_pull
        try:
            rooms.settings(22748536, {"dyn_auto": True, "dyn_hour": 0,
                                       "dyn_minute": 0, "dyn_last_ts": 0})
            srv.dynamics._tick()
            check("定时抓取 0 条不写 dyn_last_ts、页面显示失败",
                  rooms.get(22748536)["dyn_last_ts"] == 0 and
                  srv.dynamics.last_result["ok"] is False and
                  "0 条" in srv.dynamics.last_result["msg"],
                  srv.dynamics.last_result)
            srv.dynamics._tick()
            check("空结果后退避期内不重复打接口", len(empty_pulls) == 1,
                  empty_pulls)
            # 手动把退避时间推到过去，模拟「过了一会儿」
            rid = 22748536
            t, delay, streak = srv.dynamics._fail[rid]
            srv.dynamics._fail[rid] = (t - delay - 1, delay, streak)
            srv.dynamics._tick()
            check("空结果后仍会在当天补抓（退避过期就重试）",
                  len(empty_pulls) == 2, empty_pulls)
            t, delay, streak = srv.dynamics._fail[rid]
            srv.dynamics._fail[rid] = (t - delay - 1, delay, streak)
            srv.dynamics._tick()
            check("补抓成功才写 dyn_last_ts 并清退避",
                  rooms.get(22748536)["dyn_last_ts"] > 0 and
                  rid not in srv.dynamics._fail and
                  srv.dynamics.last_result["ok"] is True,
                  srv.dynamics.last_result)
        finally:
            srv.dynamics.pull_one = real_pull_one
            srv.dynamics._fail.clear()
            rooms.settings(22748536, {"dyn_auto": False, "dyn_last_ts": 0})

        # 调度器把当前启用账号的 cookie 注入抓取器；匿名时传空 dict
        acct_csv = "\n".join([
            "# Netscape HTTP Cookie File",
            ".bilibili.com\tTRUE\t/\tTRUE\t1893456000\tSESSDATA\tsched-token",
            ".bilibili.com\tTRUE\t/\tFALSE\t1893456000\tbuvid3\tB3"])
        acct_item, _, acct_err = srv.accounts.add("抓取账号", acct_csv)
        seen_cookies = []
        class RecordingFetcher:
            def __init__(self, cookies=None):
                seen_cookies.append(cookies)
            def fetch(self, mid, limit=20, offset=""):
                return [], False
        old_cls = srv.dynamics.fetcher_cls
        srv.dynamics.fetcher_cls = RecordingFetcher
        try:
            srv.dynamics.pull_one(999, "测试")
            check("调度器给抓取器注入启用账号 cookie",
                  seen_cookies and seen_cookies[-1].get("SESSDATA") == "sched-token",
                  seen_cookies)
            check("调度器身份标签写账号名",
                  "抓取账号" in srv.dynamics.auth_label(),
                  srv.dynamics.auth_label())
            srv.accounts.active_id = ""
            srv.dynamics.pull_one(999, "测试")
            check("匿名时抓取器拿到空 cookie",
                  seen_cookies[-1] == {}, seen_cookies[-1])
            check("匿名身份标签为「匿名」",
                  srv.dynamics.auth_label() == "匿名",
                  srv.dynamics.auth_label())
        finally:
            srv.dynamics.fetcher_cls = old_cls
            srv.accounts.remove(acct_item["id"])

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
                   "collect_live", "collect_dynamics", "live_only", "waiting",
                   "dyns")))
        check("采集器列表带采集目标摘要：最近开播 / 动态定时 / 上次拉取",
              all(k in j["items"][0] for k in
                  ("last_live_at", "dyn_last_at", "dyn_schedule", "dyn_auto")) and
              j["items"][0]["last_live_at"] >= 1794600000000 and
              isinstance(j["items"][0]["dyn_schedule"], str) and
              ":" in j["items"][0]["dyn_schedule"],
              {k: j["items"][0].get(k) for k in
               ("last_live_at", "dyn_last_at", "dyn_schedule")})
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
        jl = [s for s in (jj.get("segments") or []) if s["kind"] == "live"]
        check("场次接口返回场 / 场间分段",
              status == 200 and len(jl) == 2 and jl[0]["danmaku"] == 2 and
              jj["segments"][0].get("lead") is True, jj.get("segments"))
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
        srv.store.upsert_dynamics([
            {"id": "api-%d" % i, "mid": 4242, "pub_ts": now // 1000 - i,
             "type": "DYNAMIC_TYPE_WORD", "text": "接口动态 %d" % i,
             "pics": [], "jump_url": ""} for i in range(3)])
        status, raw = get("/api/dynamics?mid=4242&limit=1&offset=0")
        jp0 = json.loads(raw)
        status, raw = get("/api/dynamics?mid=4242&limit=1&offset=1")
        jpg = json.loads(raw)
        check("动态接口支持分页（limit / offset 原样回传）",
              status == 200 and jp0["total"] == jpg["total"] and
              jpg["total"] >= 3 and len(jpg["items"]) == 1 and
              jpg["limit"] == 1 and jpg["offset"] == 1 and
              jpg["items"][0]["dyn_id"] != jp0["items"][0]["dyn_id"],
              {"items": jpg.get("items"), "offset": jpg.get("offset"),
               "limit": jpg.get("limit"), "total": jpg.get("total")})
        status, raw = post("/api/dynamics/trans", {"dyn_id": "d1", "text": "接口译文"})
        check("动态翻译写入接口", status == 200 and
              json.loads(raw)["ok"] is True)
        status, raw = post("/api/dynamics/config",
                           {"room_id": 22748536, "dyn_auto": True,
                            "dyn_hour": 9, "dyn_minute": 25})
        jc = json.loads(raw)
        check("定时抓取按 up 主保存、精确到分钟",
              status == 200 and jc["config"]["dyn_hour"] == 9 and
              jc["config"]["dyn_minute"] == 25 and
              rooms.get(22748536)["dyn_auto"] is True and
              rooms.get(22748536)["dyn_minute"] == 25 and
              rooms.get(1001)["dyn_auto"] is False, jc.get("config"))
        check("定时抓取默认关闭（另一位 up 主没被顺带打开）",
              rooms.get(1001)["dyn_auto"] is False and
              rooms.get(1001)["dyn_hour"] == 8)
        status, raw = post("/api/dynamics/config",
                           {"dyn_auto": True, "dyn_hour": 9})
        check("不带 room_id 的定时设置被拒（必须指明是哪位 up 主）",
              status == 400)
        rooms.settings(22748536, {"dyn_auto": False})
        status, raw = get("/api/dynamics?mid=9")
        check("动态接口按 up 主 uid 过滤",
              status == 200 and json.loads(raw)["total"] == 1)

        # ---------- 动态调度器：按采集器逐个拉 ----------
        class _FakeFetcher(object):
            seen = []

            def __init__(self, cookies=None):
                pass

            def fetch(self, mid, limit=20):
                _FakeFetcher.seen.append(mid)
                return ([{"id": "f%s" % mid, "mid": mid, "pub_ts": now // 1000,
                          "text": "fake", "pics": [], "jump_url": ""}], "")

        sched = server.DynamicScheduler(sst, cfg, logs,
                                        fetcher_cls=_FakeFetcher, rooms=rooms)
        check("调度目标只取开了「采集动态」的 up 主",
              sched.targets() == [9], sched.targets())
        res = sched.pull("测试")
        check("调度器按采集器逐个拉动态",
              res["ok"] and _FakeFetcher.seen == [9] and
              sst.list_dynamics(mid=9)[0] == 2, (res, _FakeFetcher.seen))
        rooms.settings(22748536, {"collect_dynamics": False})
        check("全部关掉动态采集后没有目标", sched.targets() == [])
        cfg.save({"dyn_mid": 555})
        check("没有采集器时用 dyn_mid 兜底", sched.targets() == [555])
        rooms.settings(22748536, {"collect_dynamics": True})
        cfg.save({"dyn_mid": 0})
        rooms.settings(22748536, {"dyn_hour": 7, "dyn_minute": 45})
        check("定时时间按 up 主保存到分钟",
              rooms.get(22748536)["dyn_hour"] == 7 and
              rooms.get(22748536)["dyn_minute"] == 45 and
              rooms.get(1001)["dyn_hour"] == 8)
        status, raw = get("/api/feed?room_id=22748536&types=danmaku,sc")
        j = json.loads(raw)
        check("内容接口按房间返回分栏",
              len(j["items"]["danmaku"]) >= 1 and len(j["items"]["sc"]) == 1 and
              "events" not in j["items"], j["items"].keys())
        status, raw = get("/api/feed?paged=1&room_id=22748536&types=danmaku&limit=1")
        jp = json.loads(raw)
        check("弹幕分页接口：只回这一页，带总数与「还有更早」标记",
              status == 200 and jp["paged"] is True and
              len(jp["items"]["danmaku"]) == 1 and
              isinstance(jp["more"]["danmaku"], bool) and
              jp["counts"]["danmaku"] >= 1, jp.get("more"))
        cursor = jp["items"]["danmaku"][0]["received_at"]
        status, raw = get("/api/feed?paged=1&room_id=22748536&types=danmaku"
                          "&limit=50&before=%d&incl=1" % cursor)
        jb = json.loads(raw)
        check("游标翻页把游标那一毫秒也算上（前端按主键去重，不重不漏）",
              len(jb["items"]["danmaku"]) >= 1 and
              jb["items"]["danmaku"][-1]["received_at"] <= cursor,
              jb["items"]["danmaku"][-1])
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
                                                    "params": {"live_only": True}})
        j = json.loads(raw)
        check("采集器开关接口：只在开播时采集",
              status == 200 and j["room"]["live_only"] is True and
              any(x["live_only"] for x in j["rooms"]["items"]),
              j.get("room"))
        post("/api/rooms/settings", {"room_id": 22748536,
                                     "params": {"live_only": False}})
        status, raw = post("/api/rooms/settings", {"room_id": 22748536,
                                                    "params": {"protover": 3,
                                                               "heartbeat_seconds": 45,
                                                               "record_enter": True}})
        j = json.loads(raw)
        check("按房间保存采集参数",
              status == 200 and j["room"]["params"]["protover"] == 3 and
              j["room"]["params"]["heartbeat_seconds"] == 45 and
              j["room"]["params"]["record_enter"] is True, j.get("room"))
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
            business += [
                {"cmd": "SEND_GIFT", "data": {
                    "uid": 10, "uname": "送礼者", "giftName": "辣条",
                    "num": 3, "action": "投喂"}},
                {"cmd": "GUARD_BUY", "data": {
                    "uid": 11, "uname": "上舰者", "guard_level": 3,
                    "num": 1}},
                {"cmd": "POPULARITY_RED_POCKET_NEW", "data": {
                    "uid": 12, "uname": "红包人", "gift_name": "红包",
                    "total_price": 5000}},
                {"cmd": "INTERACT_WORD", "data": {
                    "uid": 13, "uname": "关注者", "msg_type": 2}},
                {"cmd": "INTERACT_WORD", "data": {
                    "uid": 14, "uname": "路过", "msg_type": 1}},
            ]
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
            # 房间 B 单独关掉礼物 / 上舰 / 红包 / 其他互动，验证按房间生效
            pre_b_kinds = {r["kind"] for r in st.query("events",
                                                       room_id=1001)[1]}
            rooms.settings(1001, {"record_gift": False, "record_guard": False,
                                  "record_red_pocket": False,
                                  "record_interact": False})
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
            kinds_a = {r["kind"] for r in live_rows}
            _, rows_b = st.query("events", room_id=1001)
            kinds_b = {r["kind"] for r in rows_b}
            check("礼物 / 上舰 / 红包 / 关注事件入库",
                  {"gift", "guard", "red_pocket", "follow"} <= kinds_a, kinds_a)
            check("入场默认不开；按房间打开后记录",
                  "enter" in kinds_a and "enter" not in kinds_b, kinds_b)
            new_b_kinds = kinds_b - pre_b_kinds
            check("按房间关闭事件子类（礼物/上舰/红包/互动）",
                  not ({"gift", "guard", "red_pocket", "follow"} & new_b_kinds),
                  new_b_kinds)
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

            # ---------- 只在开播时采集（mock，零联网） ----------
            class FlippedApi(FakeApi):
                status = 0

                def resolve_room(self, room):
                    out = dict(FakeApi.resolve_room(self, room))
                    out["live_status"] = FlippedApi.status
                    # 真开播时 B 站一定带开播时间；live_time=0 表示误报，会被跳过
                    out["live_time"] = 1794600000 if FlippedApi.status == 1 else 0
                    return out

            old_watch = capture.WATCH_INTERVAL
            capture.BiliApi, capture.WATCH_INTERVAL = FlippedApi, 1
            try:
                rooms.settings(1001, {"live_only": True})
                before = st.counts()["danmaku"]
                ok, message = manager.start(1001)
                check("只在开播时采集：可以启动", ok, message)
                time.sleep(0.8)
                st1001 = {s["room_id"]: s
                          for s in manager.snapshot()["rooms"]}[1001]
                check("没开播时标成等开播，且没有连弹幕服务器",
                      st1001["running"] and st1001["waiting"] and
                      st1001["last_packet_at"] == 0 and st1001["live_status"] == 0,
                      st1001)
                check("没开播时不产生任何数据",
                      st.counts()["danmaku"] == before, st.counts())
                FlippedApi.status = 1
                frame_pool.insert(0, frames_for(1001, "C"))
                for _ in range(60):
                    time.sleep(0.1)
                    if st.counts()["danmaku"] > before:
                        break
                st1001 = {s["room_id"]: s
                          for s in manager.snapshot()["rooms"]}[1001]
                check("开播后自动连上并开始入库",
                      st.counts()["danmaku"] > before and
                      st1001["waiting"] is False and st1001["live_status"] == 1,
                      (st.counts(), st1001["waiting"]))
                opens = st.db.execute(
                    "SELECT COUNT(*) FROM room_event WHERE room_id=1001 "
                    "AND kind='live' AND detail='开播'").fetchone()[0]
                check("连上时不重复补记开播（同一场不被切两半）", opens == 1, opens)
                manager.stop(1001)

                # ---------- 探状态失败时不能补记幻影「开播」 ----------
                class RaisingApi(FlippedApi):
                    def resolve_room(self, room):
                        raise RuntimeError("boom")

                capture.BiliApi = RaisingApi
                rooms.settings(1001, {"live_only": True})
                opens = st.db.execute(
                    "SELECT COUNT(*) FROM room_event WHERE room_id=1001 "
                    "AND kind='live' AND detail='开播'").fetchone()[0]
                ok, message = manager.start(1001)
                for _ in range(50):
                    if ok:
                        break
                    time.sleep(0.1)      # 等 stop 的异步回收完成
                    ok, message = manager.start(1001)
                check("探状态失败也能启动", ok, message)
                time.sleep(2.5)          # WATCH_INTERVAL=1，至少空转两圈
                opens2 = st.db.execute(
                    "SELECT COUNT(*) FROM room_event WHERE room_id=1001 "
                    "AND kind='live' AND detail='开播'").fetchone()[0]
                check("探状态失败时不连弹幕、不补记幻影开播",
                      opens2 == opens, (opens, opens2))
                st1001 = {s["room_id"]: s
                          for s in manager.snapshot()["rooms"]}[1001]
                check("探状态失败时停在等待开播", st1001["waiting"], st1001)
                manager.stop(1001)
            finally:
                capture.BiliApi, capture.WATCH_INTERVAL = FakeApi, old_watch
                rooms.settings(1001, {"live_only": False})
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
        check("「刷新信息 / 全部停止」挂在采集器清单头部，不在单个 up 主面板里",
              page.index('id="btnRefreshRooms"') < page.index('id="roomPanel"') and
              page.index('id="btnStopAll"') < page.index('id="roomPanel"'))
        check("采集参数全局统一（配置页），采集器页不再有每房间参数卡",
              all(k in page for k in ("f_proto", "f_heartbeat", "f_stale",
                                      "f_dm", "f_sc", "f_ev")) and
              all(k not in page for k in ("roomParamsCard", "btnSaveParams",
                                          "r_proto", "r_hb")))
        check("直播数据只按场次查看：左侧列表 + 服务端分页往前追",
              all(k in page for k in ("sessionPane", "sessionList", "loadSessions",
                                      "selectSession", "data-sess=", "/api/sessions",
                                      "paged=1", "segRange", "segByKey",
                                      "场间", "sc_amount", "feed-more")) and
              'data-sess="all"' not in page and "全部数据" not in page)
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
        check("采集器内部用局部标签切分直播间 / 动态（可继续扩展）",
              all(k in page for k in ('id="roomSubTabs"', 'data-sub="live"',
                                      'data-sub="dyn"', 'data-panel="live"',
                                      'data-panel="dyn"', "showSubTab",
                                      "SUBTABS", "lc-subtab", "subN-live",
                                      "subN-dyn")) and
              page.index('data-panel="live"') < page.index('data-panel="dyn"'))
        check("有了局部标签就不再重复子页面大标题",
              '<h2><i>◆</i>直播间数据' not in page and
              '<h2><i>◆</i>up 主动态' not in page and
              'class="block-meta"' in page and 'id="feedInfo"' in page and
              'id="dynInfo"' in page)
        check("日志页不再重复展示服务器信息",
              "服务信息" not in page and "logSvcGrid" not in page and
              'id="logBox"' in page)
        check("首页只按 up 主分，不再有跨 up 主合计统计条",
              all(k in page for k in ("开播场次", "累计流水", "互动总数", "观众数",
                                      "metrics")) and
              'id="statRow"' not in page and "最近动态" not in page and
              "recentList" not in page)
        check("采集器清单列出采集目标：谁的动态/定时何时/累计多少 + 开播次数/最近开播",
              all(k in page for k in ("collector-list", "collector-add",
                                      'class="goal live"', 'class="goal dyn"',
                                      "last_live_at", "dyn_schedule",
                                      "dyn_last_at", "采集中",
                                      "累计开播")))
        check("采集器可勾「只在开播时采集」，采集中也显示等开播",
              all(k in page for k in ('id="colOnly"', "syncCollectUI",
                                      "只在开播时采集", "等开播", "live_only")))
        check("未保存修改会提示：译文 / 采集参数 / 关页前",
              all(k in page for k in ("dynDirtyCard", "confirmDynDirty",
                                      "data-orig", "cfgDirty", "cfgSnap",
                                      "beforeunload", "未保存")))
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
        check("弹幕按时间正序展示，越上方越早，往前翻页走服务端游标",
              "rows.map(function(r){return feedRow(k,r)}).join('')" in page and
              "rows.slice().reverse()" not in page and
              "&before=" in page and "incl=1" in page and
              "feed-more" in page and "载入更早的" in page and
              "FEED_PAGE" in page and "data-more=" in page)
        check("采集器清单分页：每页 3 个 + 上一页 / 下一页",
              all(k in page for k in ("ROOM_PAGE=3", "roomPager", "data-roompage",
                                      "jumpRoomPage", "paintRoomPager")))
        check("动态分页：每页 limit + offset，带上一页 / 下一页",
              all(k in page for k in ("dynPageSize", "dynPager", "data-dynpage",
                                      "offset=", "dynPage=0")) and
              "btnPrev" not in page and "btnNext" not in page)
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
