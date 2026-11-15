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
import urllib.request
import zlib

import capture
import server
from accounts import AccountStore, parse_cookie_csv
from capture import CaptureManager
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
                                               "live_status": 1, "online": 123})
        check("添加直播间并自动取标题", created and r1["label"] == "深夜杂谈" and
              r1["live_status"] == 1, r1)
        r2, created2 = rooms.add(1001, "备用房间")
        check("添加第二个直播间", created2 and r2["label"] == "备用房间")
        _, created3 = rooms.add(22748536, "改名")
        check("同名房间号重复添加为更新", not created3 and
              rooms.get(22748536)["label"] == "改名")
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
        check("仪表盘高额 SC / 最近动态 / 时间轴",
              len(dash["top_sc"]) == 1 and len(dash["recent"]) >= 2 and
              len(dash["timeline"]["labels"]) == 12)
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

        # ---------- HTTP API ----------
        cfg = Config(os.path.join(tmp, "config.json"))
        logs = LogBuffer()
        logs.add("info", "selftest")
        manager = CaptureManager(st, cfg.get,
                                 lambda level, msg: logs.add(level, msg),
                                 rooms=rooms)
        server.fetch_room_brief = lambda rid, timeout=3: {
            "title": "房间 %s" % rid, "uid": 1, "short_id": 0,
            "live_status": 1, "online": 10}
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
        status, raw = get("/api/rooms")
        j = json.loads(raw)
        check("直播间列表含两间且都未采集",
              status == 200 and len(j["items"]) == 2 and j["active"] == 0, j)
        check("直播间列表带按房间统计",
              any(x["totals"]["events"] == 1 for x in j["items"]))
        status, raw = get("/api/dashboard")
        j = json.loads(raw)
        check("仪表盘接口", status == 200 and j["totals"]["sc"] == 1 and
              "timeline" in j and "top_sc" in j)
        status, raw = get("/api/feed?room_id=22748536&types=danmaku,sc")
        j = json.loads(raw)
        check("内容接口按房间返回分栏",
              len(j["items"]["danmaku"]) == 1 and len(j["items"]["sc"]) == 1 and
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
        status, raw = post("/api/rooms", {"room_id": 3003, "label": "第三间"})
        check("网页添加直播间（不联网）",
              status == 200 and len(json.loads(raw)["rooms"]["items"]) == 3)
        status, raw = post("/api/rooms/refresh", {})
        check("刷新直播间信息接口", json.loads(raw)["updated"] == 3)
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
            business = [{"cmd": "DANMU_MSG",
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
            check("两间弹幕都经采集链路入库",
                  snap["counts"]["danmaku"] >= 100, snap["counts"])
            check("两间 SC 都入库并区分房间",
                  len(st.query("sc", room_id=22748536)[1]) == 2 and
                  len(st.query("sc", room_id=1001)[1]) == 1,
                  (st.query("sc", room_id=22748536)[0],
                   st.query("sc", room_id=1001)[0]))
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
            for _ in range(40):
                if st.counts()["danmaku"] == 103:
                    break
                time.sleep(0.1)
            check("停止时冲刷剩余缓冲", st.counts()["danmaku"] == 103, st.counts())
        finally:
            capture.BiliApi, capture.connect = old_api, old_connect

        check("未知接口 404", get("/api/nope")[0] == 404)
        status, raw = post("/api/clear", {"confirm": "yes", "room_id": 1001})
        check("按房间清空数据", status == 200 and
              st.counts()["events"] == 1, st.counts())
        status, _ = post("/api/clear", {})
        check("清空需确认", status == 400)

        srv.shutdown()
        srv.server_close()

        # ---------- 网页结构 ----------
        with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as fh:
            page = fh.read()
        check("四个页面：仪表盘 / 直播间 / 配置 / 日志",
              all(k in page for k in ('id="page-home"', 'id="page-rooms"',
                                      'id="page-config"', 'id="page-logs"',
                                      'data-page="home"', "loadDashboard")))
        check("直播间页可增删、切换、按房间启停",
              all(k in page for k in ("btnAddRoom", "btnDelRoom", "roomChips",
                                      "/api/rooms", "/api/capture/",
                                      "/api/capture/stop_all", "btnStopAll")))
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
        check("沿用 live-archive 配色与字体变量",
              all(k in page for k in ("--fire:#ff7a45", "--jp:", "--bg:#0a0807",
                                      "--gold:#e8c489")))
        check("保留日志页与 CSV 账号上传",
              all(k in page for k in ("logBox", "acctFile", "btnUpload",
                                      "/api/export", "btnLogs")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d 项全部通过" % len(PASSED), flush=True)


if __name__ == "__main__":
    main()
