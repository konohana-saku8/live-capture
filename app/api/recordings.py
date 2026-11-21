# -*- coding: utf-8 -*-
"""录像列表、媒体文件与实时 FLV 代理。"""
from __future__ import annotations

import threading
import urllib.request

from flask import Blueprint, Response, send_file, stream_with_context

from capture import BiliApi
from ..http import ApiError, arg, int_arg, json_post
from ..settings import as_int
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
    # merged = 合并后的单文件（FLV 为主，旧 MP4 产物仍兼容）
    if kind == "merged":
        merged_path = rec.get("merged_path") or ""
        if not merged_path:
            raise ApiError("合并成品尚未就绪", 409)
        try:
            path = r.recordings.abs(merged_path)
        except ValueError:
            raise ApiError("录像路径不合法", 400)
        mime = "video/x-flv" if merged_path.lower().endswith(".flv") else "video/mp4"
        try:
            resp = send_file(path, conditional=True, mimetype=mime)
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


@bp.post("/recordings/<int:rec_id>/file/delete")
@json_post
def recording_file_delete(payload: dict, rec_id: int):
    """删单个视频文件：kind=mp4|flv，index 是录像里的分片序号（从 0 起）。"""
    r = rt()
    kind = str(payload.get("kind") or "").lower()
    if kind not in ("mp4", "flv"):
        raise ApiError("kind 必须是 mp4 或 flv", 400)
    try:
        result = r.recordings.delete_part(rec_id, kind, as_int(payload.get("index"), -1))
    except ValueError as exc:
        raise ApiError(str(exc), 409)
    except OSError as exc:
        raise ApiError("删除失败：" + str(exc), 500)
    r.invalidate_dashboard()
    return {"ok": True, "deleted": result}


@bp.post("/recordings/<int:rec_id>/delete")
def recording_delete(rec_id: int):
    """删整条录像（所有视频文件 + 录像记录），不碰弹幕 / SC / 事件。"""
    r = rt()
    try:
        result = r.recordings.delete_recording(rec_id)
    except ValueError as exc:
        raise ApiError(str(exc), 409)
    except OSError as exc:
        raise ApiError("删除失败：" + str(exc), 500)
    if result is None:
        raise ApiError("录像不存在", 404)
    r.invalidate_dashboard()
    return {"ok": True, "deleted": result}


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


@bp.post("/recordings/<int:rec_id>/reindex")
def recordings_reindex(rec_id):
    """给旧的单 FLV 合并产物补写拖动索引（幂等，已带索引则跳过）。"""
    r = rt()
    try:
        changed, detail = r.recordings.reindex_merged(rec_id)
    except ValueError as exc:
        raise ApiError(str(exc), 404)
    except Exception as exc:
        raise ApiError("索引写入失败：%s" % exc, 500)
    return {"ok": True, "id": rec_id, "changed": changed, "detail": detail}


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
