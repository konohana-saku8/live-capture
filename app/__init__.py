"""Flask 应用工厂。

设计取舍：
- 业务对象（store / manager / rooms ...）只在 `Runtime` 里构造一次，挂在
  `app.extensions["live"]`；Blueprint 通过 `api.rt()` 取，不碰全局变量。
- 前端是 Vite 构建出来的纯静态 SPA，所以 `static_folder=None`，静态文件与
  history 模式 fallback 由下面的两条规则显式处理，避免和 `/api/*` 抢路由。
- API 契约（字段名、状态码、鉴权方式）与原 `server.py` 保持一致，这样后端
  可以先换、前端再换，中间不会出现半坏状态。
"""
from __future__ import annotations

import os
from typing import Any

from flask import Flask, request, send_from_directory

from .api import register as register_api
from .api import rt
from .http import ApiError, log_requests, register_errors
from .settings import Config

# 不需要访问口令的接口：探活、锁屏探测、表情图（<img> 带不了 header）
PUBLIC_API = frozenset({"/api/health", "/api/gate", "/api/emote"})


def _build_runtime(data_dir: str, web_dir: str, host: str, port: int):
    from .runtime import Runtime
    return Runtime(data_dir, web_dir, host=host, port=port)


def create_app(data_dir: str | None = None, web_dir: str | None = None,
               host: str = "0.0.0.0", port: int = 8765,
               runtime: Any = None) -> Flask:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = data_dir or os.path.join(here, "data")
    web_dir = web_dir or os.path.join(here, "web", "dist")

    r = runtime or _build_runtime(data_dir, web_dir, host, port)

    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024
    app.extensions["live"] = r

    register_errors(app)
    register_api(app)
    # 放在鉴权钩子之前注册：401 也要有耗时基准
    log_requests(app)

    # ---------- 鉴权 ----------

    @app.before_request
    def _require_token():
        if request.method == "OPTIONS":
            return None
        if not request.path.startswith("/api/"):
            return None
        if request.path in PUBLIC_API:
            return None
        token = str(rt().config.get().get("token") or "")
        if not token:
            return None
        supplied = (request.headers.get("X-Access-Token") or "").strip()
        if not supplied:
            supplied = (request.args.get("token") or "").strip()
        if supplied != token:
            raise ApiError("需要访问口令", 401)
        return None

    # ---------- 响应头 ----------

    @app.after_request
    def _headers(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Access-Token"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        if request.path.startswith("/api/") and not resp.headers.get("Cache-Control"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------- 静态页面 ----------

    def _index():
        index = os.path.join(rt().web_dir, "index.html")
        if not os.path.isfile(index):
            return {
                "ok": False,
                "error": "前端还没构建：在 host 上执行 npm run build 后同步 dist/",
            }, 503
        return send_from_directory(rt().web_dir, "index.html")

    app.add_url_rule("/", "index", _index)
    app.add_url_rule("/index.html", "index_html", _index)

    @app.get("/<path:filename>")
    def static_or_spa(filename: str):
        """有同名文件就发文件，否则交给前端路由（history 模式）。"""
        if filename.startswith("api/"):
            raise ApiError("没有这个接口", 404)
        base = rt().web_dir
        target = os.path.normpath(os.path.join(base, filename))
        if target.startswith(base) and os.path.isfile(target):
            resp = send_from_directory(base, filename)
            # 带 hash 的静态资源可以长缓存，index.html 不行
            if "/assets/" in request.path:
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp
        return _index()

    return app


def bootstrap(app: Flask, auto_start: bool = False) -> None:
    """启动常驻线程（动态定时器 / 开播推送）并按配置拉起采集。"""
    r = app.extensions["live"]
    r.recordings.reconcile()
    if auto_start:
        r.auto_start_rooms()
    r.dynamics.start()
    r.notifier.start()


def shutdown(app: Flask) -> None:
    app.extensions["live"].shutdown()
