# -*- coding: utf-8 -*-
"""录像文件与数据库记录：开播录制、remux、按场次挂到日历。

文件统一放在 ``data/recordings/<room_id>/``，数据库只存相对路径，便于整个
data 目录搬走。录制中只写 FLV；停止后由 remux 线程把每个分片各自无损转成
MP4 并删 FLV。绝不跨分片 concat：直播重连后新分片的 SPS/PPS、AAC 声道配置、
帧率、时间戳基准都可能变，-c copy 拼接会让 MP4 只保留第一片的参数集去解后续
分片，导致后半段画面和声音全部损坏（2026-11-19 场次踩过）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from remux import RemuxWorker

MIN_FREE_BYTES = 1024 * 1024 * 1024      # 录制硬下限 1GB
WARN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 页面预警 2GB
MATCH_WINDOW_MS = 10 * 60 * 1000


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _probe_duration(path):
    """ffprobe 拿视频时长；没有 ffprobe 就返回 0，不阻塞 remux。"""
    return _probe(path)[0]


def _probe(path):
    """一次 ffprobe 同时取时长和关键编码参数。

    返回 ``(duration_ms, desc)``。desc 形如
    ``h264 640x360 30fps / aac 44100Hz 2ch``，供日志定位重连后的参数变化
    （11-19 事故中分片参数不一致却无任何日志，只能事后考古）。
    ffprobe 不可用或探测失败时返回 (0, "")，绝不阻塞录制/remux。
    """
    exe = shutil.which("ffprobe")
    if not exe:
        return 0, ""
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height,"
             "r_frame_rate,sample_rate,channels",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
        dur = int(float((data.get("format") or {}).get("duration") or 0) * 1000)
        v = a = None
        for s in data.get("streams") or []:
            if s.get("codec_type") == "video" and v is None:
                v = s
            elif s.get("codec_type") == "audio" and a is None:
                a = s
        parts = []
        if v:
            fps = v.get("r_frame_rate") or "?"
            if fps.endswith("/1"):
                fps = fps[:-2]
            parts.append("%s %sx%s %sfps" % (
                v.get("codec_name") or "?", v.get("width") or "?",
                v.get("height") or "?", fps))
        if a:
            parts.append("%s %sHz %sch" % (
                a.get("codec_name") or "?", a.get("sample_rate") or "?",
                a.get("channels") or "?"))
        return dur, " / ".join(parts)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0, ""


class RecordingService:
    def __init__(self, store, data_dir, log=None):
        self.store = store
        self.data_dir = os.path.abspath(data_dir)
        self.log = log or (lambda *_: None)
        self.remux = RemuxWorker(self.run_remux, self.log)

    # ---------- 路径 ----------

    def rel(self, path):
        return os.path.relpath(os.path.abspath(path), self.data_dir).replace("\\", "/")

    def abs(self, rel):
        path = os.path.abspath(os.path.join(self.data_dir, rel or ""))
        root = self.data_dir.rstrip(os.sep) + os.sep
        if not path.startswith(root):
            raise ValueError("录像路径越界")
        return path

    def room_dir(self, room_id):
        return os.path.join(self.data_dir, "recordings", str(_as_int(room_id)))

    # ---------- 录制 ----------

    def create(self, room_id, label, live_started_at, requested_qn, started_at=None):
        return self.store.create_recording(
            room_id, label, live_started_at, requested_qn,
            started_at=started_at or int(time.time() * 1000))

    def set_actual_qn(self, rec_id, qn):
        self.store.update_recording(rec_id, actual_qn=_as_int(qn))

    def add_part(self, rec_id, path, started_at):
        rec = self.store.get_recording(rec_id)
        if not rec:
            return
        parts = rec.get("flv_parts") or []
        rel = self.rel(path)
        if any(x.get("path") == rel for x in parts):
            return
        parts.append({"path": rel, "started_at": _as_int(started_at),
                      "bytes": 0, "duration_ms": 0})
        self.store.update_recording(rec_id, flv_parts=parts)

    def finish_part(self, rec_id, path, ended_at=None):
        rec = self.store.get_recording(rec_id)
        if not rec:
            return
        rel = self.rel(path)
        parts = rec.get("flv_parts") or []
        for part in parts:
            if part.get("path") == rel:
                full = self.abs(rel)
                part["bytes"] = _file_size(full)
                dur, desc = _probe(full)
                part["duration_ms"] = dur
                # 分片落盘即记录编码参数:重连后若参数变化,日志里逐片可见,
                # 不必等出故障再 ffprobe 坏文件反推(11-19 的痛点)。
                self.log("info", "[录像 %s] 分片参数 %s：%s，%.1f MB，%.1f 秒" %
                         (rec_id, os.path.basename(rel), desc or "未取到",
                          part["bytes"] / 1024.0 / 1024.0, dur / 1000.0))
                break
        self.store.update_recording(rec_id, flv_parts=parts)

    def finish(self, rec_id, stopped_at=None, status="pending"):
        rec = self.store.get_recording(rec_id)
        if not rec:
            return
        parts = rec.get("flv_parts") or []
        if not parts:
            self.store.update_recording(
                rec_id, stopped_at=stopped_at or int(time.time() * 1000),
                status="failed", error="没有写入任何录像分片")
            return
        self.store.update_recording(
            rec_id, stopped_at=stopped_at or int(time.time() * 1000),
            status=status, bytes=sum(_as_int(p.get("bytes")) for p in parts))
        self.remux.enqueue(rec_id)

    def retry(self, rec_id):
        rec = self.store.get_recording(rec_id)
        if not rec:
            raise ValueError("录像不存在")
        if not rec.get("flv_parts"):
            raise ValueError("没有 FLV 分片可 remux")
        self.store.update_recording(rec_id, status="pending", error="")
        self.remux.enqueue(rec_id)

    # ---------- 查询 ----------

    @staticmethod
    def public(rec):
        if not rec:
            return None
        mp4 = rec.get("mp4_parts") or []
        flv = rec.get("flv_parts") or []
        return {
            "id": rec.get("id"),
            "room_id": rec.get("room_id"),
            "label": rec.get("label") or "",
            "live_started_at": rec.get("live_started_at") or 0,
            "started_at": rec.get("started_at") or 0,
            "stopped_at": rec.get("stopped_at") or 0,
            "status": rec.get("status") or "",
            "requested_qn": rec.get("requested_qn") or 0,
            "actual_qn": rec.get("actual_qn") or 0,
            "bytes": rec.get("bytes") or 0,
            "duration_ms": rec.get("duration_ms") or 0,
            "error": rec.get("error") or "",
            "flv_count": len(flv),
            "mp4_count": len(mp4),
            "has_video": bool(mp4 or flv),
            "mp4_parts": [
                {"index": i, "started_at": p.get("started_at") or 0,
                 "bytes": p.get("bytes") or 0,
                 "duration_ms": p.get("duration_ms") or 0}
                for i, p in enumerate(mp4)
            ],
            "flv_parts": [
                {"index": i, "started_at": p.get("started_at") or 0,
                 "bytes": p.get("bytes") or 0,
                 "duration_ms": p.get("duration_ms") or 0}
                for i, p in enumerate(flv)
            ],
        }

    def list_room(self, room_id=None, since=None, until=None, status=None):
        return [self.public(x) for x in self.store.list_recordings(
            room_id, since, until, status)]

    def get(self, rec_id):
        return self.store.get_recording(rec_id)

    def summary(self, room_id=None):
        recs = self.store.list_recordings(room_id)
        try:
            usage = shutil.disk_usage(self.data_dir)
            free = usage.free
        except OSError:
            free = 0
        by_status = {}
        for rec in recs:
            key = rec.get("status") or "unknown"
            by_status[key] = by_status.get(key, 0) + 1
        return {"count": len(recs),
                "total_bytes": sum(_as_int(r.get("bytes")) for r in recs),
                "free_bytes": free,
                "warn": bool(free and free < WARN_FREE_BYTES),
                "by_status": by_status}

    def attach_segments(self, room_id, segments):
        recs = self.store.list_recordings(room_id)
        for seg in segments:
            if seg.get("kind") != "live":
                seg["recording"] = None
                continue
            seg["recording"] = self.public(self._match(seg, recs))
        return segments

    @staticmethod
    def _match(seg, recs):
        start = _as_int(seg.get("start"))
        end = _as_int(seg.get("end")) or int(time.time() * 1000)
        best, best_gap = None, MATCH_WINDOW_MS + 1
        for rec in recs:
            points = [x for x in (rec.get("live_started_at"),
                                  rec.get("started_at")) if _as_int(x)]
            for point in points:
                gap = abs(_as_int(point) - start)
                if gap < best_gap:
                    best, best_gap = rec, gap
            rec_start = _as_int(rec.get("started_at"))
            if start - MATCH_WINDOW_MS <= rec_start <= end + MATCH_WINDOW_MS:
                if best is None:
                    best = rec
        return best

    # ---------- 启动对账 ----------

    def reconcile(self):
        """把磁盘上有、数据库里没有的录像补进来；上次进程中断的重新排队。"""
        now = int(time.time() * 1000)
        for rec in self.store.list_recordings(status="recording"):
            parts = rec.get("flv_parts") or []
            if parts:
                self.store.update_recording(
                    rec["id"], stopped_at=rec.get("stopped_at") or now,
                    status="pending", error="上次录制被中断")
                self.remux.enqueue(rec["id"])
            else:
                self.store.update_recording(
                    rec["id"], stopped_at=now, status="interrupted",
                    error="上次录制没有留下文件")

        root = os.path.join(self.data_dir, "recordings")
        if not os.path.isdir(root):
            return
        known = set()
        for rec in self.store.list_recordings():
            for part in (rec.get("flv_parts") or []) + (rec.get("mp4_parts") or []):
                if part.get("path"):
                    known.add(part["path"])
        for room_name in os.listdir(root):
            room_dir = os.path.join(root, room_name)
            if not os.path.isdir(room_dir) or not room_name.isdigit():
                continue
            groups = {}
            for name in os.listdir(room_dir):
                if not name.lower().endswith((".flv", ".mp4")):
                    continue
                rel = self.rel(os.path.join(room_dir, name))
                if rel in known:
                    continue
                base = name.rsplit(".", 1)[0]
                groups.setdefault(base, []).append(name)
            for base, names in groups.items():
                flv = sorted(n for n in names if n.lower().endswith(".flv"))
                mp4 = sorted(n for n in names if n.lower().endswith(".mp4"))
                stamp = _stamp_from_name(base) or int(
                    os.path.getmtime(os.path.join(room_dir, names[0])))
                parts = []
                for name in flv:
                    path = os.path.join(room_dir, name)
                    parts.append({"path": self.rel(path), "started_at": stamp,
                                  "bytes": _file_size(path),
                                  "duration_ms": _probe_duration(path)})
                mp4_parts = []
                for i, name in enumerate(mp4):
                    path = os.path.join(room_dir, name)
                    mp4_parts.append({"path": self.rel(path),
                                      "started_at": stamp,
                                      "bytes": _file_size(path),
                                      "duration_ms": _probe_duration(path)})
                rec_id = self.store.create_recording(
                    _as_int(room_name), base, stamp, 0,
                    started_at=stamp)
                self.store.update_recording(
                    rec_id,
                    status="ready" if mp4_parts else (
                        "pending" if flv and shutil.which("ffmpeg") else "flv"),
                    flv_parts=parts, mp4_parts=mp4_parts,
                    bytes=sum(p["bytes"] for p in (mp4_parts or parts)),
                    duration_ms=sum(p["duration_ms"] for p in (mp4_parts or parts)))
                if flv and shutil.which("ffmpeg"):
                    self.remux.enqueue(rec_id)

    # ---------- remux ----------

    def run_remux(self, rec_id):
        rec = self.store.get_recording(rec_id)
        if not rec:
            return
        if rec.get("status") == "recording":
            return
        parts = rec.get("flv_parts") or []
        if not parts:
            self.store.update_recording(rec_id, status="failed",
                                        error="没有 FLV 分片")
            return
        exe = shutil.which("ffmpeg")
        if not exe:
            self.store.update_recording(
                rec_id, status="flv",
                error="未安装 ffmpeg，保留 FLV；安装后可手动重试")
            self.log("warn", "[录像 %s] 未安装 ffmpeg，跳过 remux" % rec_id)
            return
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass
        self.store.update_recording(rec_id, status="remuxing", error="")
        try:
            flv_paths = [self.abs(p["path"]) for p in parts]
            base = os.path.splitext(os.path.basename(flv_paths[0]))[0]
            room_dir = os.path.dirname(flv_paths[0])
            mp4_parts = []
            self.log("info", "[录像 %s] 开始 remux：%d 个分片，%s" %
                     (rec_id, len(flv_paths), base))
            # 每个分片独立 remux，绝不跨分片 concat。重连后各分片的编码参数、
            # 帧率、时间戳基准可能不同，合并会损坏后半段（见模块说明）。
            for i, path in enumerate(flv_paths, 1):
                if len(flv_paths) == 1:
                    out = os.path.join(room_dir, base + ".mp4")
                else:
                    out = os.path.join(room_dir, "%s_p%d.mp4" % (base, i))
                t0 = time.time()
                self._ffmpeg_copy(exe, ["-i", path], out)
                self.log("info", "[录像 %s] remux 完成第 %d/%d 片：%s → %s（%.1f 秒）" %
                         (rec_id, i, len(flv_paths), os.path.basename(path),
                          os.path.basename(out), time.time() - t0))
                mp4_parts.append(self._part(out, parts[i - 1].get("started_at") or 0))
            if not mp4_parts or any(not os.path.getsize(self.abs(p["path"]))
                                    for p in mp4_parts):
                raise RuntimeError("remux 输出为空")
            total = sum(p["bytes"] for p in mp4_parts)
            duration = sum(p["duration_ms"] for p in mp4_parts)
            self.store.update_recording(
                rec_id, status="ready", mp4_parts=mp4_parts,
                bytes=total, duration_ms=duration, error="")
            # 只有全部 MP4 成功后才删源，失败绝不删
            for path in flv_paths:
                try:
                    os.remove(path)
                except OSError:
                    pass
            self.store.update_recording(rec_id, flv_parts=[])
            self.log("info", "[录像 %s] remux 完成：%d 个 MP4" %
                     (rec_id, len(mp4_parts)))
        except Exception as exc:
            self.store.update_recording(rec_id, status="failed",
                                        error=str(exc)[:300])
            self.log("warn", "[录像 %s] remux 失败：%s" % (rec_id, exc))

    def _part(self, path, started_at):
        return {"path": self.rel(path), "started_at": _as_int(started_at),
                "bytes": _file_size(path),
                "duration_ms": _probe_duration(path)}

    def _ffmpeg_copy(self, exe, inputs, out):
        tmp = out + ".tmp.mp4"
        cmd = [exe, "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
        cmd += inputs + ["-c", "copy", "-movflags", "+faststart", tmp]
        self._run(cmd)
        if not os.path.getsize(tmp):
            raise RuntimeError("remux 输出为空")
        os.replace(tmp, out)

    @staticmethod
    def _run(cmd):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "ffmpeg 失败").strip()[:300])


def _stamp_from_name(name):
    """从 ``20261116_001630_主播`` 解析毫秒时间戳。"""
    try:
        head = name.split("_", 2)
        if len(head) < 2:
            return 0
        return int(time.mktime(time.strptime(head[0] + head[1], "%Y%m%d%H%M%S")) * 1000)
    except (ValueError, OverflowError):
        return 0
