# -*- coding: utf-8 -*-
"""界面冒烟测试：offscreen 起窗口，验证模型与按钮状态，不弹任何东西。

没装 PySide6 时整组跳过，不影响其它测试。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:                                   # pragma: no cover
    HAVE_QT = False

from pctools.core import job


def _part(idx=0, **kw):
    data = dict(rec_id=1, idx=idx, label="场次", path="a.mp4",
                bytes=30 * 1024 * 1024, duration_ms=120000)
    data.update(kw)
    return job.Part(**data)


@unittest.skipUnless(HAVE_QT, "没装 PySide6")
class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_columns_and_rows(self):
        from pctools.ui.model import COLUMNS, PartsModel

        model = PartsModel()
        model.set_parts([_part(0), _part(1)])
        self.assertEqual(model.rowCount(), 2)
        self.assertEqual(model.columnCount(), len(COLUMNS))
        self.assertEqual(model.data(model.index(0, 1)), "1")
        self.assertEqual(model.data(model.index(1, 1)), "2")

    def test_duration_and_bitrate_text(self):
        from pctools.ui.model import PartsModel

        model = PartsModel()
        model.set_parts([_part(duration_ms=125000,
                               bytes=2_000_000)])
        self.assertEqual(model.data(model.index(0, 2)), "2:05")
        self.assertTrue(model.data(model.index(0, 3)).endswith("kbps"))

    def test_status_uses_estimate(self):
        from pctools.ui.model import PartsModel

        part = _part()
        part.est_bytes = part.bytes // 2
        model = PartsModel()
        model.set_parts([part])
        self.assertEqual(model.data(model.index(0, 5)), "15.0 MB")
        self.assertEqual(model.data(model.index(0, 6)), "50%")

    def test_totals(self):
        from pctools.ui.model import PartsModel

        done = _part(0)
        done.status, done.saved = "done", 5 * 1024 * 1024
        skip = _part(1)
        skip.status = "skip"
        model = PartsModel()
        model.set_parts([done, skip])
        count, size, saved, todo = model.totals()
        self.assertEqual(count, 2)
        self.assertEqual(saved, 5 * 1024 * 1024)
        self.assertEqual(todo, 0)


@unittest.skipUnless(HAVE_QT, "没装 PySide6")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_window_builds(self):
        from pctools.ui.main import MainWindow

        win = MainWindow()
        try:
            self.assertEqual(win.model.rowCount(), 0)
            # 没数据时只有「扫描」可点
            self.assertTrue(win.btn_scan.isEnabled())
            self.assertFalse(win.btn_run.isEnabled())
            self.assertFalse(win.btn_stop.isEnabled())

            win.parts = [_part()]
            win.model.set_parts(win.parts)
            win._update_buttons()
            self.assertTrue(win.btn_run.isEnabled())

            win._set_busy(True, "处理中")
            self.assertFalse(win.btn_scan.isEnabled())
            self.assertFalse(win.btn_run.isEnabled())
            win._end_busy()
            self.assertTrue(win.btn_scan.isEnabled())
        finally:
            win.deleteLater()

    def test_picked_prefers_selection(self):
        from pctools.ui.main import MainWindow

        win = MainWindow()
        try:
            a, b = _part(0), _part(1)
            a.status = "done"
            win.parts = [a, b]
            win.model.set_parts(win.parts)
            # 没选中 → 全部未完成的
            self.assertEqual([p.idx for _, p in win._picked()], [1])
            # 选中已完成那行 → 被过滤掉
            win.table.selectRow(0)
            self.assertEqual(win._picked(), [])
        finally:
            win.deleteLater()


if __name__ == "__main__":
    unittest.main()
