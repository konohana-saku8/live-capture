# -*- coding: utf-8 -*-
"""pctools/core 的纯逻辑测试（不需要 ffmpeg，也不需要 PySide6）。"""
import unittest

from pctools.core import encoder, job
from pctools.core.api import human_size


class FakeClient:
    def __init__(self, items):
        self.items = items

    def recordings(self):
        return self.items


def _rec(**kw):
    base = {"id": 1, "status": "ready", "label": "测试场",
            "mp4_parts": [{"path": "a.mp4", "bytes": 18000000,
                           "duration_ms": 60000}]}
    base.update(kw)
    return base


class ScanTests(unittest.TestCase):
    def test_expands_one_row_per_part(self):
        rec = _rec(mp4_parts=[
            {"path": "a.mp4", "bytes": 100, "duration_ms": 1000},
            {"path": "b.mp4", "bytes": 200, "duration_ms": 1000},
        ])
        parts = job.scan(FakeClient([rec]))
        self.assertEqual([p.idx for p in parts], [0, 1])
        self.assertEqual(parts[1].path, "b.mp4")

    def test_skips_non_ready_recordings(self):
        parts = job.scan(FakeClient([_rec(status="recording"),
                                     _rec(id=2, status="remuxing")]))
        self.assertEqual(parts, [])

    def test_low_bitrate_marked_skip(self):
        # 100000 B / 60 s ≈ 13 kbps，远低于默认 1200 kbps 门槛
        rec = _rec(mp4_parts=[{"path": "a.mp4", "bytes": 100000,
                               "duration_ms": 60000}])
        parts = job.scan(FakeClient([rec]), min_kbps=1200)
        self.assertEqual(parts[0].status, "skip")
        self.assertIn("kbps", parts[0].message)

    def test_no_threshold_keeps_pending(self):
        parts = job.scan(FakeClient([_rec()]), min_kbps=0)
        self.assertEqual(parts[0].status, "pending")

    def test_high_bitrate_not_skipped(self):
        rec = _rec(mp4_parts=[{"path": "a.mp4", "bytes": 30 * 1024 * 1024,
                               "duration_ms": 60000}])
        parts = job.scan(FakeClient([rec]), min_kbps=1200)
        self.assertEqual(parts[0].status, "pending")
        self.assertGreater(parts[0].kbps, 1200)


class PartTests(unittest.TestCase):
    def test_kbps_uses_duration(self):
        part = job.Part(rec_id=1, idx=0, label="x", path="a",
                        bytes=1_000_000, duration_ms=8000)
        self.assertEqual(part.kbps, 1000)          # 1 MB / 8 s = 1000 kbps

    def test_kbps_zero_duration(self):
        part = job.Part(rec_id=1, idx=0, label="", path="a",
                        bytes=100, duration_ms=0)
        self.assertEqual(part.kbps, 0)

    def test_saving_ratio(self):
        part = job.Part(rec_id=1, idx=0, label="", path="a",
                        bytes=1000, duration_ms=1000)
        part.est_bytes = 400
        self.assertAlmostEqual(part.saving_ratio, 0.6)

    def test_title_carries_key_numbers(self):
        part = job.Part(rec_id=7, idx=2, label="场次", path="a",
                        bytes=1048576, duration_ms=125000)
        text = part.title
        self.assertIn("第3片", text)
        self.assertIn("2:05", text)


class EncoderTests(unittest.TestCase):
    def test_quality_drops_two_for_hardware(self):
        self.assertEqual(encoder.quality("x264", 23), 23)
        self.assertEqual(encoder.quality("nvenc", 23), 21)
        self.assertEqual(encoder.quality("qsv", 23), 21)
        self.assertEqual(encoder.quality("nvenc", 1), 0)   # 不会变负数

    def test_build_keeps_frame_count(self):
        for kind in ("nvenc", "qsv", "amf", "x264"):
            cmd = encoder.build("ffmpeg", "in.mp4", "out.mp4", encoder=kind,
                                crf=23, max_bitrate=1600, audio_bitrate=96)
            joined = " ".join(cmd)
            # 帧数与帧时序必须和源一致，否则手机端 commit 会按时长拒收
            self.assertIn("-fps_mode passthrough", joined)
            self.assertNotIn(" -r ", joined)
            self.assertNotIn("-vsync", joined)

    def test_build_caps_bitrate(self):
        cmd = encoder.build("ffmpeg", "a", "b", encoder="x264",
                            max_bitrate=1600)
        self.assertIn("-maxrate", cmd)
        self.assertIn("1600k", cmd)
        self.assertIn("3200k", cmd)       # bufsize = 2 × maxrate

    def test_build_without_cap(self):
        cmd = encoder.build("ffmpeg", "a", "b", encoder="x264", max_bitrate=0)
        self.assertNotIn("-maxrate", cmd)

    def test_pick_prefers_requested(self):
        self.assertEqual(encoder.pick(["x264", "nvenc"], "x264"), "x264")
        self.assertEqual(encoder.pick(["x264"], "nvenc"), "x264")
        self.assertEqual(encoder.pick([], "auto"), "")

    def test_detect_without_ffmpeg(self):
        self.assertEqual(encoder.detect(""), [])


class HumanSizeTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1024), "1.0 KB")
        self.assertEqual(human_size(1536), "1.5 KB")
        self.assertEqual(human_size(1024 ** 3 * 2), "2.0 GB")


if __name__ == "__main__":
    unittest.main()
