# -*- coding: utf-8 -*-
"""ffprobe 封装：一次调用拿全判断需要的元数据。"""
from __future__ import annotations

import json
import os
import subprocess


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _fps(raw: str) -> float:
    parts = (raw or "").split("/")
    if len(parts) == 2:
        try:
            num, den = float(parts[0]), float(parts[1])
            return round(num / den, 3) if den else 0.0
        except ValueError:
            return 0.0
    try:
        return round(float(raw), 3)
    except (TypeError, ValueError):
        return 0.0


def probe(ffprobe: str, path: str, count_frames: bool = False) -> dict:
    """返回 ``{duration_ms, bytes, vcodec, width, height, fps, bitrate, ...}``。

    失败抛 ``RuntimeError``（带 ffprobe 的 stderr 尾部），不吞异常——
    这里读不出来就说明文件有问题，必须让调用方看见。
    """
    if not ffprobe:
        raise RuntimeError("没有 ffprobe，无法读取视频信息")
    fields = ("index,codec_type,codec_name,width,height,r_frame_rate,"
              "sample_rate,channels,bit_rate,")
    fields += "nb_read_frames" if count_frames else "nb_frames"
    cmd = [ffprobe, "-v", "error"]
    if count_frames:
        cmd.append("-count_frames")
    cmd += ["-show_entries",
            "format=duration,size,bit_rate:stream=" + fields,
            "-of", "json", path]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("ffprobe 调不起来：%s" % exc)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "ffprobe 失败").strip()[:300])
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {}) or {}
    a = next((s for s in streams if s.get("codec_type") == "audio"), {}) or {}
    fmt = data.get("format") or {}
    frames = _as_int(v.get("nb_read_frames") or v.get("nb_frames"))
    return {
        "path": path,
        "bytes": _as_int(fmt.get("size")) or _size(path),
        "duration_ms": _as_int(float(fmt.get("duration") or 0) * 1000),
        "bitrate": _as_int(fmt.get("bit_rate")),
        "vcodec": v.get("codec_name") or "",
        "acodec": a.get("codec_name") or "",
        "width": _as_int(v.get("width")),
        "height": _as_int(v.get("height")),
        "fps": _fps(v.get("r_frame_rate")),
        "frames": frames,
        "audio_channels": _as_int(a.get("channels")),
        "audio_rate": _as_int(a.get("sample_rate")),
    }


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def summary(info: dict) -> str:
    """给表格/日志用的一行描述。"""
    if not info:
        return "—"
    bits = []
    if info.get("width"):
        bits.append("%dx%d" % (info["width"], info["height"]))
    if info.get("fps"):
        bits.append("%gfps" % info["fps"])
    if info.get("vcodec"):
        bits.append(info["vcodec"])
    return " ".join(bits) or "—"


def kbps(info: dict) -> int:
    return int(round(_as_int(info.get("bitrate")) / 1000.0))


def mb(size_bytes: int) -> float:
    return round(_as_int(size_bytes) / 1048576.0, 1)
