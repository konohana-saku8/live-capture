"""运行期容器：把 store / manager / rooms / accounts 等单例挂在一处。

Flask 的 `current_app.extensions["live"]` 就是它，Blueprint 里统一通过
`from .runtime import rt` 取用，避免每个函数都写一遍 app.config 读取。
"""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any

from accounts import AccountStore
from capture import CaptureManager, WS_IMPORT_ERROR
from dynamics import DynamicsFetcher
from images import EmoteProxy
from rooms import RoomStore
from store import Store
import sysinfo

from .scheduler import DynamicScheduler
from .settings import CONTENT_KEYS, Config, LogBuffer, as_int, lan_ip


class EventBus:
    """把采集线程刚入库的数据推给所有 SSE 订阅者。

    每个订阅者一个定长队列：慢客户端只丢最旧的消息，绝不阻塞采集线程；
    丢掉的部分由页面的 4 秒轮询兜底补齐。
    """

    def __init__(self, maxsize: int = 1000):
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, kind: str, rows: list[dict]) -> None:
        if not rows:
            return
        message = {"kind": kind, "rows": list(rows)}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(message)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except (queue.Empty, queue.Full):
                    pass


class Runtime:
    def __init__(self, data_dir: str, web_dir: str, host: str = "0.0.0.0",
                 port: int = 8765):
        self.data_dir = os.path.abspath(data_dir)
        self.web_dir = os.path.abspath(web_dir)
        os.makedirs(self.data_dir, exist_ok=True)

        self.logs = LogBuffer()
        self.logs.install_root()
        self.config = Config(os.path.join(self.data_dir, "config.json"))
        self.rooms = RoomStore(os.path.join(self.data_dir, "rooms.json"))
        self.accounts = AccountStore(os.path.join(self.data_dir, "accounts.json"))
        self.store = Store(os.path.join(self.data_dir, "capture.db"))
        self.bus = EventBus()
        self.manager = CaptureManager(self.store, self.config.get,
                                      self.logs.add, rooms=self.rooms,
                                      publish=self.bus.publish)
        self.manager.accounts = self.accounts
        self.recordings = self.manager.recordings
        self.images = EmoteProxy()
        self.cpu_sampler = sysinfo.CpuSampler()
        self.dynamics = DynamicScheduler(self.store, self.config, self.logs,
                                         rooms=self.rooms)
        # 首页 / 采集器页每 10 秒拉一次仪表盘；手机上一次聚合要 1-2 秒，
        # 缓存十几秒，避免重复全表扫描。数据最多晚十几秒，不影响使用。
        self._dash_lock = threading.Lock()
        self._dash_cached_at = 0.0
        self._dash_cache = None
        self.started_at = int(time.time() * 1000)
        self.host = host
        self.port = port
        self.lan_ip = lan_ip()
        self.ws_ok = not bool(WS_IMPORT_ERROR)
        self.fetcher_cls = DynamicsFetcher

    # ---------- 公共载荷 ----------

    def dashboard(self) -> dict[str, Any]:
        now = time.time()
        with self._dash_lock:
            if self._dash_cache is not None and now - self._dash_cached_at < 15:
                return self._dash_cache
            data = self.store.dashboard()
            self._dash_cache = data
            self._dash_cached_at = now
            return data

    def service(self) -> dict[str, Any]:
        return {"started_at": self.started_at,
                "host": self.host, "port": self.port,
                "lan_ip": self.lan_ip,
                "data_dir": self.data_dir,
                "websockets": self.ws_ok}

    def sys_snapshot(self) -> dict[str, Any]:
        return sysinfo.snapshot(self.data_dir, self.cpu_sampler, self.started_at)

    def first_room_id(self) -> int:
        items = self.rooms.list().get("items") or []
        return as_int(items[0]["id"]) if items else 0

    def rooms_payload(self) -> dict[str, Any]:
        """采集器清单 + 实时状态 + 累计统计，页面三个区块都吃这一份。

        逻辑与旧 Handler._rooms_payload 完全一致：直播中的数据边界优先用
        采集时看到的 LIVE 事件，没有就用房间接口返回的 live_time。
        """
        rooms = self.rooms.list().get("items") or []
        snap = self.manager.snapshot()
        states = {s["room_id"]: s for s in snap["rooms"]}
        dash = self.dashboard()
        totals = {x["room_id"]: x for x in dash.get("by_room") or []}
        dyn_by_mid = dash.get("dyn_by_mid") or {}
        cfg = self.config.get()
        items = []
        for room in rooms:
            st = states.get(room["id"]) or {}
            agg = totals.get(room["id"]) or {}
            live_status = as_int(st.get("live_status") or room.get("live_status"))
            since_ms = as_int(st.get("live_started_at"))
            if not since_ms and live_status == 1:
                since_ms = as_int(room.get("live_time")) * 1000
            live_stats = (self.store.session_stats(room["id"], since_ms)
                          if live_status == 1 and since_ms else None)
            params = room.get("params") or {}
            # 采集器级覆盖优先，没有覆盖就用全局默认
            content = {key: params.get(key, cfg.get(key, True))
                       for key in CONTENT_KEYS}
            items.append(dict(
                room,
                **content,
                running=bool(st.get("running")),
                waiting=bool(st.get("waiting")),
                live_only=bool(room.get("live_only")),
                configured=True,
                real_room_id=st.get("real_room_id") or room["id"],
                live_status=live_status,
                uname=st.get("uname") or room.get("uname", ""),
                face=room.get("face", ""),
                live_time=as_int(room.get("live_time")),
                live_started_at=as_int(st.get("live_started_at")),
                live_stats=live_stats,
                online=st.get("online") or room.get("online", 0),
                popularity=st.get("popularity", 0),
                last_packet_at=st.get("last_packet_at", 0),
                started_at=st.get("started_at", 0),
                last_error=st.get("last_error", ""),
                warning=st.get("warning", ""),
                auto_record=bool((room.get("params") or {}).get("auto_record")),
                recording=bool(st.get("recording")),
                record_started_at=as_int(st.get("record_started_at")),
                record_bytes=as_int(st.get("record_bytes")),
                record_path=st.get("record_path", ""),
                record_error=st.get("record_error", ""),
                record_qn=as_int(st.get("record_qn"), 250),
                record_qn_actual=as_int(st.get("record_qn_actual")),
                record_status=st.get("record_status", "idle"),
                counts=st.get("counts", {}),
                dyns=(dyn_by_mid.get(as_int(room.get("uid"))) or {}).get("n", 0),
                dyn_last_at=(dyn_by_mid.get(as_int(room.get("uid"))) or {}).get("last_at", 0),
                dyn_schedule="%02d:%02d" % (as_int(room.get("dyn_hour"), 8),
                                            as_int(room.get("dyn_minute"), 0)),
                dyn_auto=bool(room.get("dyn_auto")),
                dyn_last_ts=as_int(room.get("dyn_last_ts")),
                last_live_at=agg.get("last_live_at", 0),
                totals={"danmaku": agg.get("danmaku", 0),
                        "sc": agg.get("sc", 0),
                        "events": agg.get("events", 0),
                        "sc_amount": agg.get("sc_amount", 0),
                        "sessions": agg.get("sessions", 0),
                        "sessions_approx": bool(agg.get("sessions_approx")),
                        "viewers": agg.get("viewers", 0),
                        "interactions": agg.get("interactions", 0),
                        "sc_peak": agg.get("sc_peak", 0),
                        "last_at": agg.get("last_at", 0)}))
        return {"ok": True, "items": items, "active": snap["active"],
                "running": snap["running"]}

    def room_state(self, room_id: int) -> dict[str, Any]:
        for state in self.manager.snapshot()["rooms"]:
            if state["room_id"] == room_id:
                return state
        return {}

    def is_running(self, room_id: int) -> bool:
        return bool(self.room_state(room_id).get("running"))

    # ---------- 生命周期 ----------

    def auto_start_rooms(self) -> None:
        for room in (self.rooms.list().get("items") or []):
            if not room.get("collect_live", True):
                continue
            ok, message = self.manager.start(room["id"])
            self.logs.add("info" if ok else "error",
                          "自动开始采集 %s：%s" % (room["id"], message))

    def shutdown(self) -> None:
        self.dynamics.stop()
        self.manager.stop_all()
        self.logs.close()
