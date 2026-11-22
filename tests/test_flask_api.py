# -*- coding: utf-8 -*-
"""Flask 层的接口契约测试（不需要网络、不需要手机）。

    .venv-host/Scripts/python.exe tests/test_flask_api.py -v

它只验证「路由在不在、状态码对不对、返回结构有没有缺字段」，业务正确性
仍由 selftest.py 覆盖。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app import create_app, shutdown           # noqa: E402


class FlaskApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lcap-test-")
        self.app = create_app(data_dir=self.tmp,
                              web_dir=os.path.join(ROOT, "web"))
        self.app.testing = True
        self.client = self.app.test_client()
        self.rt = self.app.extensions["live"]

    def tearDown(self):
        shutdown(self.app)

    # ---------- 工具 ----------

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, payload=None, **kw):
        return self.client.post(path, json=payload if payload is not None else {}, **kw)

    def assertStatus(self, resp, code, label=""):
        self.assertEqual(resp.status_code, code,
                         "%s -> %s %s" % (label or resp.request.path,
                                          resp.status_code,
                                          resp.get_data(as_text=True)[:200]))

    # ---------- GET ----------

    def test_public_endpoints(self):
        for path in ("/api/health", "/api/gate"):
            with self.subTest(path=path):
                data = self.get(path).get_json()
                self.assertStatus(self.get(path), 200, path)
                self.assertTrue(data["ok"])

    def test_readonly_endpoints(self):
        paths = ["/api/status", "/api/dashboard", "/api/rooms", "/api/feed",
                 "/api/system", "/api/sessions", "/api/dynamics", "/api/config",
                 "/api/logs", "/api/accounts", "/api/danmaku",
                 "/api/superchats", "/api/events", "/api/recordings",
                 "/api/recordings/summary", "/api/notify"]
        for path in paths:
            with self.subTest(path=path):
                self.assertStatus(self.get(path), 200, path)

    def test_logs_file(self):
        log_dir = os.path.join(self.tmp, "logs")
        # Runtime 启动时已经建好 logs/（warn/error 会落 journal.jsonl）
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "service.log"), "w",
                  encoding="utf-8") as handle:
            handle.write("hello log\n")
        resp = self.get("/api/logs/file")
        self.assertStatus(resp, 200, "日志下载")
        self.assertIn("hello log", resp.get_data(as_text=True))

    def test_feed_shapes(self):
        for query in ("", "?paged=1&limit=10", "?all=1", "?types=danmaku"):
            with self.subTest(query=query):
                data = self.get("/api/feed" + query).get_json()
                self.assertIn("items", data)
                self.assertIn("counts", data)
                self.assertIn("paged", data)

    def test_config_hides_token(self):
        self.rt.config.save({"token": "topsecret9"})
        cfg = self.get("/api/config",
                       headers={"X-Access-Token": "topsecret9"}).get_json()["config"]
        self.assertTrue(cfg["token_set"])
        self.assertNotEqual(cfg["token"], "topsecret9")

    def test_feishu_webhook_is_masked(self):
        resp = self.post("/api/config",
                         {"feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef"})
        self.assertStatus(resp, 200, "保存 webhook")
        cfg = self.get("/api/config").get_json()["config"]
        self.assertTrue(cfg["feishu_webhook_set"])
        self.assertNotIn("abcdef", cfg["feishu_webhook"])
        # 通知状态能反映「已配置」
        status = self.get("/api/notify").get_json()["status"]
        self.assertTrue(status["enabled"])

    def test_notify_test_requires_webhook(self):
        resp = self.post("/api/notify/test", {})
        self.assertStatus(resp, 400, "没有 webhook 时不发请求")

    # ---------- 鉴权 ----------

    def test_token_required(self):
        self.assertStatus(self.get("/api/status"), 200, "未设口令")
        self.rt.config.save({"token": "abc123"})
        self.assertStatus(self.get("/api/status"), 401, "无口令")
        self.assertStatus(self.get("/api/status", headers={"X-Access-Token": "abc123"}),
                          200, "header 口令")
        self.assertStatus(self.get("/api/status?token=abc123"), 200, "query 口令")
        self.assertStatus(self.get("/api/health"), 200, "health 应免口令")
        self.assertStatus(self.get("/api/gate"), 200, "gate 应免口令")
        self.assertStatus(self.post("/api/clear", {"confirm": "yes"}), 401, "POST 也要口令")

    # ---------- POST 校验 ----------

    def test_room_settings_validation(self):
        self.assertStatus(self.post("/api/rooms/settings",
                                    {"room_id": 999, "params": {"live_only": True}}),
                          404, "房间不存在")
        self.assertStatus(self.post("/api/rooms/settings",
                                    {"room_id": 999, "params": []}),
                          400, "params 非对象")

    def test_room_settings_auto_record(self):
        item, _ = self.rt.rooms.add(22749009, "录制测试", {})
        resp = self.post("/api/rooms/settings",
                         {"room_id": item["id"], "params": {"auto_record": True}})
        self.assertStatus(resp, 200, "保存自动录制")
        data = resp.get_json()
        self.assertTrue(data["room"]["params"]["auto_record"])
        room = next(x for x in data["rooms"]["items"] if x["id"] == item["id"])
        self.assertTrue(room["auto_record"])

    def test_record_qn_validation(self):
        item, _ = self.rt.rooms.add(22749010, "清晰度测试", {})
        resp = self.post("/api/rooms/settings",
                         {"room_id": item["id"], "params": {"record_qn": 250}})
        self.assertStatus(resp, 200, "720P")
        self.assertEqual(resp.get_json()["room"]["params"]["record_qn"], 250)
        self.assertStatus(self.post("/api/rooms/settings",
                                    {"room_id": item["id"],
                                     "params": {"record_qn": 123}}), 400, "非法清晰度")

    # ---------- 录像 ----------

    def _make_recording(self, rid, start, name="a.mp4", body=b"0123456789",
                        rel_override=None):
        rec_id = self.rt.recordings.create(rid, "录像测试", start, 250,
                                           started_at=start)
        folder = os.path.join(self.tmp, "recordings", str(rid))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as fh:
            fh.write(body)
        rel = rel_override or self.rt.recordings.rel(path)
        self.rt.store.update_recording(
            rec_id, status="ready", bytes=len(body),
            mp4_parts=[{"path": rel, "started_at": start,
                        "bytes": len(body), "duration_ms": 1000}],
            flv_parts=[])
        return rec_id

    def test_recordings_list_and_media(self):
        item, _ = self.rt.rooms.add(22749011, "录像列表", {})
        rid = item["id"]
        rec_id = self._make_recording(rid, 1794700000000)
        data = self.get("/api/recordings?room_id=%d" % rid).get_json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["id"], rec_id)
        self.assertEqual(data["items"][0]["mp4_count"], 1)
        self.assertEqual(data["summary"]["count"], 1)
        self.assertEqual(data["summary"]["total_bytes"], 10)

        full = self.get("/api/recordings/%d/media?kind=mp4" % rec_id)
        self.assertStatus(full, 200, "整体")
        self.assertEqual(full.data, b"0123456789")
        part = self.get("/api/recordings/%d/media?kind=mp4" % rec_id,
                        headers={"Range": "bytes=0-3"})
        self.assertStatus(part, 206, "Range")
        self.assertEqual(part.data, b"0123")

    def test_recordings_path_guard(self):
        item, _ = self.rt.rooms.add(22749012, "录像越权", {})
        rec_id = self._make_recording(item["id"], 1794700000000,
                                      rel_override="../escape.mp4")
        self.assertStatus(self.get("/api/recordings/%d/media?kind=mp4" % rec_id),
                          400, "路径越权")
        self.assertStatus(self.get("/api/recordings/999999/media"), 404, "不存在")
        self.assertStatus(self.post("/api/recordings/999999/remux"), 404, "重试不存在")

    def test_delete_single_video_parts(self):
        rid, start, _end = self._seed_session()
        rec_id = self._make_recording(rid, start, name="a.mp4")
        folder = os.path.join(self.tmp, "recordings", str(rid))
        second = os.path.join(folder, "b.mp4")
        flv = os.path.join(folder, "c.flv")
        with open(second, "wb") as fh:
            fh.write(b"2222")
        with open(flv, "wb") as fh:
            fh.write(b"flv-data")
        self.rt.store.update_recording(
            rec_id,
            mp4_parts=[
                {"path": self.rt.recordings.rel(os.path.join(folder, "a.mp4")),
                 "started_at": start, "bytes": 10, "duration_ms": 1000},
                {"path": self.rt.recordings.rel(second),
                 "started_at": start + 1, "bytes": 4, "duration_ms": 1000},
            ],
            flv_parts=[{"path": self.rt.recordings.rel(flv),
                        "started_at": start, "bytes": 8, "duration_ms": 1000}])

        resp = self.post("/api/recordings/%d/file/delete" % rec_id,
                         {"kind": "mp4", "index": 0})
        self.assertStatus(resp, 200, "删一个分 P")
        self.assertEqual(resp.get_json()["deleted"]["files"], 1)
        self.assertFalse(os.path.exists(os.path.join(folder, "a.mp4")))
        self.assertTrue(os.path.exists(second))
        rec = self.rt.store.get_recording(rec_id)
        self.assertEqual(len(rec["mp4_parts"]), 1)
        self.assertEqual(len(rec["flv_parts"]), 1)

        resp = self.post("/api/recordings/%d/file/delete" % rec_id,
                         {"kind": "flv", "index": 0})
        self.assertStatus(resp, 200, "删 FLV 原片")
        self.assertFalse(os.path.exists(flv))

        segs = self.get("/api/sessions?room_id=%d&with_recordings=1" % rid
                        ).get_json()["segments"]
        rec = next(s["recording"] for s in segs if s.get("recording"))
        self.assertEqual(len(rec["mp4_parts"]), 1)
        self.assertEqual(rec["mp4_parts"][0]["name"], "b.mp4")
        self.assertEqual(rec["flv_count"], 0)

    def test_session_delete_removes_video_too(self):
        rid, start, end = self._seed_session()
        rec_id = self._make_recording(rid, start)
        folder = os.path.join(self.tmp, "recordings", str(rid))
        mp4 = os.path.join(folder, "a.mp4")
        flv = os.path.join(folder, "d.flv")
        with open(flv, "wb") as fh:
            fh.write(b"flv-data")
        self.rt.store.update_recording(
            rec_id, flv_parts=[{"path": self.rt.recordings.rel(flv),
                                "started_at": start, "bytes": 8,
                                "duration_ms": 1000}])

        resp = self.post("/api/session/delete",
                         {"room_id": rid, "start": start, "end": end,
                          "kind": "live"})
        self.assertStatus(resp, 200, "整场删除")
        video = resp.get_json()["deleted"]["video"]
        self.assertEqual(video["recordings"], 1)
        self.assertEqual(video["files"], 2)
        self.assertFalse(os.path.exists(mp4))
        self.assertFalse(os.path.exists(flv))
        self.assertIsNone(self.rt.store.get_recording(rec_id))

    def test_sessions_attach_recordings(self):
        rid, start, _end = self._seed_session()
        rec_id = self._make_recording(rid, start)
        segs = self.get("/api/sessions?room_id=%d&with_recordings=1" % rid).get_json()
        self.assertEqual(segs["segments"][0]["recording"]["id"], rec_id)

    def test_data_rooms_include_rooms_without_collector(self):
        """数据页的房间清单要含「采集器已删、数据还在」的房间。"""
        rid = 22749077
        self.rt.store.insert_danmaku([
            {"room_id": rid, "uid": 1, "uname": "a", "text": "1",
             "received_at": 1794700000000}])
        data = self.get("/api/data/rooms").get_json()
        self.assertStatus(self.get("/api/data/rooms"), 200, "数据页房间清单")
        rooms = {item["id"]: item for item in data["items"]}
        self.assertIn(rid, rooms)
        self.assertFalse(rooms[rid]["configured"])
        self.assertTrue(rooms[rid]["has_data"])
        self.assertEqual(rooms[rid]["totals"]["danmaku"], 1)
        # 采集器清单里没有它，两个接口各管各的
        self.assertNotIn(rid, [r["id"] for r in self.get("/api/rooms").get_json()["items"]])

    def test_feed_asc_after(self):
        rid = 22749013
        self.rt.store.insert_danmaku([
            {"room_id": rid, "uid": 1, "uname": "a", "text": "1", "received_at": 100},
            {"room_id": rid, "uid": 2, "uname": "b", "text": "2", "received_at": 200},
            {"room_id": rid, "uid": 3, "uname": "c", "text": "3", "received_at": 300},
        ])
        data = self.get("/api/feed?paged=1&asc=1&after=150&limit=10&room_id=%d" % rid
                        ).get_json()
        self.assertTrue(data["asc"])
        self.assertEqual([r["text"] for r in data["items"]["danmaku"]], ["2", "3"])

    def test_dynamics_config_needs_room(self):
        resp = self.post("/api/dynamics/config", {"dyn_auto": True})
        self.assertStatus(resp, 400, "缺 room_id")
        self.assertIn("room_id", resp.get_json()["error"])

    def test_clear_needs_confirm(self):
        self.assertStatus(self.post("/api/clear", {}), 400, "缺 confirm")

    def test_config_bounds(self):
        self.assertStatus(self.post("/api/config", {"port": 9000}), 200, "合法端口")
        self.assertStatus(self.post("/api/config", {"port": 99999}), 400, "端口越界")
        self.assertStatus(self.post("/api/config", {"protover": 9}), 400, "协议越界")

    def test_config_record_enter(self):
        self.assertStatus(self.post("/api/config", {"record_enter": True}), 200)
        cfg = self.get("/api/config").get_json()["config"]
        self.assertTrue(cfg["record_enter"])
        self.assertStatus(self.post("/api/config", {"record_enter": False}), 200)
        self.assertFalse(self.get("/api/config").get_json()["config"]["record_enter"])

    def test_capture_stop_all(self):
        data = self.post("/api/capture/stop_all").get_json()
        self.assertIn("capture", data)
        self.assertIn("rooms", data)

    # ---------- 场次删除 ----------

    def _seed_session(self):
        """造一场完整的直播数据：开播 → 弹幕/SC/事件 → 下播。"""
        rt = self.rt
        st = rt.store
        rid = 22749001
        start, end = 1794700000000, 1794703600000
        st.insert_event({"room_id": rid, "kind": "live", "detail": "开播",
                         "event_time": start // 1000, "received_at": start,
                         "raw_json": "{}"})
        st.insert_danmaku([{"room_id": rid, "uid": 1, "uname": "a",
                            "text": "hi", "received_at": start + 1000},
                           {"room_id": rid, "uid": 2, "uname": "b",
                            "text": "yo", "received_at": start + 2000},
                           {"room_id": rid, "uid": 3, "uname": "c",
                            "text": "keep", "received_at": end + 60000}])
        st.insert_event({"room_id": rid, "kind": "live", "detail": "下播",
                         "event_time": end // 1000, "received_at": end,
                         "raw_json": "{}"})
        return rid, start, end

    def test_session_delete_validation(self):
        self.assertStatus(self.post("/api/session/delete", {"start": 1}), 400,
                          "缺 room_id")
        self.assertStatus(self.post("/api/session/delete",
                                    {"room_id": 1, "start": 1}), 400, "缺 kind")
        self.assertStatus(self.post("/api/session/delete",
                                    {"room_id": 1, "start": 1, "kind": "bad"}),
                          400, "kind 非法")
        self.assertStatus(self.post("/api/session/delete",
                                    {"room_id": 1, "start": 2, "end": 1,
                                     "kind": "live"}), 400, "end 早于 start")

    def test_session_delete_live(self):
        rid, start, end = self._seed_session()
        resp = self.post("/api/session/delete",
                         {"room_id": rid, "start": start, "end": end,
                          "kind": "live"})
        self.assertStatus(resp, 200)
        d = resp.get_json()["deleted"]
        self.assertEqual(d["danmaku"], 2)
        self.assertEqual(d["events"], 2)          # 开播 + 下播标记一起删
        segs = self.get("/api/sessions?room_id=%d" % rid).get_json()["segments"]
        self.assertEqual(len(segs), 1)            # 标记删光 → 归「未分段」
        self.assertEqual(segs[0].get("single"), True)
        rows = self.rt.store.query("danmaku", limit=10, room_id=rid)[1]
        self.assertEqual([r["text"] for r in rows], ["keep"])

    def test_session_delete_gap_keeps_marks(self):
        rid, start, end = self._seed_session()
        resp = self.post("/api/session/delete",
                         {"room_id": rid, "start": start, "end": end,
                          "kind": "gap"})
        self.assertStatus(resp, 200)
        d = resp.get_json()["deleted"]
        self.assertEqual(d["danmaku"], 2)
        self.assertEqual(d["events"], 0)          # 开播/下播标记保留
        segs = self.get("/api/sessions?room_id=%d" % rid).get_json()["segments"]
        self.assertEqual(len(segs), 2)            # 场还在（内容空了）+ 场间
        self.assertEqual(segs[0]["kind"], "live")
        self.assertEqual(segs[0]["danmaku"], 0)
        self.assertEqual(segs[1]["danmaku"], 1)   # 「keep」留在场间

    def test_live_segment_includes_end_mark(self):
        """下播事件属于这一场，不落到随后的场间。"""
        rid, start, end = self._seed_session()
        segs = self.get("/api/sessions?room_id=%d" % rid).get_json()["segments"]
        live = next(s for s in segs if s["kind"] == "live")
        self.assertEqual(live["end"], end + 1)
        data = self.get(
            "/api/feed?paged=1&asc=1&types=events&room_id=%d&since=%d&until=%d"
            % (rid, start, live["end"])).get_json()
        details = [r["detail"] for r in data["items"]["events"]]
        self.assertIn("开播", details)
        self.assertIn("下播", details)

    def test_export_until(self):
        """导出按左闭右开区间：until 正好在下播时刻时不含下播事件。"""
        rid, start, end = self._seed_session()
        before = self.get("/api/export?type=events&format=json&room_id=%d&until=%d"
                          % (rid, end)).get_json()
        with_mark = self.get(
            "/api/export?type=events&format=json&room_id=%d&until=%d"
            % (rid, end + 1)).get_json()
        self.assertEqual(len(before["items"]), 1)
        self.assertEqual([r["detail"] for r in before["items"]], ["开播"])
        self.assertEqual(len(with_mark["items"]), 2)

    def test_bad_json_body(self):
        resp = self.client.post("/api/config", data="{not json",
                                content_type="application/json")
        self.assertStatus(resp, 400, "坏 JSON")

    def test_body_type_must_be_object(self):
        resp = self.client.post("/api/config", data="[1,2]",
                                content_type="application/json")
        self.assertStatus(resp, 400, "数组 body")

    # ---------- 静态与 SPA ----------

    def test_index_and_spa_fallback(self):
        self.assertStatus(self.get("/"), 200, "首页")
        self.assertStatus(self.get("/rooms"), 200, "SPA fallback")
        self.assertStatus(self.get("/api/not-exist"), 404, "未知接口不该回 HTML")

    def test_cors_headers(self):
        resp = self.get("/api/health")
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store")

    def test_path_traversal_blocked(self):
        resp = self.get("/../run.py")
        self.assertIn(resp.status_code, (200, 404))
        body = resp.get_data(as_text=True)
        self.assertNotIn("Live danmaku", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
