# -*- coding: utf-8 -*-
"""开播 / 新动态推送：卡片、签名、判重逻辑。

    .venv-host/Scripts/python.exe tests/test_notify.py -v

不发真实请求：urlopen 用假的，记录每次发出的 JSON。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import notify                                        # noqa: E402
from app.notifier import NotifierScheduler            # noqa: E402
from app.settings import Config                       # noqa: E402


class _Resp:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, *a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Spy:
    """假 urlopen：记录请求体，返回飞书成功响应。"""

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(json.loads(req.data.decode("utf-8")))
        return _Resp(b'{"code":0,"msg":"success"}')


class _Rooms:
    def __init__(self, items):
        self.items = items

    def list(self):
        return {"ok": True, "items": [dict(x) for x in self.items]}


class _Logs:
    def __init__(self):
        self.items = []

    def add(self, level, msg, exc=None, src=""):
        self.items.append((level, msg))


class NotifyUnitTest(unittest.TestCase):
    def test_sign_is_deterministic_and_secret_sensitive(self):
        a = notify.sign_feishu("s3cret", ts=1700000000)
        b = notify.sign_feishu("s3cret", ts=1700000000)
        c = notify.sign_feishu("other", ts=1700000000)
        self.assertEqual(a, b)
        self.assertNotEqual(a["sign"], c["sign"])
        self.assertEqual(a["timestamp"], "1700000000")

    def test_card_payloads(self):
        live = notify.live_card("火羽ひのめ", 22748536, "测试标题")
        self.assertEqual(live["msg_type"], "interactive")
        self.assertIn("22748536", json.dumps(live, ensure_ascii=False))
        self.assertEqual(live["card"]["elements"][-1]["actions"][0]["url"],
                         "https://live.bilibili.com/22748536")

        dyn = notify.dynamic_card("火羽ひのめ", 2094031249, "今天也播")
        self.assertEqual(dyn["card"]["header"]["template"], "blue")
        self.assertIn("2094031249", dyn["card"]["elements"][-1]["actions"][0]["url"])

    def test_send_requires_webhook(self):
        with self.assertRaises(notify.NotifyError):
            notify.send("", {"msg_type": "text"})

    def test_send_attaches_signature(self):
        spy = _Spy()
        notify.send_text("https://example.invalid/hook", "hi", "s3cret",
                         urlopen=spy)
        self.assertEqual(len(spy.calls), 1)
        self.assertIn("sign", spy.calls[0])
        self.assertIn("timestamp", spy.calls[0])


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lcap-notify-")
        self.spy = _Spy()
        self.logs = _Logs()
        self.probe_calls = []
        self.rooms_items = [{"id": 22748536, "uid": 2094031249,
                             "uname": "火羽ひのめ",
                             "notify_live": True, "notify_dynamic": True,
                             "collect_dynamics": True}]
        self.store_rows = []
        self.briefs = []
        self.config = Config(os.path.join(self.tmp, "config.json"))
        self.config.save({"feishu_webhook": "https://example.invalid/hook"})

    def _make(self):
        dyn = type("D", (), {
            "pull_one": lambda _self, mid, reason="": (1, 1)})()
        return NotifierScheduler(_Store(self.store_rows), self.config,
                                 self.logs, _Rooms(self.rooms_items), self.tmp,
                                 dynamics=dyn, live_probe=self._probe,
                                 urlopen=self.spy)

    def _probe(self, rid):
        self.probe_calls.append(rid)
        return self.briefs.pop(0) if self.briefs else {
            "live_status": 0, "live_time": 0, "uname": "", "title": ""}

    # ---------- 开播 ----------

    def test_live_baseline_then_new_session(self):
        n = self._make()
        # 首次看到已在播：只记基线，不推
        self.briefs = [{"live_status": 1, "live_time": 100,
                        "uname": "火羽ひのめ", "title": "T"}]
        self.assertEqual(n.check_live(), 0)
        self.assertEqual(len(self.spy.calls), 0)
        # 同一场再探：不重复推
        self.briefs = [{"live_status": 1, "live_time": 100,
                        "uname": "火羽ひのめ", "title": "T"}]
        self.assertEqual(n.check_live(), 0)
        # 下播
        self.briefs = [{"live_status": 0, "live_time": 0,
                        "uname": "火羽ひのめ", "title": ""}]
        self.assertEqual(n.check_live(), 0)
        # 新一场：推一次
        self.briefs = [{"live_status": 1, "live_time": 200,
                        "uname": "火羽ひのめ", "title": "新一场"}]
        self.assertEqual(n.check_live(), 1)
        self.assertEqual(len(self.spy.calls), 1)
        self.assertIn("火羽ひのめ", json.dumps(self.spy.calls[0],
                                             ensure_ascii=False))

    def test_live_skips_zero_live_time(self):
        n = self._make()
        self.briefs = [{"live_status": 1, "live_time": 0,
                        "uname": "x", "title": "y"}]
        self.assertEqual(n.check_live(), 0)
        self.assertEqual(len(self.spy.calls), 0)

    def test_live_respects_per_room_flag(self):
        self.rooms_items[0]["notify_live"] = False
        n = self._make()
        self.briefs = [{"live_status": 1, "live_time": 100,
                        "uname": "x", "title": "y"}]
        self.assertEqual(n.check_live(), 0)
        # 连探测都不该发生
        self.assertEqual(self.probe_calls, [])

    # ---------- 动态 ----------

    def test_dynamic_baseline_then_new(self):
        n = self._make()
        self.store_rows[:] = [{"dyn_id": "1", "pub_ts": 1000, "text": "旧"}]
        self.assertEqual(n.check_dynamics(), 0)
        self.assertEqual(len(self.spy.calls), 0)
        self.store_rows[:] = [{"dyn_id": "2", "pub_ts": 2000, "text": "新动态"},
                              {"dyn_id": "1", "pub_ts": 1000, "text": "旧"}]
        self.assertEqual(n.check_dynamics(), 1)
        self.assertEqual(len(self.spy.calls), 1)
        self.assertIn("新动态", json.dumps(self.spy.calls[0], ensure_ascii=False))
        # 再跑一次，没有更新的就不会重复推
        self.assertEqual(n.check_dynamics(), 0)

    def test_nothing_happens_without_webhook(self):
        self.config.save({"feishu_webhook": ""})
        n = self._make()
        n.tick()
        self.assertEqual(self.spy.calls, [])
        self.assertFalse(n.status()["enabled"])

    def test_state_survives_restart(self):
        n = self._make()
        self.briefs = [{"live_status": 1, "live_time": 100,
                        "uname": "x", "title": "y"}]
        n.check_live()                        # 记基线
        again = self._make()                 # 新实例读回状态文件
        self.briefs = [{"live_status": 1, "live_time": 100,
                        "uname": "x", "title": "y"}]
        self.assertEqual(again.check_live(), 0)   # 同一场不重复推


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list_dynamics(self, limit=20, mid=0):
        return len(self.rows), list(self.rows)[:limit]


if __name__ == "__main__":
    unittest.main(verbosity=2)
