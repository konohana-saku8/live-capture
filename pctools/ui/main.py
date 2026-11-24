# -*- coding: utf-8 -*-
"""主窗口：连接 → 扫描 → 选片 → 试算 / 执行。

所有耗时动作都在 QThread 里，界面线程只负责画。
"""
from __future__ import annotations

import html
import os
import time

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QSplitter, QTableView, QVBoxLayout, QWidget,
)

from ..core import config, encoder
from ..core.api import human_size
from .model import PartsModel
from .worker import RunWorker, ScanWorker

# 深色主题下浅色文字要更亮，两套分开
LOG_COLORS = {
    False: {"info": "#374151", "warn": "#a16207", "error": "#b91c1c",
            "ok": "#15803d", "debug": "#6b7280"},
    True: {"info": "#d1d5db", "warn": "#fbbf24", "error": "#f87171",
           "ok": "#4ade80", "debug": "#9ca3af"},
}

ENCODER_CHOICES = [("auto", "自动（优先硬件）")] + [
    (k, encoder.LABELS[k]) for k in encoder.ORDER]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("直播录像重压 · PC 工具")
        self.resize(1200, 760)

        self.cfg = config.load()
        self.client = None
        self.parts: list = []
        self.scan_worker: ScanWorker | None = None
        self.run_worker: RunWorker | None = None
        self._busy_flag = False
        self._stopping = False
        self._total = 0
        self._done = 0

        self._build_ui()
        self._load_cfg()
        self._probe_ffmpeg()
        self._update_buttons()
        self._log("info", "把手机连上 USB（或确认同一个 Wi-Fi），然后点「连接并扫描」。")

    # ------------------------------------------------------------------ 界面

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(9)

        outer.addWidget(self._build_conn())
        outer.addWidget(self._build_encode())

        self.model = PartsModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setSortingEnabled(False)

        header = self.table.horizontalHeader()
        # 场次可拖，数值列按内容，状态列吃剩余宽度（失败原因常很长）
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 150)
        for col in range(1, 7):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.Stretch)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        self.log_view.setPlaceholderText("这里显示每一步的进展")
        self.log_view.setStyleSheet(self._log_qss())

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setSizes([380, 250])
        outer.addWidget(split, 1)

        bar = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        self.progress.setFormat("就绪")
        self.summary = QLabel("尚未扫描")
        self.summary.setStyleSheet("color:#6b7280")
        bar.addWidget(self.progress, 1)
        bar.addWidget(self.summary)
        outer.addLayout(bar)

        btns = QHBoxLayout()
        self.lbl_link = QLabel("未连接")
        self.lbl_link.setStyleSheet("color:#6b7280")
        btns.addWidget(self.lbl_link, 1)

        self.btn_scan = QPushButton("扫描")
        self.btn_scan.setToolTip("连接服务并列出所有可压的分片")
        self.btn_estimate = QPushButton("试算选中")
        self.btn_estimate.setToolTip("只下载并用 30 秒样本估算能省多少，不编码不回传")
        self.btn_run = QPushButton("开始压缩")
        self.btn_run.setToolTip("未选中任何行时处理全部可压缩的分片")
        self.btn_run.setDefault(True)
        self.btn_stop = QPushButton("停止")
        for btn in (self.btn_scan, self.btn_estimate, self.btn_run, self.btn_stop):
            btn.setMinimumWidth(96)
            btns.addWidget(btn)
        outer.addLayout(btns)

        self.btn_scan.clicked.connect(self._scan)
        self.btn_estimate.clicked.connect(lambda: self._start(dry=True))
        self.btn_run.clicked.connect(lambda: self._start(dry=False))
        self.btn_stop.clicked.connect(self._stop)

        self._apply_theme()

    def _build_conn(self) -> QGroupBox:
        box = QGroupBox("连接")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self.ed_host = QLineEdit()
        self.ed_host.setPlaceholderText("127.0.0.1:8765")
        self.ed_host.setToolTip("填 127.0.0.1 时会自动尝试 adb 端口转发（USB 直连，37 MB/s）")
        self.ed_token = QLineEdit()
        self.ed_token.setEchoMode(QLineEdit.Password)
        self.ed_token.setPlaceholderText("手机 data/config.json 里的 token，没设就留空")
        self.ed_fallback = QLineEdit()
        self.ed_fallback.setPlaceholderText("adb 不可用时的 Wi-Fi 地址，例如 192.168.10.107:8765")

        grid.addWidget(QLabel("服务地址"), 0, 0)
        grid.addWidget(self.ed_host, 0, 1)
        grid.addWidget(QLabel("口令"), 0, 2)
        grid.addWidget(self.ed_token, 0, 3)
        grid.addWidget(QLabel("备用地址"), 1, 0)
        grid.addWidget(self.ed_fallback, 1, 1, 1, 3)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 2)
        return box

    def _build_encode(self) -> QGroupBox:
        box = QGroupBox("编码")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self.ed_ffmpeg = QLineEdit()
        self.ed_ffmpeg.setReadOnly(True)
        self.btn_ffmpeg = QPushButton("选择…")
        self.btn_ffmpeg.setFixedWidth(72)

        self.cbo_encoder = QComboBox()
        for key, label in ENCODER_CHOICES:
            self.cbo_encoder.addItem(label, key)

        self.spin_crf = QSpinBox()
        self.spin_crf.setRange(0, 51)
        self.spin_crf.setToolTip("数值越小画质越好体积越大。x264 用 CRF，硬件编码自动压低 2 档等价")

        self.spin_max = QSpinBox()
        self.spin_max.setRange(0, 20000)
        self.spin_max.setSingleStep(100)
        self.spin_max.setSuffix(" kbps")
        self.spin_max.setSpecialValueText("不限")
        self.spin_max.setToolTip("峰值码率上限，0 表示不限制")

        self.spin_audio = QSpinBox()
        self.spin_audio.setRange(32, 320)
        self.spin_audio.setSingleStep(16)
        self.spin_audio.setSuffix(" kbps")

        self.chk_ssim = QComboBox()
        self.chk_ssim.addItem("校验：时长+帧数+SSIM", True)
        self.chk_ssim.addItem("校验：只查时长+帧数", False)

        grid.addWidget(QLabel("ffmpeg"), 0, 0)
        grid.addWidget(self.ed_ffmpeg, 0, 1, 1, 5)
        grid.addWidget(self.btn_ffmpeg, 0, 6)
        grid.addWidget(QLabel("编码器"), 1, 0)
        grid.addWidget(self.cbo_encoder, 1, 1)
        grid.addWidget(QLabel("质量"), 1, 2)
        grid.addWidget(self.spin_crf, 1, 3)
        grid.addWidget(QLabel("峰值"), 1, 4)
        grid.addWidget(self.spin_max, 1, 5)
        grid.addWidget(QLabel("音频"), 1, 6)
        grid.addWidget(self.spin_audio, 1, 7)
        grid.addWidget(self.chk_ssim, 1, 8, 1, 2)
        grid.setColumnStretch(1, 2)

        self.btn_ffmpeg.clicked.connect(self._pick_ffmpeg)
        return box

    # ------------------------------------------------------------------ 外观

    def _is_dark(self) -> bool:
        return self.palette().color(QPalette.Window).lightness() < 128

    def _apply_theme(self) -> None:
        """表头与网格线取调色板颜色，深色/浅色主题下都不会变成浅底浅字。"""
        self._dark = self._is_dark()
        pal = self.palette()
        self.setStyleSheet("""
QTableView { gridline-color: %(line)s; }
QHeaderView::section {
    background: %(win)s; color: %(text)s; padding: 5px 6px; border: 0;
    border-right: 1px solid %(line)s; border-bottom: 1px solid %(line)s;
}
""" % {"line": pal.color(QPalette.Mid).name(),
       "win": pal.color(QPalette.Window).name(),
       "text": pal.color(QPalette.Text).name()})
        self.model.dark = self._dark

    def changeEvent(self, event) -> None:                  # noqa: N802
        if event.type() == QEvent.PaletteChange:
            self._apply_theme()
            self.model.refresh_all()
            self.log_view.setStyleSheet(self._log_qss())
        super().changeEvent(event)

    @staticmethod
    def _log_qss() -> str:
        return "font-family: Consolas, 'Courier New', monospace; font-size: 13px;"

    # ------------------------------------------------------------------ 配置

    def _load_cfg(self) -> None:
        self.ed_host.setText(str(self.cfg.get("host") or ""))
        self.ed_token.setText(str(self.cfg.get("token") or ""))
        self.ed_fallback.setText(str(self.cfg.get("fallback_host") or ""))
        idx = self.cbo_encoder.findData(str(self.cfg.get("encoder") or "auto"))
        self.cbo_encoder.setCurrentIndex(max(0, idx))
        self.spin_crf.setValue(int(self.cfg.get("crf") or 23))
        self.spin_max.setValue(int(self.cfg.get("max_bitrate") or 0))
        self.spin_audio.setValue(int(self.cfg.get("audio_bitrate") or 96))
        self.chk_ssim.setCurrentIndex(0 if self.cfg.get("check_ssim", True) else 1)

    def _collect_cfg(self) -> dict:
        self.cfg.update({
            "host": self.ed_host.text().strip(),
            "token": self.ed_token.text().strip(),
            "fallback_host": self.ed_fallback.text().strip(),
            "encoder": self.cbo_encoder.currentData(),
            "crf": self.spin_crf.value(),
            "max_bitrate": self.spin_max.value(),
            "audio_bitrate": self.spin_audio.value(),
            "check_ssim": bool(self.chk_ssim.currentData()),
        })
        return self.cfg

    def _save_cfg(self) -> None:
        try:
            config.save(self._collect_cfg())
        except OSError as exc:
            self._log("warn", "配置写不进去：%s" % exc)

    def _probe_ffmpeg(self) -> None:
        ffmpeg, _ = config.find_ffmpeg(self.cfg)
        if ffmpeg:
            self.ed_ffmpeg.setText(ffmpeg)
            self.ed_ffmpeg.setStyleSheet("")
            return
        self.ed_ffmpeg.setText("")
        self.ed_ffmpeg.setPlaceholderText(config.vendor_hint())
        self.ed_ffmpeg.setStyleSheet("color:#b91c1c")
        self._log("error", "没找到 ffmpeg。" + config.vendor_hint())

    def _pick_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ffmpeg", "", "可执行文件 (ffmpeg*.exe ffmpeg);;所有文件 (*)")
        if not path:
            return
        self.cfg["ffmpeg_path"] = os.path.dirname(path)
        self._probe_ffmpeg()
        self._save_cfg()
        if self.ed_ffmpeg.text():
            self._log("ok", "ffmpeg：%s" % self.ed_ffmpeg.text())

    # ------------------------------------------------------------------ 动作

    def _scan(self) -> None:
        if self._busy():
            return
        host = self.ed_host.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少地址", "先填服务地址，例如 127.0.0.1:8765")
            return
        self._save_cfg()
        self._set_busy(True, "连接中…")
        self.lbl_link.setText("连接中…")

        self.scan_worker = ScanWorker(self.cfg, self)
        self.scan_worker.connected.connect(self._on_connected)
        self.scan_worker.live.connect(self._on_live)
        self.scan_worker.log.connect(self._log)
        self.scan_worker.done.connect(self._on_scan_done)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.finished.connect(self._end_busy)
        self.scan_worker.start()

    def _on_connected(self, host: str, link: str) -> None:
        self.lbl_link.setText("已连接 %s（%s）" % (host, link))
        self.lbl_link.setStyleSheet("color:#15803d")

    def _on_live(self, live: bool, names: list) -> None:
        if live:
            self.lbl_link.setText("已连接 · 直播中：%s" % "、".join(names))
            self.lbl_link.setStyleSheet("color:#a16207")

    def _on_scan_done(self, parts: list) -> None:
        self.parts = parts
        self.client = self.scan_worker.client if self.scan_worker else None
        self.model.set_parts(parts)
        self._done = 0
        self._total = 0
        self.progress.setValue(0)
        self.progress.setFormat("就绪")

        count, size, _, todo = self.model.totals()
        if not count:
            self.summary.setText("没有可压缩的分片（可能都在录制中或还没 remux 完成）")
            self._log("warn", "服务返回的录像里没有 ready 状态的分片")
        else:
            self.summary.setText("%d 片 · 共 %s · %d 片待处理"
                                 % (count, human_size(size), todo))
            self._log("ok", "扫到 %d 片，共 %s" % (count, human_size(size)))
        self._update_buttons()

    def _on_scan_failed(self, message: str) -> None:
        self.lbl_link.setText("未连接")
        self.lbl_link.setStyleSheet("color:#b91c1c")
        self._log("error", message)
        QMessageBox.critical(self, "连接失败", message)

    def _picked(self) -> list[tuple[int, "object"]]:
        """选中的行；一行没选就取全部。已完成/跳过的不会再跑。"""
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        if not rows:
            rows = list(range(len(self.parts)))
        return [(r, self.parts[r]) for r in rows
                if self.parts[r].status not in ("skip", "done")]

    def _start(self, dry: bool) -> None:
        if self._busy():
            return
        if not self.parts:
            QMessageBox.information(self, "先扫描", "还没有分片清单，先点「扫描」。")
            return
        if self.client is None:
            QMessageBox.information(self, "未连接", "先点「扫描」连上服务。")
            return
        if not self.ed_ffmpeg.text():
            QMessageBox.warning(self, "缺 ffmpeg", config.vendor_hint())
            return
        items = self._picked()
        if not items:
            QMessageBox.information(self, "没有可处理的分片",
                                    "选中的行都已经完成或按码率跳过了。")
            return

        total = sum(p.bytes for _, p in items)
        what = "试算" if dry else "压缩并回传"
        extra = "\n\n试算只下载不编码不回传，用来先看能省多少。" if dry else ""
        if QMessageBox.question(
                self, "确认" + what,
                "%s %d 片，共 %s？%s" % (what, len(items), human_size(total), extra),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes) != QMessageBox.Yes:
            return

        self._save_cfg()
        self._total = len(items)
        self._done = 0
        self.progress.setValue(0)

        self.run_worker = RunWorker(self.cfg, self.client, items,
                                    dry=dry, parent=self)
        self.run_worker.log.connect(self._log)
        self.run_worker.step.connect(self._on_step)
        self.run_worker.row_start.connect(self._on_row_start)
        self.run_worker.row_done.connect(self._on_row_done)
        self.run_worker.blocked.connect(self._on_blocked)
        self.run_worker.finished_all.connect(self._on_all_done)
        self.run_worker.finished.connect(self._end_busy)

        self._set_busy(True, "准备中…")
        self.run_worker.start()

    def _stop(self) -> None:
        if self.run_worker and self.run_worker.isRunning():
            self._stopping = True
            self.run_worker.stop()
            self.btn_stop.setEnabled(False)
            self._log("warn", "正在停止，当前这一步做完就退出…")

    # ------------------------------------------------------------------ 回调

    def _on_row_start(self, row: int) -> None:
        self.table.scrollTo(self.model.index(row, 0),
                            QAbstractItemView.PositionAtCenter)

    def _on_step(self, row: int, name: str, ratio: float) -> None:
        self.model.refresh_row(row)
        if ratio >= 0 and self._total:
            base = self._done / float(self._total)
            span = 1.0 / self._total
            self.progress.setValue(int((base + span * min(1.0, ratio)) * 1000))
            self.progress.setFormat("%s %d%%" % (name, ratio * 100))

    def _on_row_done(self, row: int) -> None:
        self.model.refresh_row(row)
        self._done += 1
        if self._total:
            self.progress.setValue(int(self._done / float(self._total) * 1000))
        self._refresh_summary()
        self._update_buttons()

    def _on_all_done(self, count: int, saved: int) -> None:
        self._refresh_summary()
        self.progress.setFormat("完成")
        if saved:
            self._log("ok", "本轮处理 %d 片，省下 %s" % (count, human_size(saved)))
        else:
            self._log("info", "本轮处理 %d 片，没有产生节省" % count)
        self._update_buttons()

    def _on_blocked(self, message: str) -> None:
        self._log("warn", message)
        self.progress.setFormat("已中止")
        QMessageBox.warning(self, "无法继续", message)

    def _refresh_summary(self) -> None:
        count, size, saved, todo = self.model.totals()
        text = "%d 片 · 共 %s" % (count, human_size(size))
        if saved:
            text += " · 已省 %s" % human_size(saved)
        if todo:
            text += " · %d 片待处理" % todo
        self.summary.setText(text)

    # ------------------------------------------------------------------ 杂项

    def _log(self, level: str, text: str) -> None:
        colors = LOG_COLORS[getattr(self, "_dark", False)]
        color = colors.get(level, colors["info"])
        stamp = time.strftime("%H:%M:%S")
        self.log_view.appendHtml(
            '<span style="color:%s">%s</span> '
            '<span style="color:%s">%s</span>'
            % (colors["debug"], stamp, color, html.escape(text)))

    def _busy(self) -> bool:
        return self._busy_flag

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy_flag = busy
        if busy:
            self._stopping = False
        if text:
            self.progress.setFormat(text)
        self._update_buttons()

    def _end_busy(self) -> None:
        self._busy_flag = False
        self._stopping = False
        self._update_buttons()

    def _update_buttons(self) -> None:
        busy = self._busy_flag
        self.btn_scan.setEnabled(not busy)
        self.btn_estimate.setEnabled(not busy and bool(self.parts))
        self.btn_run.setEnabled(not busy and bool(self.parts))
        self.btn_stop.setEnabled(busy and not self._stopping
                                 and self.run_worker is not None)

    def closeEvent(self, event) -> None:                    # noqa: N802
        if self.run_worker and self.run_worker.isRunning():
            answer = QMessageBox.question(
                self, "任务进行中",
                "还在处理，确定退出吗？\n当前片会中止；手机上的文件不会被动过。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.run_worker.stop()
            self.run_worker.wait(15000)
        self._save_cfg()
        event.accept()


def main() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("live-capture-pctools")
    win = MainWindow()
    win.show()

    # 居中，避免每次开在屏幕角落
    screen = QGuiApplication.primaryScreen()
    if screen:
        geo = win.frameGeometry()
        geo.moveCenter(screen.availableGeometry().center())
        win.move(geo.topLeft())
    return app.exec()
