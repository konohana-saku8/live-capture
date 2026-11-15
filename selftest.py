# -*- coding: utf-8 -*-
"""Offline self-check: protocol, storage, REST API and web page.

    python selftest.py

Never connects to Bilibili. Uses a temporary data dir and a loopback server.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
import zlib

import capture
from accounts import AccountStore, parse_cookie_csv
from capture import CaptureManager
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
    msgs = list(capture.load_messages(
        list(capture.iter_packets(outer))[0]))
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
          c1.get("DedeUserID") == "31373462" and
          c1.get("buvid3") == "BUVID-XYZ", c1)
    chrome = ("SESSDATA\tchrome-token\t.bilibili.com\t/\t1893456000\t0\t0\t0\t0\t0\t0\t0\t0\n"
              "DedeUserID\t42\t.bilibili.com\t/\t1893456000\n")
    c2 = parse_cookie_csv(chrome)
    check("Chrome 13 列 cookie 解析",
          c2.get("SESSDATA") == "chrome-token" and c2.get("DedeUserID") == "42", c2)
    c3 = parse_cookie_csv("name,value\nSESSDATA,csv-token\nbuvid3,AAA\n")
    check("逗号两列 cookie 解析", c3.get("SESSDATA") == "csv-token", c3)
    try:
        parse_cookie_csv("")
        check("空 CSV 被拒", False)
    except ValueError:
        check("空 CSV 被拒", True)

    # ---------- field mapping ----------
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
          sc["uname"] == "观众A" and sc["start_time"] == 1794672000, sc)

    head = [0] * 16
    head[4] = 1794672000000
    head[15] = {"user": {"base": {"name": "小明"}, "uid": 123,
                         "medal": {"name": "牌子", "level": 5}}}
    dm = capture.build_danmaku(22748536, [head, "弹幕内容", [123, "小明", 0]], now)
    check("弹幕字段映射", dm and dm["uid"] == 123 and dm["uname"] == "小明" and
          dm["text"] == "弹幕内容" and dm["event_time"] == 1794672000, dm)
    check("缺字段不崩", capture.build_danmaku(1, None, now) is None)
    ev = capture.event_row(22748536, "gift", {"uid": 7, "uname": "B",
                                              "gift": {"giftName": "辣条", "num": 3}}, now)
    check("事件摘要", "辣条" in ev["detail"], ev["detail"])

    # ---------- storage ----------
    tmp = tempfile.mkdtemp(prefix="lc-selftest-")
    try:
        st = Store(os.path.join(tmp, "capture.db"))
        run_id = st.start_run(22748536, {"room_id": 22748536, "protover": 2})
        dm["run_id"] = run_id
        dm["raw_json"] = "{}"
        check("弹幕入库", st.insert_danmaku([dm]) == 1)
        sc["run_id"] = run_id
        check("SC 入库", st.insert_sc(sc) is True)
        check("SC 按 id 去重", st.insert_sc(sc) is False)
        check("译文回填", st.update_sc_translation("991", "你好") is True)
        ev["run_id"] = run_id
        st.insert_event(ev)
        total, items = st.query("sc", limit=10)
        check("SC 查询（含 sc_id 排序）", total == 1 and
              items[0]["message_trans"] == "你好", (total, items))
        total, items = st.query("danmaku", q="弹幕", limit=10)
        check("弹幕检索", total == 1 and items[0]["text"] == "弹幕内容")
        check("事件计数", st.counts()["events"] == 1)
        check("库内计数", st.counts()["danmaku"] == 1 and st.counts()["sc"] == 1)
        name, ctype, csv_body = st.export("danmaku", "csv")
        check("CSV 导出带 BOM", name == "danmaku.csv" and
              csv_body.startswith("\ufeff") and "弹幕内容" in csv_body)
        _, _, js = st.export("sc", "json")
        check("JSON 导出", json.loads(js)["count"] == 1)
        st.end_run(run_id)

        # ---------- HTTP API ----------
        cfg = Config(os.path.join(tmp, "config.json"))
        logs = LogBuffer()
        logs.add("info", "selftest")
        manager = CaptureManager(st, cfg.get, lambda level, msg: logs.add(level, msg))
        srv = create_server("127.0.0.1", 0, tmp, os.path.join(HERE, "web"),
                            st, manager, cfg, logs)
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
        check("网页可访问", status == 200 and b"\xe7\x9b\xb4\xe6\x92\xad\xe5\xbc\xb9\xe5\xb9\x95" in html)
        status, raw = get("/api/health")
        check("健康检查", status == 200 and json.loads(raw)["ok"])
        status, raw = get("/api/status")
        j = json.loads(raw)
        check("状态接口", status == 200 and j["ok"] and
              j["capture"]["running"] is False and
              j["capture"]["storage"]["sc"] == 1)
        status, raw = get("/api/config")
        check("配置脱敏", json.loads(raw)["config"]["sessdata_set"] is False)
        status, raw = post("/api/config", {"room_id": 12345})
        check("保存配置", status == 200 and cfg.get()["room_id"] == 12345)
        post("/api/config", {"room_id": 22748536})
        status, raw = get("/api/danmaku?limit=10")
        check("弹幕接口", json.loads(raw)["total"] == 1)
        status, raw = get("/api/superchats?limit=10")
        check("SC 接口", json.loads(raw)["items"][0]["message_trans"] == "你好")
        status, raw = get("/api/events?limit=10")
        check("事件接口", json.loads(raw)["total"] == 1)
        status, raw = get("/api/export?type=sc&format=csv")
        check("导出接口", status == 200 and raw.startswith(b"\xef\xbb\xbf"))
        status, raw = get("/api/logs")
        check("日志接口", len(json.loads(raw)["items"]) >= 1)
        status, raw = post("/api/stop", {})
        j = json.loads(raw)
        check("停止接口立即返回 running=false",
              j["ok"] is True and j["capture"]["running"] is False,
              j.get("capture"))

        # ---------- 账号 API ----------
        csv_text = ("SESSDATA\ttest-sessdata\t.bilibili.com\t/\t1893456000\n"
                    "DedeUserID\t31373462\t.bilibili.com\t/\t1893456000\n")
        status, raw = get("/api/accounts")
        j = json.loads(raw)
        check("账号接口初始为空", status == 200 and j["items"] == [] and
              not j["active_id"], j)
        status, raw = post("/api/accounts", {"name": "main", "csv": csv_text})
        j = json.loads(raw)
        check("上传 CSV 新增账号并自动启用",
              status == 200 and len(j["accounts"]["items"]) == 1 and
              j["accounts"]["items"][0]["active"], j)
        check("账号列表不泄露 cookie 明文",
              b"test-sessdata" not in raw and
              bool(j["accounts"]["items"][0]["sessdata_mask"]), raw[:200])
        aid = j["accounts"]["items"][0]["id"]
        status, raw = post("/api/accounts",
                           {"name": "bad", "csv": "foo,bar\nbuvid3,AAA"})
        j = json.loads(raw)
        check("缺 SESSDATA 仍可保存但标错",
              bool(j["item"]["error"]) and not j["item"]["active"], j["item"])
        status, raw = post("/api/accounts/activate", {"id": ""})
        check("停用账号", json.loads(raw)["accounts"]["active_id"] == "")
        status, raw = post("/api/accounts/activate", {"id": aid})
        check("启用账号", json.loads(raw)["accounts"]["active_id"] == aid)
        status, raw = post("/api/accounts/remove", {"id": aid})
        check("删除账号", len(json.loads(raw)["accounts"]["items"]) == 1)
        status, raw = get("/api/logs?limit=500")
        check("日志上限接口可用", status == 200 and "items" in json.loads(raw))
        acc_path = os.path.join(tmp, "acc-persist.json")
        AccountStore(acc_path).add("persist", csv_text)
        check("账号持久化到 accounts.json",
              AccountStore(acc_path).active()["cookies"]["SESSDATA"] ==
              "test-sessdata")
        status, _ = get("/api/nope")
        check("未知接口 404", status == 404)

        post("/api/config", {"token": "abc"})
        status, _ = get("/api/status")
        check("口令拦截", status == 401)
        status, raw = get("/api/status", {"X-Access-Token": "abc"})
        check("口令放行", status == 200 and json.loads(raw)["ok"])
        post("/api/config", {"token": ""}, {"X-Access-Token": "abc"})

        status, raw = post("/api/clear", {"confirm": "yes"})
        check("清空接口", status == 200 and st.counts()["sc"] == 0)
        status, _ = post("/api/clear", {})
        check("清空需确认", status == 400)

        srv.shutdown()
        srv.server_close()

        # ---------- collector loop: mocked websocket, zero network ----------
        import asyncio
        import time

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
                await asyncio.sleep(0.5)
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

        business = [{"cmd": "DANMU_MSG",
                     "info": [head, "弹幕%d" % i, [123, "小明", 0]]}
                    for i in range(51)]
        business.append({"cmd": "SUPER_CHAT_MESSAGE", "data": {
            "id": "m1", "uid": 9, "price": 50, "message": "SC 原文",
            "start_time": 1794672000,
            "user_info": {"uname": "观众B"}, "medal_info": {}}})
        business.append({"cmd": "SUPER_CHAT_MESSAGE_JPN",
                         "data": {"id": "m1", "message_trans": "译文"}})
        inner = b"".join(capture.pack_packet(
            capture.OP_MESSAGE, json.dumps(m, ensure_ascii=False).encode("utf-8"))
            for m in business)
        frames = [capture.pack_packet(capture.OP_AUTH_REPLY, b'{"code":0}'),
                  capture.pack_packet(capture.OP_MESSAGE, zlib.compress(inner),
                                      header_ver=capture.PROTO_ZLIB)]

        async def fake_connect(url, **kwargs):
            return FakeWS(frames)

        old_api, old_connect = capture.BiliApi, capture.connect
        capture.BiliApi, capture.connect = FakeApi, fake_connect
        try:
            manager.accounts.add("main", csv_text)
            ok, message = manager.start()
            check("采集线程可启动", ok, message)
            for _ in range(30):
                if FakeApi.seen:
                    break
                time.sleep(0.05)
            check("启用账号的 cookie 注入采集配置",
                  bool(FakeApi.seen) and
                  FakeApi.seen[-1].get("_cookies", {}).get("SESSDATA") ==
                  "test-sessdata" and
                  FakeApi.seen[-1].get("sessdata") == "test-sessdata",
                  FakeApi.seen[-1] if FakeApi.seen else {})
            snap = manager.snapshot()
            for _ in range(40):
                time.sleep(0.1)
                snap = manager.snapshot()
                if snap["counts"]["danmaku"] >= 1 and snap["counts"]["sc"] >= 1:
                    break
            check("弹幕经采集链路入库", snap["counts"]["danmaku"] >= 50,
                  snap["counts"])
            check("SC 经采集链路入库", snap["counts"]["sc"] >= 1,
                  snap["counts"])
            total, rows = st.query("sc")
            check("JPN 译文回填", total == 1 and rows[0]["message_trans"] == "译文",
                  rows)
            check("房间信息回填", snap["title"] == "测试房间" and
                  snap["live_status"] == 1 and snap["real_room_id"] == 22748536)
            manager.stop()
            check("停止后状态复位", manager.snapshot()["running"] is False)
            check("停止时冲刷剩余缓冲", st.counts()["danmaku"] == 51,
                  st.counts())
        finally:
            capture.BiliApi, capture.connect = old_api, old_connect

        with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as fh:
            page = fh.read()
        check("网页含配置/启停/导出控件",
              all(k in page for k in ("f_room", "btnStart", "btnStop",
                                     "btnJson", "btnCsv", "/api/export")))
        check("网页拆成配置/数据/日志三页",
              all(k in page for k in ('id="page-config"', 'id="page-data"',
                                      'id="page-logs"', "acctFile", "btnUpload",
                                      "logBox", "streamBox", "btnViewTable")))
        check("网页含账号 API 与状态即时刷新",
              all(k in page for k in ("/api/accounts", "refreshSoon",
                                      "setRunningUI", "/api/accounts/activate")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d 项全部通过" % len(PASSED), flush=True)


if __name__ == "__main__":
    main()
