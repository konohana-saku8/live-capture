# -*- coding: utf-8 -*-
"""PC 端重编码回传接口的契约测试（不需要 ffmpeg、不需要手机）。

    .venv-host/Scripts/python.exe tests/test_recode_api.py -v

这里只用 test_client 打真实 HTTP 路由，ffprobe 被 mock 掉——真编码校验
由 pctools 的 verify 负责，接口这边只保证「字节写对了、该拒的拒了、
替换后数据库跟着变」。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app import create_app, shutdown           # noqa: E402
from app.api import recordings as api_mod      # noqa: E402

PROBE_OK = {"duration_ms": 60_100, "vcodec": "h264", "width": 1280,
            "height": 720, "fps": 60.0, "bytes": 0}


class RecodeApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lc-recode-")
        self.app = create_app(data_dir=self.tmp,
                              web_dir=os.path.join(ROOT, "web"))
        self.app.testing = True
        self.client = self.app.test_client()
        self.rt = self.app.extensions["live"]
        self.store = self.rt.store

        self.part_path = os.path.join(self.tmp, "recordings", "100", "p1.mp4")
        os.makedirs(os.path.dirname(self.part_path), exist_ok=True)
        with open(self.part_path, "wb") as fh:
            fh.write(b"O" * 4096)

        self.rec_id = self.store.create_recording(
            100, "test", 1_700_000_000_000, 250)
        self.store.update_recording(
            self.rec_id, status="ready",
            mp4_parts=[{"path": "recordings/100/p1.mp4", "started_at": 1,
                        "bytes": 4096, "duration_ms": 60_000}],
            bytes=4096, duration_ms=60_000)
        self.incoming = self.part_path + ".incoming"

    def tearDown(self):
        shutdown(self.app)

    # ---------- 工具 ----------

    def upload(self, data, offset):
        return self.client.post(
            "/api/recordings/%d/parts/0/upload" % self.rec_id, data=data,
            headers={"X-Chunk-Offset": str(offset)},
            content_type="application/octet-stream")

    def commit(self, total=None):
        headers = {}
        if total is not None:
            headers["X-Total-Size"] = str(total)
        return self.client.post(
            "/api/recordings/%d/parts/0/commit" % self.rec_id, headers=headers)

    def rec(self):
        return self.store.get_recording(self.rec_id)

    # ---------- 上传 ----------

    def test_upload_appends_and_tracks_offset(self):
        r = self.upload(b"A" * 1000, 0)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["offset"], 1000)

        r = self.upload(b"B" * 500, 1000)
        self.assertEqual(r.get_json()["offset"], 1500)
        self.assertEqual(os.path.getsize(self.incoming), 1500)
        # 原分片没被动过
        self.assertEqual(os.path.getsize(self.part_path), 4096)

    def test_upload_rejects_wrong_offset(self):
        self.upload(b"A" * 100, 0)
        r = self.upload(b"B" * 100, 5000)      # 谎报偏移
        self.assertEqual(r.status_code, 409)
        self.assertIn("不一致", r.get_json()["error"])
        self.assertEqual(os.path.getsize(self.incoming), 100)

    def test_upload_requires_offset_header(self):
        r = self.client.post("/api/recordings/%d/parts/0/upload" % self.rec_id,
                             data=b"x", content_type="application/octet-stream")
        self.assertEqual(r.status_code, 400)

    def test_upload_accepts_multi_megabyte_chunk(self):
        """1 MB 远大于旧的 512 KB 全局上限，确认这个路由单独放宽了。"""
        blob = b"Z" * (1024 * 1024)
        r = self.upload(blob, 0)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["offset"], len(blob))

    def test_upload_refuses_when_not_ready(self):
        self.store.update_recording(self.rec_id, status="recording")
        r = self.upload(b"A" * 10, 0)
        self.assertEqual(r.status_code, 409)
        self.assertIn("ready", r.get_json()["error"])

    def test_upload_refuses_missing_part(self):
        r = self.client.post(
            "/api/recordings/%d/parts/9/upload" % self.rec_id, data=b"x",
            headers={"X-Chunk-Offset": "0"},
            content_type="application/octet-stream")
        self.assertEqual(r.status_code, 404)

    # ---------- commit ----------

    def test_commit_replaces_file_and_updates_db(self):
        self.upload(b"N" * 2000, 0)
        with mock.patch.object(api_mod, "_probe_video", return_value=PROBE_OK):
            r = self.commit(total=2000)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["saved_bytes"], 4096 - 2000)
        self.assertTrue(body["part"]["recompressed_at"] > 0)

        self.assertEqual(os.path.getsize(self.part_path), 2000)
        self.assertFalse(os.path.exists(self.incoming))
        rec = self.rec()
        self.assertEqual(rec["bytes"], 2000)
        self.assertEqual(rec["duration_ms"], 60_100)

    def test_commit_rejects_duration_drift(self):
        self.upload(b"N" * 2000, 0)
        drift = dict(PROBE_OK, duration_ms=65_000)   # 差 5 秒，弹幕轴必错
        with mock.patch.object(api_mod, "_probe_video", return_value=drift):
            r = self.commit()
        self.assertEqual(r.status_code, 400)
        self.assertIn("弹幕轴", r.get_json()["error"])
        # 原文件与数据库都没变
        self.assertEqual(os.path.getsize(self.part_path), 4096)
        self.assertEqual(self.rec()["bytes"], 4096)
        self.assertTrue(os.path.exists(self.incoming))

    def test_commit_rejects_wrong_codec(self):
        self.upload(b"N" * 2000, 0)
        hevc = dict(PROBE_OK, vcodec="hevc")
        with mock.patch.object(api_mod, "_probe_video", return_value=hevc):
            r = self.commit()
        self.assertEqual(r.status_code, 400)
        self.assertIn("h264", r.get_json()["error"])

    def test_commit_rejects_size_mismatch(self):
        self.upload(b"N" * 2000, 0)
        with mock.patch.object(api_mod, "_probe_video", return_value=PROBE_OK):
            r = self.commit(total=9999)
        self.assertEqual(r.status_code, 409)

    def test_commit_without_data(self):
        r = self.commit()
        self.assertEqual(r.status_code, 409)

    def test_commit_fails_when_probe_unavailable(self):
        self.upload(b"N" * 2000, 0)
        with mock.patch.object(api_mod, "_probe_video", return_value=None):
            r = self.commit()
        self.assertEqual(r.status_code, 400)
        self.assertIn("ffprobe", r.get_json()["error"])

    # ---------- abort / 对账 ----------

    def test_abort_cleans_incoming(self):
        self.upload(b"A" * 300, 0)
        r = self.client.post("/api/recordings/%d/parts/0/abort" % self.rec_id)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.exists(self.incoming))
        self.assertEqual(os.path.getsize(self.part_path), 4096)

    def test_incoming_reports_progress(self):
        self.upload(b"A" * 700, 0)
        r = self.client.get("/api/recordings/%d/parts/0/incoming" % self.rec_id)
        body = r.get_json()
        self.assertEqual(body["offset"], 700)
        self.assertEqual(body["target_bytes"], 4096)
        self.assertEqual(body["duration_ms"], 60_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
