"""公开接口：健康检查、进站锁屏探测、表情图代理。

这三个**不需要访问口令**：`/api/health` 给外部探活，`/api/gate` 用来判断
要不要弹密码框，`/api/emote` 是 `<img src>` 直接请求的（浏览器不会带
X-Access-Token），而且只回白名单 CDN 的公开图片。
"""
from __future__ import annotations

import time

from flask import Blueprint, Response

from images import FALLBACK_PNG

from ..http import arg
from . import rt

bp = Blueprint("public", __name__, url_prefix="/api")


@bp.get("/health")
def health():
    return {"ok": True, "ts": int(time.time() * 1000)}


@bp.get("/gate")
def gate():
    """进站锁屏用它判断「是否需要密码」，未设置时前端走首次设置流程。"""
    need = bool(str(rt().config.get().get("token") or ""))
    return {"ok": True, "need": need}


@bp.get("/emote")
def emote():
    """返回同源的 CDN 图片，绕开 Referer 防盗链。

    失败时回 502 + 透明 PNG（而不是 JSON），这样浏览器会触发 `error`
    事件，前端就能把坏图换成 alt 文字。
    """
    try:
        ctype, payload = rt().images.get(arg("u"))
    except Exception:
        return Response(FALLBACK_PNG, status=502, mimetype="image/png",
                        headers={"Cache-Control": "no-store",
                                 "X-Content-Type-Options": "nosniff"})
    return Response(payload, status=200, mimetype=ctype,
                    headers={"Cache-Control": "public, max-age=21600",
                             "X-Content-Type-Options": "nosniff"})
