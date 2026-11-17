"""弹幕 / SC / 事件流与场次切分、导出。

三种取法并存，是为了兼容不同页面的读法：
- 默认：取最新 N 条（limit ≤ 300）
- ``paged=1``：服务端游标分页，从最新往更早翻（``before`` + ``incl``）
- ``all=1``：整段一次拉取；脚本 / 预览用（单场上限 10 万条）。
  数据页用 ``paged=1`` + ``before``/``after`` 按页加载，只追加不替换。
"""
from __future__ import annotations

import json
import queue

from flask import Blueprint, Response, stream_with_context

from ..http import ApiError, arg, bool_arg, int_arg, json_post
from ..settings import VALID_KINDS, as_int
from . import rt

bp = Blueprint("feed", __name__, url_prefix="/api")

# 页面只渲染这些字段；raw_json 往往有几 KB/条，整段拉取时是最大的流量来源。
FEED_FIELDS = {
    "danmaku": ("seq", "received_at", "event_time", "uid", "uname", "text",
                "medal_name", "medal_level", "guard_level", "is_admin",
                "is_emoticon", "emote_url", "emote_px", "emote_map"),
    "sc": ("sc_id", "received_at", "start_time", "uid", "uname", "price",
           "message", "message_trans", "guard_level", "medal_name",
           "medal_level"),
    "events": ("seq", "received_at", "event_time", "kind", "uid", "uname",
               "detail"),
}


def _slim(kind, rows):
    cols = FEED_FIELDS.get(kind)
    if not cols:
        return rows
    return [{key: r.get(key) for key in cols} for r in rows]


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
    asc = bool_arg("asc") or arg("order") == "asc"

    items, counts, more, oldest = {}, {}, {}, {}
    for kind in kinds:
        if paged and asc:
            limit = max(1, min(1000, int_arg("limit", 600)))
            after = int_arg("after")
            rows = r.store.query_page_after(
                kind, after or 0, count=limit + 1,
                room_id=rid or None, since=since, until=until,
                inclusive=bool_arg("incl"), q=query)
            more[kind] = len(rows) > limit
            if more[kind]:
                rows = rows[:-1]          # 多取的那条只用来判断「后面还有」
            items[kind] = _slim(kind, rows)
            counts[kind] = r.store.count_range(
                kind, q=query, room_id=rid or None, since=since, until=until)
            oldest[kind] = rows[-1]["received_at"] if rows else 0
        elif paged:
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
            items[kind] = _slim(kind, rows)
            counts[kind] = r.store.count_range(
                kind, q=query, room_id=rid or None, since=since, until=until)
            oldest[kind] = rows[0]["received_at"] if rows else 0
        elif all_mode:
            rows = r.store.query_all(kind, q=query, room_id=rid or None,
                                     since=since, until=until)
            items[kind] = _slim(kind, rows)
            counts[kind] = len(rows)
        else:
            limit = max(1, min(300, int_arg("limit", 40)))
            total, rows = r.store.query(kind, limit=limit, q=query,
                                        room_id=rid or None,
                                        since=since, until=until)
            items[kind] = _slim(kind, rows)
            counts[kind] = total

    return {"ok": True, "room_id": rid, "items": items, "counts": counts,
            "all": all_mode, "paged": paged, "asc": asc, "more": more,
            "oldest": oldest}


@bp.get("/stream")
def stream():
    """SSE 增量推送：采集线程入库后立刻把这一批数据写到页面。

    同一域名同端口，沿用 `?token=` 鉴权；`EventSource` 断线会自动重连。
    每条连接占一个 waitress 工作线程（当前 16 个），个人使用 1-2 个页面没问题。
    """
    r = rt()
    rid = int_arg("room_id")
    kinds = [k for k in (arg("types") or "danmaku,sc,events").split(",")
             if k in VALID_KINDS]

    def gen():
        q = r.bus.subscribe()
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    message = q.get(timeout=20)
                except queue.Empty:
                    # run.py 的 channel_timeout=120 会掐断长时间无数据的长连接
                    yield ": ping\n\n"
                    continue
                kind = message.get("kind")
                if kind not in kinds:
                    continue
                rows = message.get("rows") or []
                if rid:
                    rows = [x for x in rows if as_int(x.get("room_id")) == rid]
                if not rows:
                    continue
                yield "event: %s\ndata: %s\n\n" % (
                    kind, json.dumps({"rows": rows}, ensure_ascii=False,
                                     separators=(",", ":")))
        finally:
            r.bus.unsubscribe(q)

    resp = Response(stream_with_context(gen()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@bp.get("/sessions")
def sessions():
    r = rt()
    rid = int_arg("room_id") or r.first_room_id()
    segs = r.store.live_segments(rid)
    if bool_arg("with_recordings"):
        segs = r.recordings.attach_segments(rid, segs)
    return {"ok": True, "room_id": rid, "segments": segs,
            "sessions": segs, "total": len(segs)}


@bp.post("/session/delete")
@json_post
def session_delete(payload: dict):
    """删掉一个场次 / 场间：时间段内的弹幕、SC、事件一起删，不可恢复。"""
    r = rt()
    rid = as_int(payload.get("room_id"))
    start = as_int(payload.get("start"))
    end = as_int(payload.get("end"))
    kind = str(payload.get("kind") or "")
    if not rid or start <= 0:
        raise ApiError("缺少 room_id / start", 400)
    if kind not in ("live", "gap"):
        raise ApiError("kind 必须是 live 或 gap", 400)
    deleted = r.store.delete_session(rid, start, end,
                                     with_marks=(kind == "live"))
    return {"ok": True, "room_id": rid, "deleted": deleted}


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
        since=arg("since") or None, until=arg("until") or None)
    return Response(payload, status=200, mimetype=ctype,
                    headers={"Content-Disposition": 'attachment; filename="%s"' % name})
