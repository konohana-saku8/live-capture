# -*- coding: utf-8 -*-
"""后台线程：扫描与执行。界面只收信号，不做任何阻塞调用。"""
from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal

from ..core import api, job


class ScanWorker(QThread):
    """连接服务 + 拉取分片清单。"""

    log = Signal(str, str)              # level, text
    connected = Signal(str, str)        # host, 链路说明
    live = Signal(bool, list)           # 是否在播, 房间名
    done = Signal(object)               # list[job.Part]
    failed = Signal(str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.client: api.Client | None = None

    def run(self) -> None:
        try:
            client, link = api.connect(
                str(self.cfg.get("host") or ""),
                str(self.cfg.get("fallback_host") or ""),
                str(self.cfg.get("token") or ""))
        except api.ApiError as exc:
            self.failed.emit(str(exc))
            return
        self.client = client
        self.connected.emit(client.host, link)
        self.log.emit("info", "已连接 %s（%s）" % (client.host, link))

        try:
            live, names = client.live()
        except api.ApiError:
            live, names = False, []
        self.live.emit(live, names)
        if live:
            self.log.emit("warn", "当前有直播在进行：%s" % "、".join(names))

        try:
            parts = job.scan(client,
                             int(self.cfg.get("min_bitrate") or 0),
                             int(self.cfg.get("min_part_mb") or 0) * 1048576)
        except api.ApiError as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(parts)


class RunWorker(QThread):
    """逐片跑完「下载 → 试算 → 编码 → 校验 → 上传」."""

    log = Signal(str, str)
    step = Signal(int, str, float)      # 行号, 步骤名, 0~1（-1 表示不确定）
    row_start = Signal(int)
    row_done = Signal(int)
    blocked = Signal(str)               # 前置条件不满足，整批不跑
    finished_all = Signal(int, int)     # 处理片数, 省下字节

    def __init__(self, cfg: dict, client: api.Client,
                 items: list[tuple[int, job.Part]], dry: bool = False,
                 parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.client = client
        self.items = items
        self.dry = dry
        self.session: job.Session | None = None
        self._stop = False

    def stop(self) -> None:
        self._stop = True
        if self.session is not None:
            self.session.stop()

    def _step_cb(self, row: int):
        """把 job 的进度回调转成信号，顺手把进度记在对象上供表格显示。"""
        def cb(part, name: str, ratio: float) -> None:
            if ratio >= 0:
                part.progress = ratio
            self.step.emit(row, name, ratio)
        return cb

    def run(self) -> None:
        try:
            self.session = job.Session(self.cfg, self.client, log=self.log.emit)
        except Exception as exc:                      # noqa: BLE001
            self.log.emit("error", "初始化失败：%s" % exc)
            self.blocked.emit("初始化失败：%s" % exc)
            return

        self.log.emit("info", "编码器：%s" % self.session.describe())
        if not self.session.ready:
            self.blocked.emit(self.session.describe())
            return

        count = 0
        saved = 0
        for row, part in self.items:
            if self._stop:
                break
            # 开播守卫：每片开始前确认一次，在播就停在片边界，不留半截文件
            if self.cfg.get("pause_when_live", True):
                live, names = self.client.live()
                if live:
                    msg = "直播中（%s），结束后再操作" % "、".join(names)
                    self.log.emit("warn", msg)
                    self.blocked.emit(msg)
                    break

            self.log.emit("info", "开始：%s" % part.title)
            self.row_start.emit(row)
            try:
                self.session.run(part, dry=self.dry,
                                 on_step=self._step_cb(row))
            except Exception as exc:                  # noqa: BLE001
                # job 内部已兜住业务异常，走到这里说明是没预料到的错误
                part.status = "failed"
                part.message = str(exc)
                self.log.emit("error", "%s：%s" % (part.title, exc))
                self.log.emit("error", traceback.format_exc(limit=3))
            count += 1
            saved += part.saved
            self.row_done.emit(row)
            if self._stop:
                break

        self.finished_all.emit(count, saved)
