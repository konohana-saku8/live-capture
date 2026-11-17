# -*- coding: utf-8 -*-
"""开播自动录制：把 B 站直播的 HTTP-FLV 流原样写入 data/recordings/<room_id>/。

不用 ffmpeg：playUrl 返回的 http-flv 是一条连续字节流，直接落盘就是可播放的
FLV。连接断了就用新地址续录成下一个 part，不覆盖已写内容。
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import urllib.request

MIN_FREE_BYTES = 1024 * 1024 * 1024
READ_CHUNK = 64 * 1024
REPORT_INTERVAL = 1.0
RETRY_SECONDS = 5.0

_BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(text):
    """文件名里只保留常见字符，避免 Windows / Android 上的非法路径。"""
    name = _BAD_NAME.sub("_", str(text or "")).strip(" ._")
    return name[:40] or "room"


def _qn_label(qn):
    return {80: "360", 150: "480", 250: "720", 400: "1080",
            10000: "原画"}.get(int(qn or 0), str(qn))


class LiveRecorder:
    """一条直播流一个录制线程；start/stop 可重复调用。"""

    def __init__(self, data_dir, room_id, label, log, on_state=None,
                 recordings=None):
        self.data_dir = data_dir
        self.room_id = int(room_id)
        self.label = label or ("房间 %s" % room_id)
        self.log = log
        self.on_state = on_state or (lambda **kw: None)
        self.recordings = recordings
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._resp = None
        self._thread = None
        self._recording = False
        self._base = ""
        self._part = 0
        self._started_at = 0
        self._bytes = 0
        self._path = ""
        self._error = ""
        self._rec_id = 0
        self._qn = 250
        self._actual_qn = 0

    @property
    def recording(self):
        with self.lock:
            return self._recording

    def start(self, api, live_started_at=0, qn=250):
        with self.lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._base = time.strftime("%Y%m%d_%H%M%S") + "_" + safe_name(self.label)
            self._part = 0
            self._recording = True
            self._started_at = int(time.time() * 1000)
            self._bytes = 0
            self._path = ""
            self._error = ""
            self._qn = int(qn or 250)
            self._actual_qn = 0
            self._rec_id = 0
            if self.recordings:
                try:
                    self._rec_id = self.recordings.create(
                        self.room_id, self.label, live_started_at,
                        self._qn, started_at=self._started_at)
                except Exception as exc:
                    self.log("warn", "[%s] 创建录像记录失败：%s" % (self.label, exc))
            self._thread = threading.Thread(
                target=self._run, args=(api,),
                name="record-%s" % self.room_id, daemon=True)
            self._report()
            self._thread.start()
        self.log("info", "[%s] 开播，开始自动录制（请求 %sP）" %
                 (self.label, _qn_label(self._qn)))
        return True

    def stop(self, reason=""):
        with self.lock:
            thread = self._thread
            was_recording = self._recording
            self._stop.set()
            resp = self._resp
            rec_id = self._rec_id
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self.lock:
            self._recording = False
            self._thread = None
            self._rec_id = 0
            self._report()
        if rec_id and self.recordings:
            try:
                self.recordings.finish(rec_id, status="pending")
            except Exception as exc:
                self.log("warn", "[%s] 结束录像记录失败：%s" % (self.label, exc))
        if was_recording:
            self.log("info", "[%s] 停止录制%s" %
                     (self.label, ("：" + reason) if reason else ""))
        return was_recording

    def _report(self):
        with self.lock:
            self.on_state(recording=self._recording,
                          record_started_at=self._started_at,
                          record_bytes=self._bytes,
                          record_path=self._path,
                          record_error=self._error,
                          record_qn=self._qn,
                          record_qn_actual=self._actual_qn,
                          record_status="recording" if self._recording else
                          ("error" if self._error else "idle"))

    def _set_error(self, message):
        with self.lock:
            self._error = str(message)
            self._report()

    def _run(self, api):
        delay = RETRY_SECONDS
        while not self._stop.is_set():
            try:
                if shutil.disk_usage(self.data_dir).free < MIN_FREE_BYTES:
                    raise RuntimeError("磁盘剩余空间不足 %d MB" %
                                       (MIN_FREE_BYTES // 1024 // 1024))
                urls, real_qn = api.get_play_urls(self.room_id, qn=self._qn)
                if real_qn and real_qn != self._actual_qn:
                    with self.lock:
                        self._actual_qn = int(real_qn)
                    if self.recordings and self._rec_id:
                        try:
                            self.recordings.set_actual_qn(self._rec_id, real_qn)
                        except Exception:
                            pass
                    self._report()
                if not urls:
                    raise RuntimeError("playUrl 没有返回直播流")
                wrote = 0
                for url in urls:
                    if self._stop.is_set():
                        break
                    wrote = self._record_one(url, api)
                    if wrote:
                        break
                delay = RETRY_SECONDS
                if not wrote:
                    self._stop.wait(1.0)
            except Exception as exc:
                if self._stop.is_set():
                    break
                self._set_error(exc)
                self.log("warn", "[%s] 录制中断：%s，%.0f 秒后重试" %
                         (self.label, exc, delay))
                self._stop.wait(delay)
                delay = min(60.0, delay * 2)
        with self.lock:
            self._recording = False
            self._thread = None
            self._report()

    def _record_one(self, url, api):
        directory = os.path.join(self.data_dir, "recordings", str(self.room_id))
        os.makedirs(directory, exist_ok=True)
        with self.lock:
            self._part += 1
            part = self._part
            name = self._base + (".flv" if part == 1 else "_p%d.flv" % part)
            path = os.path.join(directory, name)
            self._path = path
            self._bytes = 0
            self._error = ""
            self._report()
        headers = {
            "User-Agent": api.user_agent,
            "Referer": "https://live.bilibili.com/",
            "Origin": "https://live.bilibili.com",
        }
        cookie = api.cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, headers=headers)
        wrote = 0
        with urllib.request.urlopen(req, timeout=30) as resp:
            with self.lock:
                self._resp = resp
            part_started = int(time.time() * 1000)
            if self.recordings and self._rec_id:
                try:
                    self.recordings.add_part(self._rec_id, path, part_started)
                except Exception as exc:
                    self.log("warn", "[%s] 记录录像分片失败：%s" % (self.label, exc))
            self.log("info", "[%s] 录制写入 %s" % (self.label, path))
            try:
                with open(path, "wb") as handle:
                    last = 0.0
                    first = True
                    while not self._stop.is_set():
                        chunk = resp.read(READ_CHUNK)
                        if not chunk:
                            break
                        if first:
                            first = False
                            if not chunk.startswith(b"FLV"):
                                raise RuntimeError("返回的不是 FLV 直播流")
                        handle.write(chunk)
                        wrote += len(chunk)
                        with self.lock:
                            self._bytes += len(chunk)
                        now = time.time()
                        if now - last >= REPORT_INTERVAL:
                            last = now
                            self._report()
            finally:
                with self.lock:
                    self._resp = None
                if self.recordings and self._rec_id:
                    try:
                        self.recordings.finish_part(self._rec_id, path)
                    except Exception:
                        pass
                self._report()
        return wrote
