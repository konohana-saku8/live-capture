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
                 "/api/superchats", "/api/events"]
        for path in paths:
            with self.subTest(path=path):
                self.assertStatus(self.get(path), 200, path)

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

    def test_capture_stop_all(self):
        data = self.post("/api/capture/stop_all").get_json()
        self.assertIn("capture", data)
        self.assertIn("rooms", data)

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
