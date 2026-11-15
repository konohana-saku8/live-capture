# -*- coding: utf-8 -*-
"""HTTP server: dashboard / rooms / config / logs web UI + REST API."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from accounts import AccountStore
from capture import WS_IMPORT_ERROR, fetch_room_brief
from dynamics import DynamicsFetcher
from images import EmoteProxy, FALLBACK_PNG
from rooms import PARAM_KEYS, RoomStore
import sysinfo

DEFAULTS = {
    "protover": 2,                 # 2=zlib (no extra deps), 3=brotli
    "heartbeat_seconds": 30,
    "stale_seconds": 120,
    "http_timeout": 10,
    "record_danmaku": True,
    "record_sc": True,
    "record_events": True,
    "host": "0.0.0.0",
    "port": 8765,
    "token": "",
    "auto_start": False,
    "dyn_mid": 2094031249,     # 主播 uid，动态抓取目标
    "dyn_auto": True,          # 每天定时拉取
    "dyn_hour": 8,             # 本地时间每天几点拉取
    "dyn_last_ts": 0,          # 上次成功拉取时间（内部用）
}
SECRET_KEYS = ("token",)
BOOL_KEYS = ("record_danmaku", "record_sc", "record_events", "auto_start",
             "dyn_auto")
INT_BOUNDS = {
    "protover": (2, 3),
    "heartbeat_seconds": (10, 120),
    "stale_seconds": (30, 600),
    "http_timeout": (3, 60),
    "port": (1, 65535),
    "dyn_mid": (1, 999999999),
    "dyn_hour": (0, 23),
}
VALID_KINDS = ("danmaku", "sc", "events")


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                saved = json.load(handle)
            if isinstance(saved, dict):
                with self.lock:
                    self.data.update({k: v for k, v in saved.items()
                                      if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        return self.get()

    def get(self):
        with self.lock:
            return dict(self.data)

    def save(self, patch):
        if not isinstance(patch, dict):
            raise ValueError("配置必须是 JSON 对象")
        clean = {}
        for key, value in patch.items():
            if key not in DEFAULTS:
                continue
            if key in BOOL_KEYS:
                clean[key] = bool(value)
            elif key in INT_BOUNDS:
                low, high = INT_BOUNDS[key]
                try:
                    num = int(value)
                except (TypeError, ValueError):
                    raise ValueError("%s 必须是数字" % key)
                if not low <= num <= high:
                    raise ValueError("%s 需要在 %s–%s 之间" % (key, low, high))
                clean[key] = num
            elif key in ("host", "token"):
                clean[key] = str(value).strip()
            else:
                clean[key] = value
        with self.lock:
            self.data.update(clean)
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return dict(self.data)

    @staticmethod
    def masked(config):
        out = dict(config)
        for key in SECRET_KEYS:
            value = str(out.get(key) or "")
            out[key + "_set"] = bool(value)
            if value:
                out[key] = (value[:2] + "…" + value[-2:]) if len(value) > 6 else "****"
        return out


class LogBuffer:
    def __init__(self, capacity=1000):
        self.capacity = capacity
        self.lock = threading.RLock()
        self.items = []

    def add(self, level, message):
        with self.lock:
            self.items.append({"ts": int(time.time() * 1000),
                               "level": level, "msg": str(message)})
            if len(self.items) > self.capacity:
                self.items = self.items[-self.capacity:]
        print("[%s] %s" % (level, message), flush=True)

    def tail(self, limit=100):
        with self.lock:
            return list(self.items[-max(1, min(1000, limit)):])


def _lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class DynamicScheduler:
    """Pull the anchor's public dynamics once a day, plus optional startup pull.

    Keeps a last-pull timestamp in the config so a restart does not refetch
    repeatedly within the same day. All work is best-effort and logged.
    """

    def __init__(self, store, config, logs, fetcher_cls=DynamicsFetcher):
        self.store = store
        self.config = config
        self.logs = logs
        self.fetcher_cls = fetcher_cls
        self.lock = threading.RLock()
        self.last_result = {"ok": False, "msg": "尚未拉取", "ts": 0}
        self._stop = threading.Event()
        self._thread = None

    def pull(self, reason="手动", limit=20):
        cfg = self.config.get()
        mid = _as_int(cfg.get("dyn_mid"))
        if not mid:
            return {"ok": False, "msg": "未设置主播 uid", "ts": int(time.time()*1000)}
        try:
            fetcher = self.fetcher_cls()
            items, _ = fetcher.fetch(mid, limit=limit)
            n = self.store.upsert_dynamics(items)
            result = {"ok": True, "msg": "%s拉取 %d 条（新增/更新 %d）" %
                      (reason, len(items), n), "ts": int(time.time() * 1000),
                      "count": len(items)}
            self.logs.add("info", result["msg"])
        except Exception as exc:
            result = {"ok": False, "msg": "%s拉取失败：%s" % (reason, exc),
                      "ts": int(time.time() * 1000)}
            self.logs.add("error", result["msg"])
        with self.lock:
            self.last_result = result
        return result

    def status(self):
        with self.lock:
            out = dict(self.last_result)
        total, _ = self.store.list_dynamics(limit=1)
        out["total"] = total
        return out

    def start(self):
        cfg = self.config.get()
        if not cfg.get("dyn_auto"):
            return
        # 启动时若今天还没拉过就补一次（后台线程，不阻塞启动）
        last = _as_int(cfg.get("dyn_last_ts"))
        today = time.strftime("%Y-%m-%d", time.localtime())
        last_day = time.strftime("%Y-%m-%d", time.localtime(last / 1000)) \
            if last else ""
        if last_day != today:
            threading.Thread(target=self._startup_pull, daemon=True).start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _startup_pull(self):
        time.sleep(3)  # 让服务先起来
        res = self.pull("启动补拉")
        if res.get("ok"):
            self.config.save({"dyn_last_ts": res["ts"]})

    def _loop(self):
        while not self._stop.is_set():
            now = time.localtime()
            # 计算到下一次目标 hour:00 的秒数
            target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                  _as_int(self.config.get().get("dyn_hour"), 8),
                                  0, 0, 0, 0, -1))
            if target <= time.time():
                target += 86400
            wait = max(60, target - time.time())
            if self._stop.wait(wait):
                break
            res = self.pull("定时")
            if res.get("ok"):
                self.config.save({"dyn_last_ts": res["ts"]})

    def stop(self):
        self._stop.set()


class Handler(BaseHTTPRequestHandler):
    server_version = "LiveCapture/2.0"

    def log_message(self, fmt, *args):
        return

    # ---------- low level ----------

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        out = {
            "Content-Type": ctype,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, X-Access-Token",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        }
        out.update(headers or {})       # allow callers to override Cache-Control
        self.send_response(code)
        for key, value in out.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _fail(self, code, message):
        self._json({"ok": False, "error": message}, code)

    def _parts(self):
        split = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(split.query)
        return split.path, {k: v[0] for k, v in query.items()}

    def _authorized(self, query):
        token = str(self.server.config.get().get("token") or "")
        if not token:
            return True
        supplied = (self.headers.get("X-Access-Token") or "").strip()
        if not supplied:
            supplied = (query.get("token") or "").strip()
        return supplied == token

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 512 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求体不是合法 JSON")
        if not isinstance(parsed, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return parsed

    # ---------- verbs ----------

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path, query = self._parts()
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(self.server.web_dir, "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._fail(500, "找不到 web/index.html")
            return
        if path == "/favicon.ico":
            self._send(204, b"")
            return
        if path == "/api/health":
            self._json({"ok": True, "ts": int(time.time() * 1000)})
            return
        if path == "/api/gate":
            # 公开：进站锁屏用它判断"是否需要密码 / 是否已设置密码"。
            # 复用 config.token 作为访问密码；未设置时前端进入首次设置流程。
            need = bool(str(self.server.config.get().get("token") or ""))
            self._json({"ok": True, "need": need})
            return
        if path == "/api/emote":
            # <img> cannot send X-Access-Token, and this endpoint only ever
            # serves whitelisted public CDN pictures, so it stays public.
            self._serve_emote(query)
            return
        if not path.startswith("/api/"):
            self._fail(404, "没有这个地址")
            return
        if not self._authorized(query):
            self._fail(401, "需要访问口令")
            return
        try:
            self._api_get(path, query)
        except Exception as exc:
            self._fail(500, str(exc))

    def do_POST(self):
        path, query = self._parts()
        if not self._authorized(query):
            self._fail(401, "需要访问口令")
            return
        try:
            body = self._body()
            self._api_post(path, body)
        except ValueError as exc:
            self._fail(400, str(exc))
        except Exception as exc:
            self._fail(500, str(exc))

    # ---------- helpers ----------

    def _service(self):
        return {"started_at": self.server.started_at,
                "host": self.server.host, "port": self.server.port,
                "lan_ip": self.server.lan_ip,
                "data_dir": self.server.data_dir,
                "websockets": self.server.ws_ok}

    def _rooms_payload(self):
        rooms = self.server.rooms.list().get("items") or []
        snap = self.server.manager.snapshot()
        states = {s["room_id"]: s for s in snap["rooms"]}
        totals = {x["room_id"]: x for x in
                  self.server.store.dashboard().get("by_room") or []}
        items = []
        for room in rooms:
            st = states.get(room["id"]) or {}
            agg = totals.get(room["id"]) or {}
            live_status = _as_int(st.get("live_status") or
                                  room.get("live_status"))
            # 本场直播的数据边界：优先用采集时看到的 LIVE 事件，
            # 否则用房间接口返回的 live_time（本场开播的 unix 秒）
            since_ms = _as_int(st.get("live_started_at"))
            if not since_ms and live_status == 1:
                since_ms = _as_int(room.get("live_time")) * 1000
            live_stats = (self.server.store.session_stats(room["id"], since_ms)
                          if live_status == 1 and since_ms else None)
            items.append(dict(
                room,
                running=bool(st.get("running")),
                configured=True,
                real_room_id=st.get("real_room_id") or room["id"],
                live_status=live_status,
                uname=st.get("uname") or room.get("uname", ""),
                face=room.get("face", ""),
                live_time=_as_int(room.get("live_time")),
                live_started_at=_as_int(st.get("live_started_at")),
                live_stats=live_stats,
                online=st.get("online") or room.get("online", 0),
                popularity=st.get("popularity", 0),
                last_packet_at=st.get("last_packet_at", 0),
                started_at=st.get("started_at", 0),
                last_error=st.get("last_error", ""),
                warning=st.get("warning", ""),
                counts=st.get("counts", {}),
                totals={"danmaku": agg.get("danmaku", 0),
                        "sc": agg.get("sc", 0),
                        "events": agg.get("events", 0),
                        "sc_amount": agg.get("sc_amount", 0),
                        "sessions": agg.get("sessions", 0),
                        "viewers": agg.get("viewers", 0),
                        "interactions": agg.get("interactions", 0),
                        "sc_peak": agg.get("sc_peak", 0),
                        "last_at": agg.get("last_at", 0)}))
        return {"ok": True, "items": items, "active": snap["active"],
                "running": snap["running"]}

    def _capture_payload(self, ok, message, code=200):
        self._json({"ok": ok, "message": message,
                    "capture": self.server.manager.snapshot(),
                    "rooms": self._rooms_payload()}, code)

    def _first_room_id(self):
        items = self.server.rooms.list().get("items") or []
        return _as_int(items[0]["id"]) if items else 0

    # ---------- image proxy ----------

    def _serve_emote(self, query):
        """Return a CDN picture from our own origin (no Referer trap).

        A 502 with a transparent PNG (instead of JSON) lets the browser fire
        a second ``error`` event, so the UI can replace the broken image with
        its alt text.
        """
        try:
            ctype, body = self.server.images.get(query.get("u") or "")
        except Exception:
            self._send(502, FALLBACK_PNG, "image/png",
                       {"Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff"})
            return
        self._send(200, body, ctype,
                   {"Cache-Control": "public, max-age=21600",
                    "X-Content-Type-Options": "nosniff"})

    # ---------- api GET ----------

    def _api_get(self, path, q):
        store, manager, config = (self.server.store, self.server.manager,
                                  self.server.config)
        if path == "/api/status":
            self._json({"ok": True, "service": self._service(),
                        "capture": manager.snapshot(),
                        "rooms": self._rooms_payload(),
                        "config": Config.masked(config.get())})
            return
        if path == "/api/dashboard":
            data = store.dashboard()
            self._json({"ok": True,
                        "totals": data["totals"], "by_room": data["by_room"],
                        "top_sc": data["top_sc"], "recent": data["recent"],
                        "timeline": data["timeline"],
                        "last_received_at": data["last_received_at"],
                        "rooms": self._rooms_payload(),
                        "capture": manager.snapshot(),
                        "service": self._service(),
                        "config": Config.masked(config.get())})
            return
        if path == "/api/rooms":
            self._json(self._rooms_payload())
            return
        if path == "/api/feed":
            rid = _as_int(q.get("room_id")) or self._first_room_id()
            kinds = [k for k in (q.get("types") or "danmaku,sc,events").split(",")
                     if k in VALID_KINDS]
            since = q.get("since")
            until = q.get("until")
            all_mode = str(q.get("all") or "").lower() in ("1", "true", "yes")
            items, counts = {}, {}
            for kind in kinds:
                if all_mode:
                    rows = store.query_all(kind, q=q.get("q") or "",
                                           room_id=rid or None,
                                           since=since, until=until)
                    items[kind] = rows
                    counts[kind] = len(rows)
                else:
                    limit = max(1, min(300, _as_int(q.get("limit"), 40)))
                    total, rows = store.query(kind, limit=limit,
                                              q=q.get("q") or "",
                                              room_id=rid or None,
                                              since=since, until=until)
                    items[kind] = rows
                    counts[kind] = total
            self._json({"ok": True, "room_id": rid, "items": items,
                        "counts": counts, "all": all_mode})
            return
        if path == "/api/system":
            self._json({"ok": True, "system": self.server.sys_snapshot()})
            return
        if path == "/api/sessions":
            rid = _as_int(q.get("room_id")) or self._first_room_id()
            sessions = store.live_sessions(rid)
            self._json({"ok": True, "room_id": rid, "sessions": sessions,
                        "total": len(sessions)})
            return
        if path == "/api/dynamics":
            total, items = store.list_dynamics(
                limit=q.get("limit") or 200, q=q.get("q") or "")
            self._json({"ok": True, "total": total, "items": items,
                        "status": self.server.dynamics.status(),
                        "config": {"dyn_mid": _as_int(config.get().get("dyn_mid")),
                                   "dyn_auto": bool(config.get().get("dyn_auto")),
                                   "dyn_hour": _as_int(config.get().get("dyn_hour"), 8)}})
            return
        if path == "/api/config":
            self._json({"ok": True, "config": Config.masked(config.get()),
                        "defaults": DEFAULTS})
            return
        if path == "/api/logs":
            try:
                limit = int(q.get("limit") or 100)
            except (TypeError, ValueError):
                limit = 100
            self._json({"ok": True, "items": self.server.logs.tail(limit)})
            return
        if path == "/api/accounts":
            self._json(self.server.accounts.list())
            return
        kinds = {"/api/danmaku": "danmaku",
                 "/api/superchats": "sc",
                 "/api/events": "events"}
        if path in kinds:
            total, items = store.query(
                kinds[path],
                limit=q.get("limit") or 100,
                offset=q.get("offset") or 0,
                q=q.get("q") or "",
                room_id=q.get("room_id"),
                since=q.get("since"),
                run_id=q.get("run_id"))
            self._json({"ok": True, "total": total, "items": items})
            return
        if path == "/api/export":
            kind = q.get("type") or "danmaku"
            fmt = q.get("format") or "json"
            if kind not in VALID_KINDS or fmt not in ("json", "csv"):
                self._fail(400, "type/format 不合法")
                return
            name, ctype, body = store.export(
                kind, fmt, q=q.get("q") or "", room_id=q.get("room_id"),
                since=q.get("since"))
            self._send(200, body, ctype, {
                "Content-Disposition": 'attachment; filename="%s"' % name})
            return
        self._fail(404, "没有这个接口")

    # ---------- api POST ----------

    def _api_post(self, path, body):
        manager, config = self.server.manager, self.server.config
        rooms, accounts = self.server.rooms, self.server.accounts

        # ---- room management ----
        if path == "/api/rooms":
            rid = _as_int(body.get("room_id"))
            if rid <= 0:
                self._fail(400, "房间号必须是正整数")
                return
            label = str(body.get("label") or "")[:80]
            warning = ""
            try:
                brief = fetch_room_brief(rid, timeout=3)
            except Exception as exc:
                brief, warning = {}, "直播间信息获取失败，先用房间号：%s" % exc
            try:
                item, created = rooms.add(rid, label, brief)
            except ValueError as exc:
                self._fail(400, str(exc))
                return
            self.server.logs.add(
                "info", "%s直播间：%s（%s）" %
                ("新增" if created else "更新", item["label"], rid))
            self._json({"ok": True, "room": item, "created": created,
                        "warning": warning, "rooms": self._rooms_payload()})
            return
        if path == "/api/rooms/remove":
            rid = _as_int(body.get("room_id"))
            if manager.snapshot()["running"] and any(
                    s["room_id"] == rid and s["running"]
                    for s in manager.snapshot()["rooms"]):
                manager.stop(rid)
            ok = rooms.remove(rid)
            self.server.logs.add("info", "删除直播间：%s（%s）" %
                                 ("成功" if ok else "不存在", rid))
            self._json({"ok": ok, "rooms": self._rooms_payload(),
                        "capture": manager.snapshot()})
            return
        if path == "/api/rooms/settings":
            rid = _as_int(body.get("room_id"))
            params = body.get("params")
            if not isinstance(params, dict):
                self._fail(400, "params 必须是 JSON 对象")
                return
            clean = {}
            for key, value in params.items():
                if key not in PARAM_KEYS:
                    continue
                if key in BOOL_KEYS:
                    clean[key] = bool(value)
                elif key in INT_BOUNDS:
                    low, high = INT_BOUNDS[key]
                    try:
                        num = int(value)
                    except (TypeError, ValueError):
                        self._fail(400, "%s 必须是数字" % key)
                        return
                    if not low <= num <= high:
                        self._fail(400, "%s 需要在 %s–%s 之间" % (key, low, high))
                        return
                    clean[key] = num
            item = rooms.settings(rid, clean)
            if not item:
                self._fail(404, "直播间不存在")
                return
            running = any(s["room_id"] == rid and s["running"]
                          for s in manager.snapshot()["rooms"])
            self.server.logs.add(
                "info", "保存直播间参数：%s（%s）" % (item["label"], rid))
            self._json({"ok": True, "room": item, "restart_required": running,
                        "rooms": self._rooms_payload()})
            return
        if path == "/api/rooms/refresh":
            updated = 0
            for room in (rooms.list().get("items") or [])[:20]:
                try:
                    brief = fetch_room_brief(room["id"], timeout=3)
                except Exception:
                    continue
                if rooms.refresh(room["id"], brief):
                    updated += 1
            self.server.logs.add("info", "刷新直播间信息：%s 个" % updated)
            self._json({"ok": True, "updated": updated,
                        "rooms": self._rooms_payload()})
            return

        # ---- dynamics ----
        if path == "/api/dynamics/fetch":
            if _as_int(body.get("dyn_mid")) > 0:
                config.save({"dyn_mid": _as_int(body.get("dyn_mid"))})
            res = self.server.dynamics.pull("手动", limit=_as_int(body.get("limit"), 20) or 20)
            if res.get("ok"):
                config.save({"dyn_last_ts": res["ts"]})
            total, items = self.server.store.list_dynamics(limit=200)
            self._json({"ok": res["ok"], "message": res["msg"],
                        "total": total, "items": items,
                        "status": self.server.dynamics.status()},
                       200 if res["ok"] else 502)
            return
        if path == "/api/dynamics/trans":
            dyn_id = str(body.get("dyn_id") or "")
            if not dyn_id:
                self._fail(400, "缺少 dyn_id")
                return
            ok = self.server.store.update_dyn_translation(
                dyn_id, str(body.get("text") or ""))
            self._json({"ok": ok, "dyn_id": dyn_id})
            return
        if path == "/api/dynamics/delete":
            dyn_id = str(body.get("dyn_id") or "")
            ok = self.server.store.delete_dynamic(dyn_id) if dyn_id else False
            total, items = self.server.store.list_dynamics(limit=200)
            self._json({"ok": ok, "total": total, "items": items})
            return
        if path == "/api/dynamics/config":
            patch = {}
            if "dyn_auto" in body:
                patch["dyn_auto"] = bool(body["dyn_auto"])
            if _as_int(body.get("dyn_hour"), -1) >= 0:
                patch["dyn_hour"] = _as_int(body["dyn_hour"])
            if _as_int(body.get("dyn_mid")) > 0:
                patch["dyn_mid"] = _as_int(body["dyn_mid"])
            if patch:
                config.save(patch)
            self._json({"ok": True,
                        "config": {"dyn_mid": _as_int(config.get().get("dyn_mid")),
                                   "dyn_auto": bool(config.get().get("dyn_auto")),
                                   "dyn_hour": _as_int(config.get().get("dyn_hour"), 8)}})
            return

        # ---- capture control ----
        if path in ("/api/capture/start", "/api/start"):
            rid = _as_int(body.get("room_id")) or self._first_room_id()
            ok, message = manager.start(rid)
            self._capture_payload(ok, message, 200 if ok else 409)
            return
        if path in ("/api/capture/stop", "/api/stop"):
            rid = _as_int(body.get("room_id"))
            ok, message = manager.stop(rid or None)
            self._capture_payload(ok, message)
            return
        if path == "/api/capture/stop_all":
            ok, message = manager.stop_all()
            self._capture_payload(ok, message)
            return

        # ---- accounts (CSV only) ----
        if path == "/api/accounts":
            try:
                item, created, error = accounts.add(
                    body.get("name") or "", body.get("csv") or "",
                    activate=body.get("activate", True) is not False)
            except ValueError as exc:
                self._fail(400, str(exc))
                return
            self.server.logs.add(
                "info", "%s cookie 账号：%s%s" %
                ("新增" if created else "更新", item["label"],
                 "（%s）" % error if error else ""))
            self._json({"ok": True, "item": item, "accounts": accounts.list(),
                        "restart_required": manager.snapshot()["running"]})
            return
        if path == "/api/accounts/remove":
            ok = accounts.remove(body.get("id") or "")
            self.server.logs.add(
                "info", "删除 cookie 账号：%s" % ("成功" if ok else "账号不存在"))
            self._json({"ok": ok, "accounts": accounts.list(),
                        "restart_required": manager.snapshot()["running"]})
            return
        if path == "/api/accounts/activate":
            try:
                out, label = accounts.activate(body.get("id") or "")
            except ValueError as exc:
                self._fail(400, str(exc))
                return
            self.server.logs.add("info", "启用 cookie 账号：%s" %
                                 (label or "已停用（回退匿名采集）"))
            self._json({"ok": True, "accounts": out,
                        "restart_required": manager.snapshot()["running"]})
            return

        # ---- config / data ----
        if path == "/api/config":
            before = config.get()
            after = config.save(body)
            self.server.logs.add("info", "配置已保存")
            changed = any(before.get(k) != after.get(k) for k in DEFAULTS)
            self._json({"ok": True, "config": Config.masked(after),
                        "restart_required": changed and manager.snapshot()["running"]})
            return
        if path == "/api/clear":
            if body.get("confirm") != "yes":
                self._fail(400, '需要 {"confirm":"yes"}')
                return
            rid = _as_int(body.get("room_id"))
            self.server.store.clear(rid or None)
            self.server.logs.add("warn", "数据已清空：%s" %
                                 ("房间 %s" % rid if rid else "全部"))
            self._json({"ok": True})
            return
        self._fail(404, "没有这个接口")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(host, port, data_dir, web_dir, store, manager, config, logs,
                  accounts=None, rooms=None):
    """Build the server without blocking; used by run.py and selftest.py."""
    srv = AppServer((host, port), Handler)
    srv.store = store
    srv.manager = manager
    srv.config = config
    srv.logs = logs
    srv.accounts = accounts or AccountStore(
        os.path.join(data_dir, "accounts.json"))
    srv.rooms = rooms or RoomStore(os.path.join(data_dir, "rooms.json"))
    srv.images = EmoteProxy()
    srv.dynamics = DynamicScheduler(store, config, logs)
    srv.cpu_sampler = sysinfo.CpuSampler()

    def _sys_snapshot():
        return sysinfo.snapshot(data_dir, srv.cpu_sampler, srv.started_at)

    srv.sys_snapshot = _sys_snapshot
    # 采集线程在用户点「启动」时才读账号与房间列表，这里挂上即可
    if getattr(manager, "accounts", None) is None:
        manager.accounts = srv.accounts
    if getattr(manager, "rooms", None) is None:
        manager.rooms = srv.rooms
    srv.web_dir = web_dir
    srv.data_dir = data_dir
    srv.host = host
    srv.port = port
    srv.started_at = int(time.time() * 1000)
    srv.lan_ip = _lan_ip()
    srv.ws_ok = not bool(WS_IMPORT_ERROR)
    return srv


def serve(host, port, data_dir, web_dir, store, manager, config, logs,
          rooms=None, auto_start=False):
    srv = create_server(host, port, data_dir, web_dir, store, manager,
                        config, logs, rooms=rooms)
    ip = srv.lan_ip
    print("=" * 52, flush=True)
    print("  直播弹幕 / SC 采集服务已启动", flush=True)
    print("  本机：http://127.0.0.1:%s/" % port, flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("  局域网：http://%s:%s/" % (ip, port), flush=True)
    print("  数据：%s" % os.path.abspath(data_dir), flush=True)
    print("  直播间：%s 个" % len(srv.rooms.list().get("items") or []), flush=True)
    print("=" * 52, flush=True)
    if auto_start:
        for room in (srv.rooms.list().get("items") or []):
            ok, message = manager.start(room["id"])
            logs.add("info" if ok else "error",
                     "自动开始采集 %s：%s" % (room["id"], message))
    srv.dynamics.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.dynamics.stop()
        manager.stop_all()
        srv.server_close()
