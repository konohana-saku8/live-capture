"""采集开关：按 up 主启停，以及一键全部停止。"""
from __future__ import annotations

from flask import Blueprint

from ..http import ApiError, body
from ..settings import as_int
from . import rt

bp = Blueprint("capture", __name__, url_prefix="/api")


def _payload(r, ok_flag: bool, message: str, status: int = 200):
    return {"ok": ok_flag, "message": message,
            "capture": r.manager.snapshot(),
            "rooms": r.rooms_payload()}, status


def _start():
    r = rt()
    rid = as_int(body().get("room_id")) or r.first_room_id()
    target = r.rooms.get(rid) or {}
    if target and target.get("collect_live") is False:
        raise ApiError("这个采集器关了「采集直播间」，先在采集器里打开", 409)
    ok_flag, message = r.manager.start(rid)
    return _payload(r, ok_flag, message, 200 if ok_flag else 409)


def _stop():
    r = rt()
    rid = as_int(body().get("room_id"))
    ok_flag, message = r.manager.stop(rid or None)
    return _payload(r, ok_flag, message)


# 新旧两套路径都留着：旧前端与外部脚本用的是 /api/start /api/stop
bp.add_url_rule("/capture/start", "capture_start", _start, methods=["POST"])
bp.add_url_rule("/start", "start", _start, methods=["POST"])
bp.add_url_rule("/capture/stop", "capture_stop", _stop, methods=["POST"])
bp.add_url_rule("/stop", "stop", _stop, methods=["POST"])


@bp.post("/capture/stop_all")
def stop_all():
    r = rt()
    ok_flag, message = r.manager.stop_all()
    return _payload(r, ok_flag, message)
