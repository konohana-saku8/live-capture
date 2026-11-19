# -*- coding: utf-8 -*-
"""录像列表、媒体文件与实时 FLV 代理。"""
from __future__ import annotations

import threading
import urllib.request

from flask import Blueprint, Response, send_file, stream_with_context

from capture import BiliApi
from ..http import ApiError, arg, int_arg
from . import rt

bp = Blueprint("recordings", __name__, url_prefix="/api")

_live_lock = threading.Lock()
_live_clients = {}
_LIVE_MAX = 2
_QNS = (80, 150, 250, 400, 10000)


@bp.get("/recordings")
def recordings():
    r = rt()
    rid = int_arg("room_id") or r.first_room_id()
    since = arg("from") or None
    until = arg("to") or None
    state = arg("state") or None
    items = r.recordings.list_room(rid or None, since, until, state)
    return {"ok": True, "room_id": rid, "items": items,
            "summary": r.recordings.summary(rid or None)}


@bp.get("/recordings/summary")
def recordings_summary():
    r = rt()
    rid = int_arg("room_id") or None
    return {"ok": True, "room_id": rid,
            "summary": r.recordings.summary(rid)}


@bp.get("/recordings/<int:rec_id>/media")
def recordings_media(rec_id):
    r = rt()
    rec = r.recordings.get(rec_id)
    if not rec:
        raise ApiError("录像不存在", 404)
    kind = (arg("kind") or "").lower()
    mp4 = rec.get("mp4_parts") or []
    flv = rec.get("flv_parts") or []
    # merged = 合并后的单 MP4（前端优先播放这一路）
    if kind == "merged":
        if not (rec.get("merged_path") or ""):
            raise ApiError("合并成品尚未就绪", 409)
        try:
            path = r.recordings.abs(rec["merged_path"])
        except ValueError:
            raise ApiError("录像路径不合法", 400)
        try:
            resp = send_file(path, conditional=True, mimetype="video/mp4")
        except OSError:
            raise ApiError("合并成品不存在", 404)
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    if kind not in ("mp4", "flv"):
        kind = "mp4" if mp4 else "flv"
    parts = mp4 if kind == "mp4" else flv
    part = max(0, int_arg("part", 0))
    if part >= len(parts):
        raise ApiError("录像分片不存在", 404)
    try:
        path = r.recordings.abs(parts[part].get("path"))
    except ValueError:
        raise ApiError("录像路径不合法", 400)
    try:
        resp = send_file(path, conditional=True,
                         mimetype="video/mp4" if kind == "mp4" else "video/x-flv")
    except OSError:
        raise ApiError("录像文件不存在", 404)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@bp.post("/recordings/<int:rec_id>/remux")
def recordings_remux(rec_id):
    r = rt()
    try:
        r.recordings.retry(rec_id)
    except ValueError as exc:
        raise ApiError(str(exc), 404)
    return {"ok": True, "id": rec_id}


@bp.post("/recordings/<int:rec_id>/merge")
def recordings_merge(rec_id):
    """手动重试合并成单文件（失败兜底；正常情况 remux 后自动触发）。"""
    r = rt()
    try:
        r.recordings.retry_merge(rec_id)
    except ValueError as exc:
        raise ApiError(str(exc), 404)
    return {"ok": True, "id": rec_id}


@bp.get("/live/flv")
def live_flv():
    """把 B 站 HTTP-FLV 透传给 mpegts.js；同源，避免浏览器 CORS。"""
    r = rt()
    rid = int_arg("room_id") or r.first_room_id()
    if not rid:
        raise ApiError("缺少 room_id", 400)
    qn = int_arg("qn", 250)
    if qn not in _QNS:
        qn = 250
    with _live_lock:
        if _live_clients.get(rid, 0) >= _LIVE_MAX:
            raise ApiError("该直播间实时画面连接数已满", 429)
        _live_clients[rid] = _live_clients.get(rid, 0) + 1

    def gen():
        try:
            cfg = r.manager._cookie_cfg(dict(r.config.get()))
            api = BiliApi(cfg)
            urls, _real_qn = api.get_play_urls(rid, qn=qn)
            headers = {"User-Agent": api.user_agent,
                       "Referer": "https://live.bilibili.com/",
                       "Origin": "https://live.bilibili.com"}
            cookie = api.cookie_header()
            if cookie:
                headers["Cookie"] = cookie
            last_error = None
            for url in urls:
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        while True:
                            chunk = resp.read(64 * 1024)
                            if not chunk:
                                break
                            yield chunk
                    return
                except GeneratorExit:
                    raise
                except Exception as exc:
                    last_error = exc
            if last_error:
                raise last_error
        finally:
            with _live_lock:
                left = _live_clients.get(rid, 1) - 1
                if left > 0:
                    _live_clients[rid] = left
                else:
                    _live_clients.pop(rid, None)

    resp = Response(stream_with_context(gen()), mimetype="video/x-flv")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp
