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
import raw_fields

from .scheduler import DynamicScheduler
from .notifier import NotifierScheduler
from .settings import (CONTENT_KEYS, Config, LogBuffer, as_int,
                       install_excepthooks, lan_ip)


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
        # 警告/错误另外落一份 jsonl，服务重启后回填到页面：
        # 手机上的进程随时会被系统杀掉，内存日志一丢就查不到原因了。
        self.logs.bind_journal(os.path.join(self.data_dir, "logs",
                                            "journal.jsonl"))
        self.logs.install_root()
        install_excepthooks(self.logs)
        self.logs.replay()
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
                                         rooms=self.rooms, accounts=self.accounts)
        # 开播 / 新动态推送：独立线程，分钟级轮询，和上面的定时采集互不影响
        self.notifier = NotifierScheduler(self.store, self.config, self.logs,
                                          self.rooms, self.data_dir,
                                          dynamics=self.dynamics,
                                          accounts=self.accounts)
        # 首页 / 采集器页每 10 秒拉一次仪表盘；手机上一次聚合要 1-2 秒，
        # 缓存十几秒，避免重复全表扫描。数据最多晚十几秒，不影响使用。
        self._dash_lock = threading.Lock()
        self._dash_cached_at = 0.0
        self._dash_cache = None
        self._data_rooms_lock = threading.Lock()
        self._data_rooms_cache: Any = None
        self._dash_refreshing = False
        self.started_at = int(time.time() * 1000)
        self.host = host
        self.port = port
        self.lan_ip = lan_ip()
        self.ws_ok = not bool(WS_IMPORT_ERROR)
        self.fetcher_cls = DynamicsFetcher
        # 存储明细由 Store 在后台算，算完直接补进首页缓存，省得等整体重算
        self.store.on_storage_ready = self._patch_storage

    # ---------- 公共载荷 ----------

    def dashboard(self) -> dict[str, Any]:
        """仪表盘数据：命中缓存直接返回，过期了交给后台刷新，请求不等重算。

        手机上完整聚合一次要两秒多，前端每 10 秒轮询一次，同步重算会把整个
        请求线程连同全局锁一起占住。这里换成「先给旧值、后台再算」，页面永远
        是毫秒级返回。
        """
        with self._dash_lock:
            if self._dash_cache is not None:
                if time.time() - self._dash_cached_at >= self.DASH_TTL:
                    self._dash_refresh_async()
                return self._dash_cache
            # 服务刚起、还没有任何缓存：第一次只能同步算
            data = self._compute_dashboard()
            self._dash_cache = data
            self._dash_cached_at = time.time()
            return data

    DASH_TTL = 30.0

    def _compute_dashboard(self) -> dict[str, Any]:
        data = self.store.dashboard()
        labels = {}
        for room in (self.rooms.list().get("items") or []):
            labels[as_int(room["id"])] = room.get("uname") or room.get("label") or ""
        data["data_status"] = self.store.data_status(labels)
        return data

    def _dash_refresh_async(self) -> None:
        """后台重算一次仪表盘，算完替换缓存；已有任务在跑就不重复起线程。"""
        if self._dash_refreshing:
            return
        self._dash_refreshing = True

        def run():
            try:
                data = self._compute_dashboard()
                with self._dash_lock:
                    # 期间被 invalidate（删过数据）就丢弃这次结果
                    if self._dash_cached_at:
                        self._dash_cache = data
                        self._dash_cached_at = time.time()
            except Exception:  # noqa: BLE001 - 后台任务失败保留旧数据
                pass
            finally:
                self._dash_refreshing = False

        threading.Thread(target=run, daemon=True, name="dash-refresh").start()

    def _patch_storage(self, storage: dict[str, Any]) -> None:
        """存储明细算完后就地替换首页缓存里的那一块，不用整体重算。"""
        with self._dash_lock:
            if not self._dash_cache:
                return
            status = self._dash_cache.get("data_status")
            if isinstance(status, dict):
                status["storage"] = storage

    def invalidate_dashboard(self) -> None:
        """删除数据后让下一次仪表盘请求重新聚合，避免看到旧数字。"""
        with self._dash_lock:
            self._dash_cache = None
            self._dash_cached_at = 0.0
        with self._data_rooms_lock:
            self._data_rooms_cache = None
        self.store.invalidate_segments()
        self.store.invalidate_storage()
        self.store.invalidate_viewers()

    def service(self) -> dict[str, Any]:
        return {"started_at": self.started_at,
                "host": self.host, "port": self.port,
                "lan_ip": self.lan_ip,
                "data_dir": self.data_dir,
                "websockets": self.ws_ok}

    def sys_snapshot(self) -> dict[str, Any]:
        return sysinfo.snapshot(self.data_dir, self.cpu_sampler, self.started_at)

    def diagnostics(self) -> dict[str, Any]:
        """线程 / 磁盘 / 数据库 / ffmpeg 的即时快照，给日志页用。"""
        from . import diagnostics
        return diagnostics.snapshot(self)

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

    def data_rooms_payload(self) -> dict[str, Any]:
        """数据页的房间清单：采集器 + 库里还留着数据的房间。

        采集器删掉后弹幕 / SC / 事件 / 录像仍在库里，这些房间不会出现在
        ``/api/rooms``（那是采集器清单）。数据页只看数据，所以按数据侧并集
        列出，并标出 ``configured``——没有采集器的房间仍能看场次和录像，
        只是没有直播状态与动态。
        """
        recs: dict[int, dict[str, Any]] = {}
        for rec in self.store.list_recordings():
            rid = as_int(rec.get("room_id"))
            if not rid:
                continue
            item = recs.setdefault(rid, {"count": 0, "bytes": 0,
                                         "label": "", "last_at": 0})
            item["count"] += 1
            item["bytes"] += as_int(rec.get("bytes"))
            item["last_at"] = max(item["last_at"], as_int(rec.get("started_at")))
            if not item["label"] and rec.get("label"):
                item["label"] = str(rec.get("label"))

        # 只用 store 的聚合，不碰 runtime.dashboard()——那会顺带算存储明细，
        # 数据页打开时没必要为这个等一两秒。
        with self._data_rooms_lock:
            cached = self._data_rooms_cache
            if cached and time.time() - cached[0] < 15:
                agg = cached[1]
            else:
                agg = {as_int(x.get("room_id")): x
                       for x in self.store.dashboard().get("by_room") or []}
                self._data_rooms_cache = (time.time(), agg)
        rooms = {as_int(r["id"]): r
                 for r in (self.rooms.list().get("items") or [])}

        items = []
        for rid in set(agg) | set(recs) | set(rooms):
            room = rooms.get(rid) or {}
            a = agg.get(rid) or {}
            rec = recs.get(rid) or {}
            items.append({
                "id": rid,
                "configured": bool(room),
                "uid": as_int(room.get("uid")),
                "uname": room.get("uname", ""),
                "label": (room.get("label") or rec.get("label")
                          or ("房间 " + str(rid))),
                "face": room.get("face", ""),
                "live_status": as_int(room.get("live_status")),
                "totals": {"danmaku": a.get("danmaku", 0),
                           "sc": a.get("sc", 0),
                           "events": a.get("events", 0),
                           "interactions": a.get("interactions", 0),
                           "sessions": a.get("sessions", 0)},
                "recordings": rec.get("count", 0),
                "rec_bytes": rec.get("bytes", 0),
                "has_data": bool(a or rec),
                "last_at": max(as_int(a.get("last_at")),
                               as_int(rec.get("last_at"))),
            })
        # 最近有数据的在前；只有采集器、一条数据都没有的排后面
        items.sort(key=lambda x: (-x["last_at"], not x["configured"], x["id"]))
        return {"ok": True, "items": items}

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
        self.notifier.stop()
        self.dynamics.stop()
        self.recordings.stop()
        self.manager.stop_all()
        # 采集线程是 daemon，join 在独立线程里做；这里直接把原始报文缓冲刷掉，
        # 免得起停一次丢最后几十秒的报文。
        raw_fields.flush_all()
        self.logs.close()
