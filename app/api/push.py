"""开播 / 新动态推送：状态查询与自测。

模块名用 push 而不是 notify，避免和根目录的 notify.py（飞书发送实现）同名。
"""
from __future__ import annotations

from flask import Blueprint

import notify
from ..http import ApiError, body
from . import rt

bp = Blueprint("push", __name__, url_prefix="/api")


@bp.get("/notify")
def notify_status():
    return {"ok": True, "status": rt().notifier.status()}


@bp.post("/notify/test")
def notify_test():
    """发一条测试消息；body 里可临时带 webhook / secret 覆盖配置（不保存）。"""
    r = rt()
    data = body()
    cfg = r.config.get()
    webhook = str(data.get("webhook") or cfg.get("feishu_webhook") or "").strip()
    secret = str(data.get("secret") or cfg.get("feishu_secret") or "").strip()
    if not webhook:
        raise ApiError("还没有配置飞书 webhook")
    try:
        notify.send_text(webhook, "live-capture 推送测试：看到这条就说明配置正确",
                         secret)
    except notify.NotifyError as exc:
        return {"ok": False, "error": str(exc)}, 502
    r.logs.add("info", "推送测试成功")
    return {"ok": True, "status": r.notifier.status()}
