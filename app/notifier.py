# -*- coding: utf-8 -*-
"""开播 / 新动态的推送定时器。

和 `DynamicScheduler`（按 up 主每天定时抓一次动态）分开：那一个是**采集**，
只在你设定的时间点拉一次；这里是**通知**，按分钟级轮询，发现状态变化就推
飞书。两者共用 `DynamicsFetcher` / `store.upsert_dynamics`，不会重复入库。

判重口径（重启不重复推送）：

- 开播看 `live_time`（本场开播时间戳）。它在 B 站是「这一场」的唯一标识，
  同一场里反复轮询不会重复；下播再开播会换新值，才会再推一次。
- 动态看 `pub_ts`。只推比上次已推最大时间更新的条目。

第一次看到某位 up 主时只记基线、不推送——否则服务一启动就会把当前正在播
的场次和历史动态全推一遍。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

import notify

from rooms import is_test_room

from .settings import as_int


class NotifierScheduler:
    def __init__(self, store, config, logs, rooms, data_dir: str,
                 dynamics=None, accounts=None,
                 live_probe: Callable[[int], dict] | None = None,
                 urlopen=None):
        self.store = store
        self.config = config
        self.logs = logs
        self.rooms = rooms
        self.dynamics = dynamics
        self.accounts = accounts
        self.data_dir = data_dir
        self.urlopen = urlopen
        # 共用一份 BiliApi：cookie 播种和 WBI key 都有缓存，每秒级轮询不必重建
        self._api = None
        self.live_probe = live_probe or self._probe_live
        self.state_path = os.path.join(data_dir, "notify-state.json")
        self.lock = threading.RLock()
        self.state: dict[str, Any] = {"version": 1, "rooms": {}}
        self._load()
        self.last_result: dict[str, Any] = {"ok": True, "msg": "尚未检查", "ts": 0}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_live = 0.0
        self._last_dyn = 0.0

    # ---------- 状态文件 ----------

    def _load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("rooms"), dict):
                self.state = {"version": 1, "rooms": data["rooms"]}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            self.logs.add("warn", "通知状态写入失败：%s" % exc)

    def _mark(self, key: str, **patch) -> None:
        with self.lock:
            row = self.state["rooms"].setdefault(str(key), {})
            row.update(patch)
            self._save()

    def _seen(self, key: str) -> dict:
        with self.lock:
            return dict(self.state["rooms"].get(str(key)) or {})

    # ---------- 配置 ----------

    def _settings(self) -> tuple[str, str, int, int]:
        cfg = self.config.get()
        return (str(cfg.get("feishu_webhook") or "").strip(),
                str(cfg.get("feishu_secret") or "").strip(),
                max(20, as_int(cfg.get("notify_live_seconds"), 60) or 60),
                max(60, as_int(cfg.get("notify_dyn_seconds"), 300) or 300))

    def enabled(self) -> bool:
        return bool(self._settings()[0])

    # ---------- 推送 ----------

    def _push(self, payload: dict, what: str) -> bool:
        webhook, secret, _, _ = self._settings()
        try:
            notify.send(webhook, payload, secret, urlopen=self.urlopen)
        except notify.NotifyError as exc:
            self.logs.add("warn", "推送%s失败：%s" % (what, exc))
            self._result(False, "推送%s失败：%s" % (what, exc))
            return False
        self.logs.add("info", "已推送：%s" % what)
        self._result(True, "已推送：%s" % what)
        return True

    def _result(self, ok: bool, msg: str) -> None:
        with self.lock:
            self.last_result = {"ok": ok, "msg": msg, "ts": int(time.time() * 1000)}

    # ---------- 开播探测 ----------

    def _probe_live(self, rid: int) -> dict:
        """取本场开播时间。`room/v1/Room/get_info` 的 live_time 是日期字符串，
        解析不出秒；`getInfoByRoom` 的 `live_start_time` 才是 epoch，采集线程
        判断开播用的也是它，这里保持一致。
        """
        from capture import BiliApi
        if self._api is None:
            cfg = self.config.get()
            active = self.accounts.active() if self.accounts is not None else None
            self._api = BiliApi({"http_timeout": cfg.get("http_timeout"),
                                 "_cookies": dict((active or {}).get("cookies") or {})})
        return self._api.resolve_room(rid)

    # ---------- 开播 ----------

    def check_live(self) -> int:
        """返回本次推送的条数。"""
        sent = 0
        for room in (self.rooms.list().get("items") or []):
            if room.get("notify_live", True) is False:
                continue
            # 测试源没有 up 主，去 B 站探开播只会 404，别让它刷日志
            if is_test_room(room):
                continue
            rid = as_int(room.get("id"))
            if rid <= 0:
                continue
            try:
                brief = self.live_probe(rid)
            except Exception as exc:            # 单个房间失败不影响其他
                self._api = None                # 下次重建，重新播种 cookie
                self.logs.add("warn", "开播探测失败（%s）：%s" % (rid, exc))
                continue
            status = as_int(brief.get("live_status"))
            live_time = as_int(brief.get("live_time"))
            key = "live:%d" % rid
            seen = self._seen(key)
            # 状态说在播但开播时间为 0，是 B 站抖动，当没开播
            if status != 1 or live_time <= 0:
                continue
            if seen.get("live_time") == live_time:
                continue                        # 同一场，已经处理过
            first_time = "live_time" not in seen
            self._mark(key, live_time=live_time)
            if first_time:
                continue                        # 首次只见基线，不推
            uname = str(brief.get("uname") or room.get("uname") or "")
            title = str(brief.get("title") or room.get("title") or "")
            if self._push(notify.live_card(uname, rid, title),
                          "%s 开播" % (uname or rid)):
                sent += 1
        return sent

    # ---------- 动态 ----------

    def check_dynamics(self) -> int:
        sent = 0
        for room in (self.rooms.list().get("items") or []):
            if room.get("notify_dynamic", True) is False:
                continue
            if room.get("collect_dynamics", True) is False:
                continue
            mid = as_int(room.get("uid"))
            if mid <= 0 or self.dynamics is None:
                continue
            try:
                fetched, _ = self.dynamics.pull_one(mid, "通知轮询")
            except Exception as exc:
                self.logs.add("warn", "拉取动态失败（uid %s）：%s" % (mid, exc))
                continue
            if not fetched:
                # B 站常对匿名请求返回 code=0 但 items=[]，看起来像成功其实没拿到
                self.logs.add("warn", "拉取动态返回 0 条（uid %s）：可能需要登录账号，"
                                      "或稍后重试" % mid)
                continue
            _, items = self.store.list_dynamics(limit=20, mid=mid)
            if not items:
                continue
            newest = max(as_int(item.get("pub_ts")) for item in items)
            key = "dyn:%d" % mid
            seen = self._seen(key)
            base = as_int(seen.get("dyn_pub_ts"))
            if base <= 0:
                self._mark(key, dyn_pub_ts=newest)   # 首次只记基线
                continue
            fresh = [item for item in items if as_int(item.get("pub_ts")) > base]
            if not fresh:
                continue
            self._mark(key, dyn_pub_ts=newest)
            uname = str(room.get("uname") or "")
            for item in sorted(fresh, key=lambda x: as_int(x.get("pub_ts"))):
                if self._push(notify.dynamic_card(
                        uname, mid, str(item.get("text") or ""),
                        str(item.get("jump_url") or "")), "%s 新动态" % (uname or mid)):
                    sent += 1
        return sent

    # ---------- 线程 ----------

    def tick(self) -> None:
        if not self.enabled():
            return
        _, _, live_sec, dyn_sec = self._settings()
        now = time.time()
        if now - self._last_live >= live_sec:
            self._last_live = now
            self.check_live()
        if now - self._last_dyn >= dyn_sec:
            self._last_dyn = now
            self.check_dynamics()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._last_live = self._last_dyn = 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="notifier")
        self._thread.start()

    def _loop(self) -> None:
        # 5 秒对一次表：改了轮询间隔或刚填了 webhook，不用重启服务
        while not self._stop.wait(5):
            try:
                self.tick()
            except Exception as exc:            # 通知线程不能因为一次异常就死
                self.logs.add("error", "通知检查出错：%s" % exc, exc=exc)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        with self.lock:
            out = dict(self.last_result)
        webhook, _, live_sec, dyn_sec = self._settings()
        out.update({"enabled": bool(webhook), "webhook_set": bool(webhook),
                    "live_seconds": live_sec, "dyn_seconds": dyn_sec,
                    "rooms": len(self.state.get("rooms") or {})})
        return out
