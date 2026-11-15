# -*- coding: utf-8 -*-
"""HTTP server: one web page for config/control + a small read-only REST API."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULTS = {
    "room_id": 22748536,
    "sessdata": "",
    "dede_user_id": "",
    "buvid3": "",
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
}
SECRET_KEYS = ("sessdata", "dede_user_id", "buvid3", "token")
BOOL_KEYS = ("record_danmaku", "record_sc", "record_events", "auto_start")
INT_BOUNDS = {
    "room_id": (1, 999999999),
    "protover": (2, 3),
    "heartbeat_seconds": (10, 120),
    "stale_seconds": (30, 600),
    "http_timeout": (3, 60),
    "port": (1, 65535),
}


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
            elif key in ("host", "sessdata", "dede_user_id", "buvid3", "token"):
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
    def __init__(self, capacity=300):
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
            return list(self.items[-max(1, min(300, limit)):])


def _lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "LiveCapture/1.0"

    def log_message(self, fmt, *args):
        return

    # ---------- low level ----------

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Access-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for key, value in (headers or {}).items():
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
        if length > 65536:
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

    # ---------- api ----------

    def _api_get(self, path, q):
        store, manager, config = self.server.store, self.server.manager, self.server.config
        if path == "/api/status":
            self._json({"ok": True,
                        "service": {"started_at": self.server.started_at,
                                    "host": self.server.host,
                                    "port": self.server.port,
                                    "lan_ip": self.server.lan_ip,
                                    "data_dir": self.server.data_dir,
                                    "websockets": self.server.ws_ok},
                        "capture": manager.snapshot(),
                        "config": Config.masked(config.get())})
            return
        if path == "/api/config":
            self._json({"ok": True, "config": Config.masked(config.get()),
                        "defaults": DEFAULTS})
            return
        if path == "/api/logs":
            self._json({"ok": True, "items": self.server.logs.tail(
                int(q.get("limit") or 100))})
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
            if kind not in ("danmaku", "sc", "events") or fmt not in ("json", "csv"):
                self._fail(400, "type/format 不合法")
                return
            name, ctype, body = store.export(
                kind, fmt, q=q.get("q") or "", room_id=q.get("room_id"),
                since=q.get("since"))
            self._send(200, body, ctype, {
                "Content-Disposition": 'attachment; filename="%s"' % name})
            return
        self._fail(404, "没有这个接口")

    def _api_post(self, path, body):
        manager, config = self.server.manager, self.server.config
        if path == "/api/config":
            before = config.get()
            after = config.save(body)
            self.server.logs.add("info", "配置已保存")
            changed = any(before.get(k) != after.get(k) for k in
                          ("room_id", "sessdata", "dede_user_id", "buvid3",
                           "protover", "heartbeat_seconds", "stale_seconds"))
            self._json({"ok": True, "config": Config.masked(after),
                        "restart_required": changed and manager.snapshot()["running"]})
            return
        if path == "/api/start":
            if body.get("room_id"):
                config.save({"room_id": body["room_id"]})
            ok, message = manager.start()
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
            return
        if path == "/api/stop":
            ok, message = manager.stop()
            self._json({"ok": ok, "message": message})
            return
        if path == "/api/clear":
            if body.get("confirm") != "yes":
                self._fail(400, "需要 {\"confirm\":\"yes\"}")
                return
            self.server.store.clear()
            self.server.logs.add("warn", "数据已清空")
            self._json({"ok": True})
            return
        self._fail(404, "没有这个接口")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(host, port, data_dir, web_dir, store, manager, config, logs):
    """Build the server without blocking; used by run.py and selftest.py."""
    srv = AppServer((host, port), Handler)
    srv.store = store
    srv.manager = manager
    srv.config = config
    srv.logs = logs
    srv.web_dir = web_dir
    srv.data_dir = data_dir
    srv.host = host
    srv.port = port
    srv.started_at = int(time.time() * 1000)
    srv.lan_ip = _lan_ip()
    from capture import WS_IMPORT_ERROR
    srv.ws_ok = not bool(WS_IMPORT_ERROR)
    return srv


def serve(host, port, data_dir, web_dir, store, manager, config, logs,
          auto_start=False):
    srv = create_server(host, port, data_dir, web_dir, store, manager,
                        config, logs)
    ip = srv.lan_ip
    print("=" * 52, flush=True)
    print("  直播弹幕 / SC 采集服务已启动", flush=True)
    print("  本机：http://127.0.0.1:%s/" % port, flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("  局域网：http://%s:%s/" % (ip, port), flush=True)
    print("  数据：%s" % os.path.abspath(data_dir), flush=True)
    print("=" * 52, flush=True)
    if auto_start:
        ok, message = manager.start()
        logs.add("info" if ok else "error", "自动启动采集：%s" % message)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()
        srv.server_close()
