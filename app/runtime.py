"""运行期容器：把 store / manager / rooms / accounts 等单例挂在一处。

Flask 的 `current_app.extensions["live"]` 就是它，Blueprint 里统一通过
`from .runtime import rt` 取用，避免每个函数都写一遍 app.config 读取。
"""
from __future__ import annotations

import os
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
from .settings import Config, LogBuffer, as_int, lan_ip


class Runtime:
    def __init__(self, data_dir: str, web_dir: str, host: str = "0.0.0.0",
                 port: int = 8765):
        self.data_dir = os.path.abspath(data_dir)
        self.web_dir = os.path.abspath(web_dir)
        os.makedirs(self.data_dir, exist_ok=True)

        self.logs = LogBuffer()
        self.config = Config(os.path.join(self.data_dir, "config.json"))
        self.rooms = RoomStore(os.path.join(self.data_dir, "rooms.json"))
        self.accounts = AccountStore(os.path.join(self.data_dir, "accounts.json"))
        self.store = Store(os.path.join(self.data_dir, "capture.db"))
        self.manager = CaptureManager(self.store, self.config.get,
                                      self.logs.add, rooms=self.rooms)
        self.manager.accounts = self.accounts
        self.images = EmoteProxy()
        self.cpu_sampler = sysinfo.CpuSampler()
        self.dynamics = DynamicScheduler(self.store, self.config, self.logs,
                                         rooms=self.rooms)
        self.started_at = int(time.time() * 1000)
        self.host = host
        self.port = port
        self.lan_ip = lan_ip()
        self.ws_ok = not bool(WS_IMPORT_ERROR)
        self.fetcher_cls = DynamicsFetcher

    # ---------- 公共载荷 ----------

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
        dash = self.store.dashboard()
        totals = {x["room_id"]: x for x in dash.get("by_room") or []}
        dyn_by_mid = dash.get("dyn_by_mid") or {}
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
            items.append(dict(
                room,
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
