# -*- coding: utf-8 -*-
"""录像文件与数据库记录：开播录制、remux、按场次挂到日历。

文件统一放在 ``data/recordings/<room_id>/``，数据库只存相对路径，便于整个
data 目录搬走。录制中只写 FLV；停止后由 remux 线程把每个分片各自无损转成
MP4。**绝不跨分片 concat -c copy**：重连后新分片的 SPS/PPS、AAC 声道配置、
帧率、时间戳基准都可能变，无损拼接会让 MP4 只保留第一片参数集去解后续分片，
导致后半段画面和声音全部损坏（2026-11-19 场次踩过）。

播放不走单文件合并，而是**分 P**：前端把 ``_p1.mp4``、``_p2.mp4`` 当成选集，
用浏览器原生 ``<video>`` 依次播放。原生 MP4 不依赖 MSE，PC 和手机都能看。
跨分片合并成单文件需要重编码（码率上涨、体积更大、整场几十分钟），不划算，
因此不再自动合并；``run_merge`` 保留为手动接口，默认不排队。

remux 成功后把原始 FLV 归档到 ``data/cache/flv/<room_id>/``：录节目录只留
MP4 分片，缓存只做数据恢复用，不参与播放。等分 P 方案稳定、确认无需回滚
再考虑清理。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

from remux import RemuxWorker
from flv_index import inject as flv_inject_index

MIN_FREE_BYTES = 1024 * 1024 * 1024      # 录制硬下限 1GB
WARN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 页面预警 2GB
MATCH_WINDOW_MS = 10 * 60 * 1000

# 播放与缓存
KEEP_FLV_DEFAULT = True       # remux 后保留 FLV 原片，归档到缓存目录做恢复兜底
FLV_CACHE_DIR = "cache/flv"   # 归档目录（相对 data_dir）
RETRY_NIGHT_START = 3         # 失败重试的夜间窗口 [3, 8)
RETRY_NIGHT_END = 8


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
    def __init__(self, store, data_dir, log=None, config_getter=None):
        self.store = store
        self.data_dir = os.path.abspath(data_dir)
        self.log = log or (lambda *_: None)
        self.config_getter = config_getter or (lambda: {})
        self.remux = RemuxWorker(self.run_remux, self.log)
        # 合并单文件用独立队列：重编码很慢，不能堵住分片 remux
        self.merge = RemuxWorker(self.run_merge, self.log, name="recording-merge")
        self._night_stop = threading.Event()
        self._night_thread = None

    def _cfg(self, key, default):
        try:
            return self.config_getter().get(key, default)
        except Exception:
            return default

    @property
    def keep_flv(self):
        return bool(self._cfg("keep_flv", KEEP_FLV_DEFAULT))

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
                if part["bytes"] <= 0:
                    # 重连时可能连上就断，写出 0 字节空分片。ffmpeg 打不开它，
                    # 会让整场 remux 失败（11-22 下午场）；当场丢掉，别留到后面。
                    parts.remove(part)
                    try:
                        os.remove(full)
                    except OSError:
                        pass
                    self.log("warn", "[录像 %s] 空分片已丢弃：%s" %
                             (rec_id, os.path.basename(rel)))
                    break
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

    def retry(self, rec_id, force=False):
        """手动重转。默认只补缺失的 MP4；force=True 才全部重转。"""
        rec = self.store.get_recording(rec_id)
        if not rec:
            raise ValueError("录像不存在")
        if not rec.get("flv_parts"):
            raise ValueError("没有 FLV 分片可 remux")
        patch = {"status": "pending", "error": ""}
        if force:
            patch["mp4_parts"] = []
        self.store.update_recording(rec_id, **patch)
        self.remux.enqueue(rec_id)

    # ---------- 删除 ----------

    @staticmethod
    def _busy(rec):
        """录制 / 转封装 / 合并中的录像不能删文件，免得和工作线程抢文件。"""
        return ((rec.get("status") or "") in ("recording", "remuxing")
                or (rec.get("merge_status") or "") == "merging")

    def _unlink(self, paths):
        """只删数据库登记过的相对路径，逐个报错，不按目录通配。"""
        count, freed, errors = 0, 0, []
        for rel in paths:
            if not rel:
                continue
            try:
                path = self.abs(rel)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            try:
                if os.path.isfile(path):
                    freed += os.path.getsize(path)
                    os.remove(path)
                    count += 1
            except OSError as exc:
                errors.append(os.path.basename(path) + "：" + str(exc))
        return count, freed, errors

    def delete_part(self, rec_id, kind, index):
        """删一条录像的某个 MP4 分 P 或 FLV 原片，保留其余分片和录像记录。"""
        rec = self.get(rec_id)
        if not rec:
            raise ValueError("录像不存在")
        if self._busy(rec):
            raise ValueError("录像正在处理中，稍后再删")
        key = "mp4_parts" if kind == "mp4" else "flv_parts"
        parts = rec.get(key) or []
        index = _as_int(index, -1)
        if index < 0 or index >= len(parts):
            raise ValueError("分片不存在")
        part = parts[index]
        count, freed, errors = self._unlink([part.get("path")])
        if errors:
            raise OSError("；".join(errors))
        parts.pop(index)
        remain_flv = rec.get("flv_parts") or []
        remain_mp4 = rec.get("mp4_parts") or []
        basis = remain_flv or remain_mp4
        fields = {key: parts,
                  "bytes": sum(_as_int(p.get("bytes")) for p in basis),
                  "duration_ms": sum(_as_int(p.get("duration_ms")) for p in basis)}
        self.store.update_recording(rec_id, **fields)
        self.log("info", "[录像 %s] 已删除 %s 第 %d 片，释放 %.1f MB" %
                 (rec_id, kind, index + 1, freed / 1024.0 / 1024.0))
        return {"rec_id": _as_int(rec_id), "kind": kind, "index": index,
                "files": count, "bytes": freed}

    def delete_recording(self, rec_id):
        """删一整条录像：所有 MP4 / FLV / 旧合并文件 + 录像记录，不碰文本数据。"""
        rec = self.get(rec_id)
        if not rec:
            return None
        if self._busy(rec):
            raise ValueError("录像正在处理中，稍后再删")
        paths = [p.get("path") for p in
                 (rec.get("mp4_parts") or []) + (rec.get("flv_parts") or [])]
        paths.append(rec.get("merged_path"))
        count, freed, errors = self._unlink(paths)
        if errors:
            raise OSError("；".join(errors))
        self.store.delete_recording(rec_id)
        self.log("info", "[录像 %s] 已删除整条录像：%d 个文件，释放 %.1f MB" %
                 (rec_id, count, freed / 1024.0 / 1024.0))
        return {"rec_id": _as_int(rec_id), "files": count, "bytes": freed}

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
            "merge_status": rec.get("merge_status") or "",
            "merge_error": rec.get("merge_error") or "",
            "merge_seconds": rec.get("merge_seconds") or 0,
            "merged": (None if not (rec.get("merged_path") or "")
                       else {"bytes": rec.get("merged_bytes") or 0,
                             "duration_ms": rec.get("merged_duration_ms") or 0,
                             "kind": ("flv" if (rec.get("merged_path") or "")
                                      .lower().endswith(".flv") else "mp4")}),
            "mp4_parts": [
                {"index": i, "name": os.path.basename(p.get("path") or ""),
                 "started_at": p.get("started_at") or 0,
                 "bytes": p.get("bytes") or 0,
                 "duration_ms": p.get("duration_ms") or 0}
                for i, p in enumerate(mp4)
            ],
            "flv_parts": [
                {"index": i, "name": os.path.basename(p.get("path") or ""),
                 "started_at": p.get("started_at") or 0,
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
        used: set[int] = set()          # 一场录像只能属于一场直播
        for seg in segments:
            if seg.get("kind") != "live":
                seg["recording"] = None
                continue
            rec = self._match(seg, recs, used)
            seg["recording"] = self.public(rec)
            if rec and rec.get("id"):
                used.add(rec["id"])
        return segments

    @staticmethod
    def _match(seg, recs, used=None):
        """把一段场次对到它的录像。

        只认「录制真正开始的时间」（started_at）落在本场区间内，因为它无法
        被别的场次继承；`live_started_at` 是补记开播时写进去的，可能残留上一
        场的值（19 号那场就带着 18 号的开播时间），只在它离 started_at 足够近
        时才作为兜底依据。
        """
        start = _as_int(seg.get("start"))
        end = _as_int(seg.get("end")) or int(time.time() * 1000)
        skip = used or set()
        pool = [r for r in recs if r.get("id") not in skip]

        best, best_gap = None, MATCH_WINDOW_MS + 1
        for rec in pool:
            rec_start = _as_int(rec.get("started_at"))
            if not rec_start:
                continue
            if not (start - MATCH_WINDOW_MS <= rec_start <= end + MATCH_WINDOW_MS):
                continue
            gap = abs(rec_start - start)
            if gap < best_gap:
                best, best_gap = rec, gap
        if best is not None:
            return best

        for rec in pool:
            point = _as_int(rec.get("live_started_at"))
            if not point:
                continue
            rec_start = _as_int(rec.get("started_at"))
            if rec_start and abs(point - rec_start) > MATCH_WINDOW_MS:
                continue            # 上一场残留的开播时间，不可信
            gap = abs(point - start)
            if gap < best_gap:
                best, best_gap = rec, gap
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
        rec_starts = []
        for rec in self.store.list_recordings():
            for part in (rec.get("flv_parts") or []) + (rec.get("mp4_parts") or []):
                if part.get("path"):
                    known.add(part["path"])
            # 补转产物（_p3_p1.mp4）和原 FLV 的时间戳相同、路径却不在已登记
            # 列表里。只按路径判重会把它当成新场次再建一条记录，然后 attach
            # 又绑到那条没有 FLV 的记录上。用起始时间再兜一层。
            start = _as_int(rec.get("started_at"))
            if start:
                rec_starts.append(start)
            # 旧版合并成品（_merged.flv / _merged.mp4）也算已登记，
            # 避免被当成新分片重复建记录
            if rec.get("merged_path"):
                known.add(rec["merged_path"])
            # 旧场次的 FLV 还留在录节目录里，MP4 已全部就绪时补一次归档
            if rec.get("status") == "ready" and (rec.get("mp4_parts") or []):
                flvs = rec.get("flv_parts") or []
                stale = [self.abs(p["path"]) for p in flvs
                         if (p.get("path") or "").startswith("recordings/")]
                if stale and all(os.path.isfile(p) for p in stale):
                    try:
                        moved = self._archive_flv(rec, flvs, stale)
                        self.store.update_recording(rec["id"], flv_parts=moved)
                        self.log("info", "[录像 %s] 旧 FLV 已归档到缓存：%d 片" %
                                 (rec["id"], len(moved)))
                    except Exception as exc:
                        self.log("warn", "[录像 %s] 旧 FLV 归档失败：%s" %
                                 (rec["id"], exc), exc=exc)
        for room_name in os.listdir(root):
            room_dir = os.path.join(root, room_name)
            if not os.path.isdir(room_dir) or not room_name.isdigit():
                continue
            groups = {}
            for name in os.listdir(room_dir):
                if not name.lower().endswith((".flv", ".mp4")):
                    continue
                if name.endswith(("_merged.mp4", "_merged.flv")):
                    continue  # 合并成品不是分片
                if name.startswith("_merge_"):
                    continue  # 合并过程中的临时 FLV
                rel = self.rel(os.path.join(room_dir, name))
                if rel in known:
                    continue
                base = name.rsplit(".", 1)[0]
                groups.setdefault(base, []).append(name)
            for base, names in groups.items():
                # 磁盘文件的场次时间戳若落在已有录像的起始时间附近，说明它
                # 是那场的补转产物 / 归档残留，不是新录像（11-22 的重复记录
                # 就是因为补转产生的 _p3_p1.mp4 没登记而新建了一条）。
                file_stamp = _stamp_from_name(base)
                if file_stamp and any(abs(file_stamp - s) <= 60_000
                                      for s in rec_starts):
                    continue
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

        self._start_night_retry()

    def _start_night_retry(self):
        """每 5 分钟检查一次夜间窗口，失败合并自动补跑；重复调用只起一个。"""
        if self._night_thread and self._night_thread.is_alive():
            return

        def loop():
            while not self._night_stop.wait(300):
                try:
                    self.nightly_retry()
                except Exception as exc:
                    self.log("warn", "[录像] 夜间重试检查出错：%s" % exc)

        self._night_thread = threading.Thread(
            target=loop, name="recording-night-retry", daemon=True)
        self._night_thread.start()

    def stop(self):
        self._night_stop.set()

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
        mp4_parts = []
        try:
            # 再兜一层：旧场次 / 手工导入的库里可能还留着空分片。
            usable = []
            for part in parts:
                full = self.abs(part.get("path") or "")
                if _as_int(part.get("bytes")) <= 0 or _file_size(full) <= 0:
                    self.log("warn", "[录像 %s] 跳过空/缺失分片：%s" %
                             (rec_id, os.path.basename(full)))
                    continue
                usable.append((part, full))
            if not usable:
                raise RuntimeError("没有可 remux 的有效 FLV 分片")
            parts = [part for part, _ in usable]
            flv_paths = [full for _, full in usable]
            base = os.path.splitext(os.path.basename(flv_paths[0]))[0]
            room_dir = os.path.dirname(flv_paths[0])
            # 手动补转（以及夜间重试）只处理还没有 MP4 的分片。按 started_at
            # 对齐：它是分片落盘时刻，不受重连改名 / 归档目录影响。force 重试
            # 会在 retry() 里先清空 mp4_parts，走到这里就是全转。
            done = {}
            for prev in (rec.get("mp4_parts") or []):
                stamp = _as_int(prev.get("started_at"))
                if stamp <= 0:
                    continue
                try:
                    if _file_size(self.abs(prev.get("path") or "")) > 0:
                        done[stamp] = prev
                except ValueError:
                    continue
            if done:
                self.log("info", "[录像 %s] 已有 %d 个 MP4，本次只补缺失分片" %
                         (rec_id, len(done)))
            self.log("info", "[录像 %s] 开始 remux：%d 个分片，%s" %
                     (rec_id, len(flv_paths), base))
            # 每个分片独立 remux，绝不跨分片 concat。重连后各分片的编码参数、
            # 帧率、时间戳基准可能不同，合并会损坏后半段（见模块说明）。
            for i, path in enumerate(flv_paths, 1):
                stamp = _as_int(parts[i - 1].get("started_at"))
                prev = done.get(stamp) if stamp > 0 else None
                if prev:
                    mp4_parts.append(prev)
                    self.log("info", "[录像 %s] 复用已有 MP4：%s" %
                             (rec_id, os.path.basename(prev.get("path") or "")))
                    continue
                if len(flv_paths) == 1:
                    out = os.path.join(room_dir, base + ".mp4")
                else:
                    out = os.path.join(room_dir, "%s_p%d.mp4" % (base, i))
                t0 = time.time()
                self._ffmpeg_copy(exe, ["-i", path], out)
                self.log("info", "[录像 %s] remux 完成第 %d/%d 片：%s → %s（%.1f 秒）" %
                         (rec_id, i, len(flv_paths), os.path.basename(path),
                          os.path.basename(out), time.time() - t0))
                mp4_parts.append(self._part(out, stamp))
            if not mp4_parts or any(not os.path.getsize(self.abs(p["path"]))
                                    for p in mp4_parts):
                raise RuntimeError("remux 输出为空")
            total = sum(p["bytes"] for p in mp4_parts)
            duration = sum(p["duration_ms"] for p in mp4_parts)
            self.store.update_recording(
                rec_id, status="ready", mp4_parts=mp4_parts,
                bytes=total, duration_ms=duration, error="")
            # 录节目录只留 MP4 分片：FLV 原片归档到缓存目录做恢复兜底，
            # 只有 keep_flv=false 时才真删，且只在全部 MP4 成功后才动。
            if self.keep_flv:
                moved = self._archive_flv(rec, parts, flv_paths)
                self.store.update_recording(rec_id, flv_parts=moved)
                self.log("info", "[录像 %s] remux 完成：%d 个 MP4，FLV 原片已归档到缓存" %
                         (rec_id, len(mp4_parts)))
            else:
                for path in flv_paths:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                self.store.update_recording(rec_id, flv_parts=[])
                self.log("info", "[录像 %s] remux 完成：%d 个 MP4，FLV 源已清理" %
                         (rec_id, len(mp4_parts)))
        except Exception as exc:
            # 已转好的分片留在数据库里，失败时前端还能看这部分，
            # 重试也不会把它们当孤立文件重复生成（11-22 下午场的问题）。
            patch = {"status": "failed", "error": str(exc)[:300]}
            if mp4_parts:
                patch["mp4_parts"] = mp4_parts
            self.store.update_recording(rec_id, **patch)
            self.log("error", "[录像 %s] remux 失败：%s" % (rec_id, exc), exc=exc)

    def _archive_flv(self, rec, parts, flv_paths):
        """把 FLV 原片移到 ``data/cache/flv/<room_id>/``，返回新的 flv_parts。

        只做备份：录节目录保持干净、播放全走 MP4 分片；需要重跑 remux 时，
        后端仍按 flv_parts 里的新路径读得到文件。
        """
        room = str(_as_int(rec.get("room_id")))
        dst_dir = os.path.join(self.data_dir, FLV_CACHE_DIR, room)
        moved = []
        for i, path in enumerate(flv_paths):
            part = dict(parts[i]) if i < len(parts) else {}
            dst = os.path.join(dst_dir, os.path.basename(path))
            try:
                if os.path.isfile(path):
                    os.makedirs(dst_dir, exist_ok=True)
                    shutil.move(path, dst)
                part["path"] = self.rel(dst)
                part["bytes"] = _file_size(dst)
            except OSError as exc:
                self.log("warn", "[录像] FLV 归档失败 %s：%s" %
                         (os.path.basename(path), exc))
                part["path"] = self.rel(path)
            moved.append(part)
        return moved

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

    # ---------- 合并成单 FLV ----------

    def merged_path(self, rec):
        """合并成品路径：与分片同目录的 ``<base>_merged.flv``。"""
        parts = rec.get("mp4_parts") or []
        stored = rec.get("merged_path") or ""
        if stored.lower().endswith(".flv"):
            return self.abs(stored)
        if not parts:
            return ""
        base = os.path.splitext(os.path.basename(self.abs(parts[0].get("path"))))[0]
        if base.endswith("_p1"):
            base = base[:-3]
        return os.path.join(os.path.dirname(self.abs(parts[0].get("path"))),
                            base + "_merged.flv")

    def run_merge(self, rec_id):
        """把多个 MP4 分片无损合并成单个 FLV，前端只播放一个视频。

        先把每片 -c copy 转成 FLV，再 concat copy。FLV 每段开头都有
        AVC/AAC sequence header，播放器遇到新参数会重新生成 init segment，
        因此重连后 SPS/PPS、帧率、声道变化都能安全拼接；全程不重编码，
        十几秒完成且画质零损失。失败只记状态、绝不动 FLV 源与分片。
        """
        rec = self.store.get_recording(rec_id)
        if not rec:
            return
        parts = rec.get("mp4_parts") or []
        if len(parts) < 2:
            return
        exe = shutil.which("ffmpeg")
        if not exe:
            self.store.update_recording(
                rec_id, merge_status="error",
                merge_error="未安装 ffmpeg，无法合并")
            return
        out = self.merged_path(rec)
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass
        self.store.update_recording(
            rec_id, merge_status="merging", merge_error="", merge_seconds=0)
        started = time.time()
        tag = "_merge_%d_%d" % (rec_id, int(started * 1000))
        workdir = os.path.dirname(out)
        tmp_parts = [os.path.join(workdir, "%s_p%d.flv" % (tag, i))
                     for i in range(len(parts))]
        list_file = os.path.join(workdir, tag + "_list.txt")
        tmp_out = os.path.join(workdir, tag + ".tmp.flv")
        try:
            srcs = [self.abs(p.get("path")) for p in parts]
            missing = [s for s in srcs if not os.path.isfile(s)]
            if missing:
                raise RuntimeError("有 %d 个分片文件丢失" % len(missing))
            self.log("info", "[录像 %s] 开始合并：%d 片 → 单 FLV（无损 copy）" %
                     (rec_id, len(srcs)))

            try:
                for src, dst in zip(srcs, tmp_parts):
                    self._run([exe, "-nostdin", "-y", "-hide_banner",
                               "-loglevel", "error", "-i", src,
                               "-c", "copy", "-f", "flv", dst])
                with open(list_file, "w", encoding="utf-8") as f:
                    for p in tmp_parts:
                        f.write("file '%s'\n" % p.replace("'", "'\\''"))
                self._run([exe, "-nostdin", "-y", "-hide_banner",
                           "-loglevel", "error", "-f", "concat", "-safe", "0",
                           "-i", list_file, "-c", "copy", "-f", "flv", tmp_out])
                if not os.path.getsize(tmp_out):
                    raise RuntimeError("合并输出为空")
                # 补写 keyframes 索引，否则播放器能顺序播但拖动会卡在加载。
                # 先注入到另一个临时文件，成功后再整体替换，失败不污染产物。
                indexed = tmp_out + ".idx.flv"
                info = flv_inject_index(tmp_out, indexed)
                os.replace(indexed, out)
                self.log("info", "[录像 %s] 已写入 %d 个关键帧索引（%.0f–%.0fs）" %
                         (rec_id, info["keyframes"], info["first_kf_sec"],
                          info["last_kf_sec"]))
            finally:
                for p in tmp_parts + [list_file, tmp_out, tmp_out + ".idx.flv"]:
                    try:
                        os.remove(p)
                    except OSError:
                        pass

            seconds = round(time.time() - started, 1)
            dur, _ = _probe(out)
            old = rec.get("merged_path") or ""
            if old.lower().endswith(".mp4") and self.rel(out) != old:
                try:
                    os.remove(self.abs(old))
                    self.log("info", "[录像 %s] 已清理旧的 MP4 合并产物" % rec_id)
                except OSError:
                    pass
            self.store.update_recording(
                rec_id, merge_status="done", merged_path=self.rel(out),
                merged_bytes=_file_size(out), merged_duration_ms=dur,
                merge_error="", merge_seconds=seconds)
            self.log("info", "[录像 %s] 合并完成：%d 片 → 单 FLV %.1fMB，耗时 %.1fs" %
                     (rec_id, len(srcs), _file_size(out) / 1048576.0, seconds))
        except Exception as exc:
            self.store.update_recording(
                rec_id, merge_status="error", merge_error=str(exc)[:300])
            self.log("warn", "[录像 %s] 合并失败（FLV 与分片均保留，可重试）：%s" %
                     (rec_id, exc))

    def reindex_merged(self, rec_id):
        """给旧的单 FLV 合并产物补写 keyframes 索引（幂等）。

        早期版本合并出的 FLV 没有索引，能顺序播但拖动卡加载。已带索引的
        文件跳过。返回 (是否处理, 关键帧数或说明)。
        """
        rec = self.store.get_recording(rec_id)
        if not rec:
            raise ValueError("录像不存在")
        path = rec.get("merged_path") or ""
        if not path.lower().endswith(".flv"):
            return False, "不是 FLV 合并产物"
        target = self.abs(path)
        if not os.path.isfile(target):
            return False, "合并文件不存在"
        with open(target, "rb") as f:
            head = f.read(64 * 1024)
        if b"keyframes" in head:
            return False, "已带索引"
        tmp = target + ".idx.flv"
        try:
            info = flv_inject_index(target, tmp)
            os.replace(tmp, target)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self.log("info", "[录像 %s] 回填 %d 个关键帧索引" %
                 (rec_id, info["keyframes"]))
        return True, info["keyframes"]

    def retry_merge(self, rec_id):
        rec = self.store.get_recording(rec_id)
        if not rec:
            raise ValueError("录像不存在")
        if len(rec.get("mp4_parts") or []) < 2:
            raise ValueError("该录像没有多个分片，无需合并")
        self.store.update_recording(rec_id, merge_status="pending", merge_error="")
        self.merge.enqueue(rec_id)

    def nightly_retry(self):
        """夜间窗口内重跑失败的 remux；非窗口时段不动。

        分 P 播放不依赖单文件合并，夜间只补转封装失败的场次：FLV 还在
        缓存目录里就能重来。
        """
        hour = time.localtime().tm_hour
        if not (RETRY_NIGHT_START <= hour < RETRY_NIGHT_END):
            return 0
        pending = [r for r in self.store.list_recordings()
                   if r.get("status") == "failed" and (r.get("flv_parts") or [])]
        for rec in pending:
            self.log("info", "[录像 %s] 夜间重试转封装" % rec["id"])
            self.remux.enqueue(rec["id"])
        return len(pending)


def _probe_stream(path):
    """探测视频分辨率/帧率，供合并统一参数用；失败给保守默认值。"""
    out = {"width": 1280, "height": 720, "fps": 30}
    exe = shutil.which("ffprobe")
    if not exe:
        return out
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout or "{}")
        s = ((data.get("streams") or [{}])[0])
        if s.get("width"):
            out["width"] = int(s["width"])
        if s.get("height"):
            out["height"] = int(s["height"])
        fps = s.get("r_frame_rate") or ""
        if "/" in fps:
            num, den = fps.split("/", 1)
            if float(den or 0) > 0:
                out["fps"] = max(1, min(60, round(float(num) / float(den))))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return out


def _stamp_from_name(name):
    """从 ``20261116_001630_主播`` 解析毫秒时间戳。"""
    try:
        head = name.split("_", 2)
        if len(head) < 2:
            return 0
        return int(time.mktime(time.strptime(head[0] + head[1], "%Y%m%d%H%M%S")) * 1000)
    except (ValueError, OverflowError):
        return 0
