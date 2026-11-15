"""按 up 主逐个拉取动态的定时器。

定时抓取是**每位 up 主自己的设置**（rooms.json 里的 dyn_auto / dyn_hour /
dyn_minute，默认关闭）：到点只拉这一位，互不影响。整轮结果写进
`last_result` 供页面展示。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from dynamics import DynamicsFetcher

from .settings import as_int


class DynamicScheduler:
    def __init__(self, store, config, logs, fetcher_cls=DynamicsFetcher,
                 rooms=None):
        self.store = store
        self.config = config
        self.logs = logs
        self.fetcher_cls = fetcher_cls
        self.rooms = rooms
        self.lock = threading.RLock()
        self.last_result: dict[str, Any] = {"ok": False, "msg": "尚未拉取", "ts": 0}
        self._stop = threading.Event()
        self._fail: dict[int, float] = {}      # room_id -> 上次失败时间，30 分钟内不重试
        self._thread: threading.Thread | None = None

    # ---------- 目标 ----------

    def targets(self) -> list[int]:
        """所有打开了「采集动态」的 uid（「立即拉取」针对单个 up 主时用不到）。"""
        items = self.rooms.collect_targets("collect_dynamics") \
            if self.rooms is not None else []
        mids = [as_int(x.get("uid")) for x in items]
        mids = [m for m in mids if m > 0]
        if not mids:
            legacy = as_int(self.config.get().get("dyn_mid"))
            if legacy:
                mids = [legacy]
        return mids

    def auto_rooms(self) -> list[dict[str, Any]]:
        """打开了定时抓取的采集器（各自带时:分）。"""
        return self.rooms.auto_dyn_rooms() if self.rooms is not None else []

    # ---------- 拉取 ----------

    def pull_one(self, mid: int, reason: str = "手动", limit: int = 20):
        fetcher = self.fetcher_cls()
        items, _ = fetcher.fetch(as_int(mid), limit=limit)
        n = self.store.upsert_dynamics(items)
        return len(items), n

    def pull(self, reason: str = "手动", limit: int = 20, mid: int = 0) -> dict[str, Any]:
        mids = [as_int(mid)] if as_int(mid) > 0 else self.targets()
        if not mids:
            result = {"ok": False, "msg": "没有开启「采集动态」的 up 主",
                      "ts": int(time.time() * 1000)}
            with self.lock:
                self.last_result = result
            return result
        ok, done, total, errors = True, 0, 0, []
        for one in mids:
            try:
                got, _ = self.pull_one(one, reason, limit)
                total += got
                done += 1
            except Exception as exc:            # 单个 up 失败不影响其他
                ok = False
                errors.append("uid %s：%s" % (one, exc))
        ts = int(time.time() * 1000)
        if errors and not done:
            msg = "%s拉取失败：%s" % (reason, "；".join(errors))
        else:
            msg = "%s拉取 %d 位 up 主，共 %d 条%s" % (
                reason, done, total,
                ("；失败 " + "；".join(errors)) if errors else "")
        result = {"ok": ok, "msg": msg, "ts": ts, "count": total, "mids": mids}
        self.logs.add("info" if ok else "error", msg)
        with self.lock:
            self.last_result = result
        return result

    def status(self) -> dict[str, Any]:
        with self.lock:
            out = dict(self.last_result)
        out["total"] = self.store.list_dynamics(limit=1)[0]
        out["targets"] = self.targets()
        out["auto"] = [as_int(x.get("id")) for x in self.auto_rooms()]
        return out

    # ---------- 定时 ----------

    def start(self) -> None:
        """定时线程常驻：某位 up 主随时打开定时，30 秒内就生效。"""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="dyn-scheduler")
        self._thread.start()

    def _next_run(self, hour: int = 8, minute: int = 0) -> float:
        """今天的「时:分」时间戳；已经过了就算明天。

        `time.mktime` 要 9 元组（末尾 isdst），少一项会抛
        「illegal time tuple argument」——定时线程会直接死掉。
        """
        now = time.localtime()
        target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                              hour, minute, 0, 0, 0, -1))
        if target <= time.time():
            target += 86400
        return target

    def _loop(self) -> None:
        """每 30 秒对一次表：到点且今天这个时间点还没拉过就拉。

        不用「睡到明天同一时刻」的长等待，这样手机休眠错过时间点后醒来能补拉，
        改过时/分也能马上生效；失败后隔 30 分钟再试，避免反复撞风控。
        """
        while not self._stop.is_set():
            if self._stop.wait(30):
                break
            try:
                self._tick()
            except Exception as exc:            # 定时线程不能因为一次异常就死
                self.logs.add("error", "动态定时检查出错：%s" % exc)

    def _tick(self) -> None:
        now = time.time()
        lt = time.localtime(now)
        for room in self.auto_rooms():
            rid = as_int(room.get("id"))
            uid = as_int(room.get("uid"))
            if uid <= 0:
                continue
            hour = min(23, max(0, as_int(room.get("dyn_hour"), 8)))
            minute = min(59, max(0, as_int(room.get("dyn_minute"), 0)))
            target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                  hour, minute, 0, 0, 0, -1))
            if now < target:
                continue                        # 今天还没到点
            last = as_int(room.get("dyn_last_ts"))
            if last and last >= int(target * 1000):
                continue                        # 今天这个时间点已经拉过
            failed = self._fail.get(rid) or 0
            if failed and now - failed < 1800:
                continue                        # 刚失败过，等半小时
            who = room.get("uname") or ("uid %s" % uid)
            try:
                got, _ = self.pull_one(uid, "定时")
            except Exception as exc:
                self._fail[rid] = now
                msg = "定时拉取 %s 的动态失败：%s" % (who, exc)
                self.logs.add("error", msg)
                with self.lock:
                    self.last_result = {"ok": False, "msg": msg, "ts": int(now * 1000)}
                continue
            self._fail.pop(rid, None)
            ts = int(time.time() * 1000)
            self.rooms.settings(rid, {"dyn_last_ts": ts})
            msg = "定时拉取 %s 的动态：%d 条" % (who, got)
            self.logs.add("info", msg)
            with self.lock:
                self.last_result = {"ok": True, "msg": msg, "ts": ts,
                                    "count": got, "mids": [uid]}

    def stop(self) -> None:
        self._stop.set()
