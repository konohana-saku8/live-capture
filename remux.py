# -*- coding: utf-8 -*-
"""单线程 remux 队列：下播后把 FLV 无损转成 MP4，避免和采集抢 CPU。"""
from __future__ import annotations

import queue
import threading


class RemuxWorker:
    def __init__(self, runner, log=None):
        self.runner = runner
        self.log = log or (lambda *_: None)
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.seen = set()
        self.thread = None

    def enqueue(self, rec_id):
        rec_id = int(rec_id)
        with self.lock:
            if rec_id in self.seen:
                return
            self.seen.add(rec_id)
        self._ensure_thread()
        self.queue.put(rec_id)

    def _ensure_thread(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(
                target=self._run, name="recording-remux", daemon=True)
            self.thread.start()

    def _run(self):
        while True:
            rec_id = self.queue.get()
            try:
                self.runner(rec_id)
            except Exception as exc:
                self.log("warn", "[录像 %s] remux 线程异常：%s" % (rec_id, exc))
            finally:
                with self.lock:
                    self.seen.discard(rec_id)
                self.queue.task_done()
