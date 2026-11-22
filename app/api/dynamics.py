"""UP 主动态：列表、立即拉取、翻译补录、删除、按 up 主的定时配置。"""
from __future__ import annotations

from flask import Blueprint

from ..http import ApiError, arg, body, int_arg
from ..settings import as_int
from . import rt

bp = Blueprint("dynamics", __name__, url_prefix="/api")


def _config_of(room: dict, mid: int) -> dict:
    r = rt()
    last_ts = as_int(room.get("dyn_last_ts")) or r.store.last_dyn_fetch(mid)
    return {"dyn_auto": bool(room.get("dyn_auto")),
            "dyn_hour": as_int(room.get("dyn_hour"), 8),
            "dyn_minute": as_int(room.get("dyn_minute")),
            "dyn_last_ts": last_ts,
            "collect_dynamics": room.get("collect_dynamics", True)}


@bp.get("/dynamics")
def list_dynamics():
    r = rt()
    limit = max(1, min(200, int_arg("limit", 10)))
    offset = max(0, int_arg("offset", 0))
    mid = int_arg("mid")
    total, items = r.store.list_dynamics(limit=limit, offset=offset,
                                         q=arg("q"), mid=mid or 0)
    # 定时抓取是这一位 up 主自己的设置，随列表一起回填给页面
    room = r.rooms.get(arg("room_id")) or {}
    return {"ok": True, "total": total, "items": items,
            "limit": limit, "offset": offset,
            "status": r.dynamics.status(),
            "config": _config_of(room, mid)}


@bp.post("/dynamics/fetch")
def fetch_dynamics():
    r = rt()
    data = body()
    mid = as_int(data.get("mid"))
    res = r.dynamics.pull("手动", limit=as_int(data.get("limit"), 20) or 20, mid=mid)
    rid = as_int(data.get("room_id"))
    if res.get("ok") and rid:
        r.rooms.settings(rid, {"dyn_last_ts": res["ts"]})
    total, items = r.store.list_dynamics(limit=200, mid=mid)
    return ({"ok": res["ok"], "message": res["msg"], "total": total,
             "items": items, "status": r.dynamics.status()},
            200 if res["ok"] else 502)


@bp.post("/dynamics/trans")
def translate_dynamic():
    r = rt()
    data = body()
    dyn_id = str(data.get("dyn_id") or "")
    if not dyn_id:
        raise ApiError("缺少 dyn_id")
    changed = r.store.update_dyn_translation(dyn_id, str(data.get("text") or ""))
    return {"ok": changed, "dyn_id": dyn_id}


@bp.post("/dynamics/delete")
def delete_dynamic():
    r = rt()
    data = body()
    dyn_id = str(data.get("dyn_id") or "")
    changed = r.store.delete_dynamic(dyn_id) if dyn_id else False
    total, items = r.store.list_dynamics(limit=200, mid=as_int(data.get("mid")))
    return {"ok": changed, "total": total, "items": items}


@bp.post("/dynamics/config")
def save_dynamics_config():
    r = rt()
    data = body()
    rid = as_int(data.get("room_id"))
    if rid <= 0:
        raise ApiError("缺少 room_id：定时抓取按 up 主分别设置")
    patch = {}
    if "collect_dynamics" in data:
        patch["collect_dynamics"] = bool(data["collect_dynamics"])
    if "dyn_auto" in data:
        patch["dyn_auto"] = bool(data["dyn_auto"])
    if "notify_dynamic" in data:
        patch["notify_dynamic"] = bool(data["notify_dynamic"])
    if as_int(data.get("dyn_hour"), -1) >= 0:
        patch["dyn_hour"] = as_int(data["dyn_hour"])
    if as_int(data.get("dyn_minute"), -1) >= 0:
        patch["dyn_minute"] = as_int(data["dyn_minute"])
    room = r.rooms.settings(rid, patch) if patch else r.rooms.get(rid)
    if not room:
        raise ApiError("没有这个采集器", 404)
    return {"ok": True, "room": room, "rooms": r.rooms_payload(),
            "config": _config_of(room, as_int(room.get("uid")))}
