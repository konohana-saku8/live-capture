# -*- coding: utf-8 -*-
"""录像列表、媒体文件与实时 FLV 代理。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request

from flask import Blueprint, Response, request, send_file, stream_with_context

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
    """手动补转：默认只转还没有 MP4 的分片；body 传 force=true 则全部重转。"""
    r = rt()
    try:
        data = request.get_json(silent=True) or {}
        r.recordings.retry(rec_id, force=bool(data.get("force")))
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


# ---------- PC 端重编码回传 ----------
#
# 手机只负责收字节 + 复核元数据，编码在本机 PC 上做（见 pctools/）。
# 分块追加到「原分片.incoming」，commit 时用 ffprobe 验时长与编码，通过才
# os.replace 原子替换 —— 任何一步不过，磁盘上的原分片一个字节都不动。
# 时长漂移超过 150 ms 说明重编码丢帧或补帧了，弹幕轴会错位，必须拒绝。

_RECODE_CHUNK_MAX = 16 * 1024 * 1024
_RECODE_DRIFT_MS = 150


def _file_bytes(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _recode_part(rec, idx):
    """取出可替换的分片，返回 ``(part, target, incoming)``。"""
    parts = rec.get("mp4_parts") or []
    if idx < 0 or idx >= len(parts):
        raise ApiError("录像分片不存在", 404)
    status = (rec.get("status") or "").lower()
    if status != "ready":
        raise ApiError("录像当前是 %s 状态，等它 ready 再传" %
                       (status or "未知"), 409)
    part = parts[idx]
    try:
        target = rt().recordings.abs(part.get("path") or "")
    except ValueError:
        raise ApiError("录像路径不合法", 400)
    if _file_bytes(target) <= 0:
        raise ApiError("原分片文件不存在", 404)
    return part, target, target + ".incoming"


def _probe_video(path):
    """ffprobe 读时长/编码/分辨率；读不出返回 None（不抛异常）。"""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height,"
             "r_frame_rate", "-of", "json", path],
            capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    v = next((s for s in data.get("streams") or []
              if s.get("codec_type") == "video"), None)
    if not v:
        return None
    fps = 0.0
    raw = (v.get("r_frame_rate") or "").split("/")
    if len(raw) == 2:
        try:
            fps = round(float(raw[0]) / float(raw[1]), 3) if float(raw[1]) else 0.0
        except ValueError:
            fps = 0.0
    return {
        "duration_ms": int(float((data.get("format") or {}).get("duration") or 0) * 1000),
        "vcodec": v.get("codec_name") or "",
        "width": as_int(v.get("width")),
        "height": as_int(v.get("height")),
        "fps": fps,
        "bytes": _file_bytes(path),
    }


@bp.post("/recordings/<int:rec_id>/parts/<int:idx>/upload")
def recording_part_upload(rec_id: int, idx: int):
    """追加一段字节；``X-Chunk-Offset`` 必须与服务端已有大小一致。"""
    try:
        # Flask 3.1 支持请求级覆盖，避免为了这个接口放宽全局上限。
        request.max_content_length = _RECODE_CHUNK_MAX
    except AttributeError:
        pass
    rec = rt().recordings.get(rec_id)
    if not rec:
        raise ApiError("录像不存在", 404)
    _, _, incoming = _recode_part(rec, idx)

    try:
        offset = int(request.headers.get("X-Chunk-Offset") or -1)
    except (TypeError, ValueError):
        offset = -1
    if offset < 0:
        raise ApiError("缺少或非法的 X-Chunk-Offset", 400)

    length = request.content_length or 0
    if length <= 0:
        raise ApiError("请求体为空", 400)
    if length > _RECODE_CHUNK_MAX:
        raise ApiError("单次最多 %d MB" % (_RECODE_CHUNK_MAX // 1048576), 413)

    have = _file_bytes(incoming)
    if offset != have:
        # 客户端按返回的 offset 对账后续传，不当作错误。
        raise ApiError("服务端已有 %d 字节，与请求偏移 %d 不一致" %
                       (have, offset), 409)

    written = 0
    try:
        with open(incoming, "ab") as fh:
            while written < length:
                chunk = request.stream.read(min(1024 * 1024, length - written))
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise ApiError("写入失败：%s" % exc, 500)
    # 连接中途断掉时 written < length，把真实进度回给客户端续传。
    return {"ok": True, "offset": have + written, "short": written < length}


@bp.post("/recordings/<int:rec_id>/parts/<int:idx>/commit")
def recording_part_commit(rec_id: int, idx: int):
    """校验 incoming 后原子替换，并回写分片体积/时长。"""
    r = rt()
    rec = r.recordings.get(rec_id)
    if not rec:
        raise ApiError("录像不存在", 404)
    part, target, incoming = _recode_part(rec, idx)

    size = _file_bytes(incoming)
    if size <= 0:
        raise ApiError("没有待提交的数据", 409)
    expect = as_int(request.headers.get("X-Total-Size"))
    if expect > 0 and expect != size:
        raise ApiError("收到 %d 字节，与声明的 %d 不一致" % (size, expect), 409)

    info = _probe_video(incoming)
    if not info or info["duration_ms"] <= 0:
        raise ApiError("ffprobe 读不出这个文件（未装 ffprobe 或文件损坏）", 400)
    if info["vcodec"] != "h264":
        raise ApiError("视频编码是 %s，页面只认 h264" % (info["vcodec"] or "未知"), 400)
    want = as_int(part.get("duration_ms"))
    if want > 0 and abs(info["duration_ms"] - want) > _RECODE_DRIFT_MS:
        raise ApiError("新分片 %d ms 与原分片 %d ms 相差超过 %d ms，"
                       "弹幕轴会错位，已拒绝替换" %
                       (info["duration_ms"], want, _RECODE_DRIFT_MS), 400)

    try:
        os.replace(incoming, target)
    except OSError as exc:
        raise ApiError("替换失败：%s" % exc, 500)

    parts = [dict(p) for p in (rec.get("mp4_parts") or [])]
    before = as_int(parts[idx].get("bytes"))
    parts[idx].update({"bytes": size, "duration_ms": info["duration_ms"],
                       "recompressed_at": int(time.time() * 1000)})
    total_bytes = sum(as_int(p.get("bytes")) for p in parts)
    total_ms = sum(as_int(p.get("duration_ms")) for p in parts)
    r.recordings.store.update_recording(
        rec_id, mp4_parts=parts, bytes=total_bytes, duration_ms=total_ms)
    r.invalidate_dashboard()
    r.logs.add("info", "[录像 %s] 第 %d 片已替换：%.1f MB → %.1f MB（%s）" %
               (rec_id, idx + 1, before / 1048576.0, size / 1048576.0,
                info["vcodec"]))
    return {"ok": True, "part": parts[idx], "saved_bytes": before - size,
            "bytes": total_bytes, "duration_ms": total_ms}


@bp.post("/recordings/<int:rec_id>/parts/<int:idx>/abort")
def recording_part_abort(rec_id: int, idx: int):
    """丢掉未完成的 incoming，原分片不动。"""
    rec = rt().recordings.get(rec_id)
    if not rec:
        raise ApiError("录像不存在", 404)
    _, _, incoming = _recode_part(rec, idx)
    try:
        if os.path.isfile(incoming):
            os.remove(incoming)
    except OSError as exc:
        raise ApiError("清理失败：%s" % exc, 500)
    return {"ok": True}


@bp.get("/recordings/<int:rec_id>/parts/<int:idx>/incoming")
def recording_part_incoming(rec_id: int, idx: int):
    """已收到多少字节，供客户端断点续传对账。"""
    rec = rt().recordings.get(rec_id)
    if not rec:
        raise ApiError("录像不存在", 404)
    part, target, incoming = _recode_part(rec, idx)
    return {"ok": True, "offset": _file_bytes(incoming),
            "target_bytes": _file_bytes(target),
            "duration_ms": as_int(part.get("duration_ms"))}
