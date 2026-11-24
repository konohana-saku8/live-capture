# -*- coding: utf-8 -*-
"""测试源（uid=0 的假房间）不该被任何外呼路径碰到。

2026-11-24 线上日志被「开播探测失败（999999999）」刷了 2300 多条：本地测试源
写进了正式 rooms.json，而 check_live 只看 notify_live，不认 uid=0。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.notifier import NotifierScheduler      # noqa: E402
from rooms import is_test_room                  # noqa: E402


class _Rooms:
    def __init__(self, items):
        self._items = items

    def list(self):
        return {"ok": True, "items": [dict(x) for x in self._items]}


class _Config:
    def get(self):
        return {}


class _Logs:
    def __init__(self):
        self.items = []

    def add(self, level, message, **kwargs):
        self.items.append((level, message))


def _notifier(tmp, rooms, probe):
    return NotifierScheduler(store=None, config=_Config(), logs=_Logs(),
                             rooms=_Rooms(rooms), data_dir=tmp,
                             live_probe=probe)


class TestSourceRoom(unittest.TestCase):
    def test_uid_zero_is_test_room(self):
        self.assertTrue(is_test_room({"uid": 0}))
        self.assertTrue(is_test_room({"uid": None}))
        self.assertTrue(is_test_room({}))
        self.assertFalse(is_test_room({"uid": 2094031249}))

    def test_check_live_skips_test_room(self):
        probed = []

        def probe(rid):
            probed.append(rid)
            return {"live_status": 0, "live_time": 0}

        rooms = [{"id": 999999999, "uid": 0, "notify_live": True},
                 {"id": 22748536, "uid": 2094031249, "notify_live": True}]
        with tempfile.TemporaryDirectory() as tmp:
            sent = _notifier(tmp, rooms, probe).check_live()
        self.assertEqual(sent, 0)
        self.assertEqual(probed, [22748536])

    def test_check_live_skips_realtime_off_when_uid_zero(self):
        """uid=0 且关了 notify_live，两条路都不该探。"""
        probed = []

        def probe(rid):
            probed.append(rid)
            return {"live_status": 0, "live_time": 0}

        rooms = [{"id": 999999999, "uid": 0, "notify_live": False}]
        with tempfile.TemporaryDirectory() as tmp:
            _notifier(tmp, rooms, probe).check_live()
        self.assertEqual(probed, [])


if __name__ == "__main__":
    unittest.main()
