"""HTTP 层公共件：异常、返回格式、校验助手。

所有接口都返回 `{"ok": bool, ...}`，失败时带 `error`，这样前端的
`if (!j.ok) toast(j.error)` 一套逻辑能吃下全部响应。
"""
from __future__ import annotations

import functools
import json
import time
from typing import Any, Callable

from flask import current_app, g, jsonify, request
from werkzeug.exceptions import HTTPException

from .settings import as_bool, as_int

MAX_BODY = 512 * 1024

# 超过这个耗时的接口调用值得看一眼（手机上一秒级已经是全表扫描了）
SLOW_MS = 2000


def runtime():
    """当前请求的 Runtime；拿不到就返回 None（错误处理里不能二次抛错）。"""
    try:
        return current_app.extensions.get("live")
    except Exception:
        return None


class ApiError(Exception):
    """业务错误：直接映射成 `{"ok": false, "error": msg}`。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def ok(payload: dict[str, Any] | None = None, status: int = 200):
    body = {"ok": True}
    if payload:
        body.update(payload)
    return jsonify(body), status


def fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def body() -> dict[str, Any]:
    """取 JSON 请求体，拒绝非对象与超大体积。

    直接解析原始字节而不是 `request.get_json()`：后者在 content-type 不是
    application/json 时会返回 415，而页面里有些调用没带这个头。
    """
    if request.content_length and request.content_length > MAX_BODY:
        raise ApiError("请求体过大", 413)
    raw = request.get_data(cache=True)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError("请求体不是合法 JSON")
    if not isinstance(parsed, dict):
        raise ApiError("请求体必须是 JSON 对象")
    return parsed


def arg(name: str, default: str = "") -> str:
    return (request.args.get(name) or default).strip()


def int_arg(name: str, default: int = 0) -> int:
    return as_int(request.args.get(name), default)


def bool_arg(name: str, default: bool = False) -> bool:
    return as_bool(request.args.get(name), default)


def page_window(default_limit: int, hard_max: int) -> tuple[int, int]:
    """统一的 limit/offset 解析，防止前端传个 1e9 把内存打爆。"""
    limit = max(1, min(hard_max, int_arg("limit", default_limit) or default_limit))
    offset = max(0, int_arg("offset", 0))
    return limit, offset


def int_in(value: Any, name: str, low: int, high: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise ApiError("%s 必须是数字" % name)
    if not low <= num <= high:
        raise ApiError("%s 需要在 %s–%s 之间" % (name, low, high))
    return num


def register_errors(app) -> None:
    """把异常统一成 JSON，别让 Flask 吐 HTML 错误页给前端。"""

    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError):
        return fail(exc.message, exc.status)

    @app.errorhandler(ValueError)
    def _value_error(exc: ValueError):
        # Config.save / rooms.settings 这类校验失败抛的都是 ValueError，
        # 语义上是「参数不对」，不能算服务端故障。
        return fail(str(exc) or "参数不合法", 400)

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        if request.path.startswith("/api/"):
            return fail(exc.description or exc.name, exc.code or 500)
        return exc

    @app.errorhandler(Exception)
    def _crash(exc: Exception):
        # 500 是真正需要排查的东西：记完整 traceback，页面上点开就能看到
        # 是哪个文件的哪一行炸的，不用再把 service.log 拉下来翻。
        g.__dict__["_logged_error"] = True
        r = runtime()
        if r is not None:
            r.logs.add("error", "接口 500：%s %s → %s" %
                       (request.method, request.path,
                        exc.__class__.__name__ + ": " + str(exc)), exc=exc)
        else:
            app.logger.exception("unhandled error on %s", request.path)
        return fail(str(exc) or exc.__class__.__name__, 500)


def log_requests(app) -> None:
    """把 API 的 4xx / 5xx / 慢请求写进运行日志。

    写代码的人需要知道“谁在报错”，而不是只有“登录成功”。静态资源不计，
    否则 404 的 favicon 会把真正的错误淹掉。
    """

    @app.before_request
    def _mark_start():
        g.__dict__["_t0"] = time.monotonic()

    @app.after_request
    def _log_outcome(resp):
        if not request.path.startswith("/api/"):
            return resp
        r = runtime()
        if r is None:
            return resp
        start = g.__dict__.get("_t0")
        ms = int((time.monotonic() - start) * 1000) if start else 0
        code = resp.status_code
        line = "%s %s → %s · %d ms" % (request.method, request.path, code, ms)
        if code >= 500:
            # _crash 已经记过带 traceback 的那条，这里不重复
            if not g.__dict__.get("_logged_error"):
                r.logs.add("error", "接口 5xx：" + line)
        elif code >= 400:
            detail = ""
            if resp.is_json:
                payload = resp.get_json(silent=True)
                if isinstance(payload, dict):
                    detail = str(payload.get("error") or "")[:200]
            r.logs.add("warn", "接口 %s：%s%s" %
                       (code, line, " · " + detail if detail else ""))
        elif ms >= SLOW_MS:
            r.logs.add("warn", "慢请求：" + line)
        return resp


def json_post(fn: Callable):
    """把 `body()` 解析提前失败变成 400，省得每个 POST 都写一层 try。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(body(), *args, **kwargs)

    return wrapper
