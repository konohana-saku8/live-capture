"""采集器（up 主）增删改查与开关设置。"""
from __future__ import annotations

from flask import Blueprint

from capture import fetch_room_brief, fetch_room_by_uid
from rooms import NOTIFY_KEYS, PARAM_KEYS

from ..http import ApiError, body, int_in, ok
from ..settings import BOOL_KEYS, INT_BOUNDS, as_int
from . import rt

bp = Blueprint("rooms", __name__, url_prefix="/api")


@bp.get("/rooms")
def list_rooms():
    return rt().rooms_payload()


@bp.post("/rooms")
def add_room():
    r = rt()
    data = body()
    uid = as_int(data.get("uid"))
    rid = as_int(data.get("room_id"))
    label = str(data.get("label") or "")[:80]
    warning, brief = "", {}

    if uid > 0:
        try:
            brief = fetch_room_by_uid(uid, timeout=4)
            rid = as_int(brief.get("room_id")) or rid
        except Exception as exc:
            raise ApiError("按 uid 获取直播间失败：%s" % exc)
    if rid <= 0:
        raise ApiError("请填写 up 主 uid")
    if not brief:
        try:
            brief = fetch_room_brief(rid, timeout=3)
        except Exception as exc:
            brief, warning = {}, "直播间信息获取失败，先用房间号：%s" % exc

    try:
        item, created = r.rooms.add(rid, label, brief)
    except ValueError as exc:
        raise ApiError(str(exc))

    r.logs.add("info", "%s采集器：%s（uid %s · 房间 %s）" % (
        "新增" if created else "更新", item["label"],
        item.get("uid") or "—", rid))
    return {"ok": True, "room": item, "created": created,
            "warning": warning, "rooms": r.rooms_payload()}


@bp.post("/rooms/remove")
def remove_room():
    r = rt()
    rid = as_int(body().get("room_id"))
    if r.is_running(rid):
        r.manager.stop(rid)
    removed = r.rooms.remove(rid)
    r.logs.add("info", "删除采集器：%s（%s）" % ("成功" if removed else "不存在", rid))
    return {"ok": removed, "rooms": r.rooms_payload(),
            "capture": r.manager.snapshot()}


@bp.post("/rooms/settings")
def save_settings():
    r = rt()
    data = body()
    rid = as_int(data.get("room_id"))
    params = data.get("params")
    if not isinstance(params, dict):
        raise ApiError("params 必须是 JSON 对象")

    clean = {}
    for key, value in params.items():
        if key in ("collect_live", "collect_dynamics", "live_only"):
            clean[key] = bool(value)
            continue
        # 推送开关也按 up 主存，和定时抓取一样走这个接口
        if key in NOTIFY_KEYS:
            clean[key] = bool(value)
            continue
        # 定时抓取也按 up 主存，走同一个接口
        if key == "dyn_auto":
            clean[key] = bool(value)
            continue
        if key in ("dyn_hour", "dyn_minute"):
            clean[key] = int_in(value, key, 0, 23 if key == "dyn_hour" else 59)
            continue
        if key not in PARAM_KEYS:
            continue
        if key == "record_qn":
            qn = int_in(value, key, 80, 10000)
            if qn not in (80, 150, 250, 400, 10000):
                raise ApiError("record_qn 只支持 80/150/250/400/10000")
            clean[key] = qn
        elif key in BOOL_KEYS:
            clean[key] = bool(value)
        elif key in INT_BOUNDS:
            low, high = INT_BOUNDS[key]
            clean[key] = int_in(value, key, low, high)

    item = r.rooms.settings(rid, clean)
    if not item:
        raise ApiError("采集器不存在", 404)
    # 关掉「采集直播间」时顺手停掉正在跑的采集
    if item.get("collect_live") is False and r.is_running(rid):
        r.manager.stop(rid)
    r.logs.add("info", "保存采集器开关：%s（%s）" % (item["label"], rid))
    return {"ok": True, "room": item,
            "restart_required": bool(item.get("collect_live")),
            "rooms": r.rooms_payload(),
            "capture": r.manager.snapshot()}


@bp.post("/rooms/refresh")
def refresh_rooms():
    r = rt()
    updated = 0
    for room in (r.rooms.list().get("items") or [])[:20]:
        try:
            brief = fetch_room_brief(room["id"], timeout=3)
        except Exception:
            continue
        if r.rooms.refresh(room["id"], brief):
            updated += 1
    r.logs.add("info", "刷新直播间信息：%s 个" % updated)
    return {"ok": True, "updated": updated, "rooms": r.rooms_payload()}
