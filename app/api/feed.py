"""弹幕 / SC / 事件流与场次切分、导出。

三种取法并存，是为了兼容不同页面的读法：
- 默认：取最新 N 条（limit ≤ 300）
- ``paged=1``：服务端游标分页，从最新往更早翻（``before`` + ``incl``）
- ``all=1``：整段拉取（旧接口保留，页面已不用）
"""
from __future__ import annotations

from flask import Blueprint, Response

from ..http import ApiError, arg, bool_arg, int_arg
from ..settings import VALID_KINDS
from . import rt

bp = Blueprint("feed", __name__, url_prefix="/api")


@bp.get("/feed")
def feed():
    r = rt()
    rid = int_arg("room_id") or r.first_room_id()
    kinds = [k for k in (arg("types") or "danmaku,sc,events").split(",")
             if k in VALID_KINDS]
    since = arg("since") or None
    until = arg("until") or None
    query = arg("q")
    all_mode = bool_arg("all")
    paged = bool_arg("paged")

    items, counts, more, oldest = {}, {}, {}, {}
    for kind in kinds:
        if paged:
            # 数据量大了（十几万条弹幕）不能整段塞给浏览器
            limit = max(1, min(1000, int_arg("limit", 600)))
            before = int_arg("before")
            rows = r.store.query_page_before(
                kind, before or 0, count=limit + 1,
                room_id=rid or None, since=since, until=until,
                inclusive=bool_arg("incl"), q=query)
            more[kind] = len(rows) > limit
            if more[kind]:
                rows = rows[1:]           # 多取的那条只用来判断「还有更早的」
            items[kind] = rows
            counts[kind] = r.store.count_range(
                kind, q=query, room_id=rid or None, since=since, until=until)
            oldest[kind] = rows[0]["received_at"] if rows else 0
        elif all_mode:
            rows = r.store.query_all(kind, q=query, room_id=rid or None,
                                     since=since, until=until)
            items[kind] = rows
            counts[kind] = len(rows)
        else:
            limit = max(1, min(300, int_arg("limit", 40)))
            total, rows = r.store.query(kind, limit=limit, q=query,
                                        room_id=rid or None,
                                        since=since, until=until)
            items[kind] = rows
            counts[kind] = total

    return {"ok": True, "room_id": rid, "items": items, "counts": counts,
            "all": all_mode, "paged": paged, "more": more, "oldest": oldest}


@bp.get("/sessions")
def sessions():
    r = rt()
    rid = int_arg("room_id") or r.first_room_id()
    segs = r.store.live_segments(rid)
    return {"ok": True, "room_id": rid, "segments": segs,
            "sessions": segs, "total": len(segs)}


def _list_kind(kind: str):
    """按类型直接翻页（limit/offset），给导出前的预览用。"""
    r = rt()
    total, rows = r.store.query(
        kind,
        limit=int_arg("limit", 100) or 100,
        offset=int_arg("offset", 0),
        q=arg("q"),
        room_id=arg("room_id") or None,
        since=arg("since") or None,
        run_id=arg("run_id") or None)
    return {"ok": True, "total": total, "items": rows}


@bp.get("/danmaku")
def list_danmaku():
    return _list_kind("danmaku")


@bp.get("/superchats")
def list_superchats():
    return _list_kind("sc")


@bp.get("/events")
def list_events():
    return _list_kind("events")


@bp.get("/export")
def export():
    r = rt()
    kind = arg("type") or "danmaku"
    fmt = arg("format") or "json"
    if kind not in VALID_KINDS or fmt not in ("json", "csv"):
        raise ApiError("type/format 不合法")
    name, ctype, payload = r.store.export(
        kind, fmt, q=arg("q"), room_id=arg("room_id") or None,
        since=arg("since") or None)
    return Response(payload, status=200, mimetype=ctype,
                    headers={"Content-Disposition": 'attachment; filename="%s"' % name})
