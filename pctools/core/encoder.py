# -*- coding: utf-8 -*-
"""编码器探测、命令构造与进度解析。

只用 ffmpeg 命令行，不用 python 绑定——``-progress pipe:1`` 能拿到精确
进度，``h264_nvenc`` 这类硬件编码器也直接可用，出错还能把 stderr 原文
交给用户看。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0

# 优先级：核显/独显硬件编码优先，都没有才回退 libx264。
ORDER = ("nvenc", "qsv", "amf", "x264")

CODECS = {
    "nvenc": "h264_nvenc",
    "qsv": "h264_qsv",
    "amf": "h264_amf",
    "x264": "libx264",
}

LABELS = {
    "nvenc": "NVIDIA NVENC",
    "qsv": "Intel QuickSync",
    "amf": "AMD AMF",
    "x264": "libx264（CPU）",
}


def detect(ffmpeg: str) -> list[str]:
    """返回本机可用的编码器代号，按 ``ORDER`` 排序。"""
    if not ffmpeg:
        return []
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=60,
                             creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    text = (out.stdout or "") + (out.stderr or "")
    found = [k for k in ORDER if CODECS[k] in text]
    return found


def pick(available: list[str], prefer: str = "auto") -> str:
    """``prefer`` 指定且可用就用它，否则按优先级取第一个。"""
    if prefer and prefer != "auto" and prefer in available:
        return prefer
    return available[0] if available else ""


def quality(encoder: str, crf: int) -> int:
    """硬件编码的 cq 与 x264 的 crf 不等价，同数值画质更差，压低 2 档补偿。"""
    return max(0, int(crf) - 2) if encoder != "x264" else int(crf)


def build(ffmpeg: str, src: str, dst: str, *, encoder: str, crf: int = 23,
          max_bitrate: int = 1600, audio_bitrate: int = 96,
          threads: int = 0) -> list[str]:
    """构造一条重编码命令。

    刻意不加 ``-r`` / ``-vsync``，并用 ``-fps_mode passthrough``：帧数与帧
    时序必须和源完全一致，否则手机端 commit 会按时长漂移拒收，弹幕轴也会错。
    """
    q = quality(encoder, crf)
    cap = ["-maxrate", "%dk" % max_bitrate, "-bufsize", "%dk" % (max_bitrate * 2)] \
        if max_bitrate else []

    if encoder == "nvenc":
        venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(q), "-b:v", "0"] + cap
    elif encoder == "qsv":
        venc = ["-c:v", "h264_qsv", "-global_quality", str(q),
                "-look_ahead", "1"] + cap
    elif encoder == "amf":
        venc = ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", str(q),
                "-qp_p", str(q)] + cap
    else:
        venc = ["-c:v", "libx264", "-preset", "medium", "-crf", str(q)] + cap
        if threads:
            venc += ["-threads", str(threads)]

    return ([ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
             "-progress", "pipe:1", "-nostats",
             "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-fps_mode",
             "passthrough"] + venc +
            ["-c:a", "aac", "-b:a", "%dk" % audio_bitrate, "-ac", "2",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst])


def run(cmd: list[str], duration_ms: int, on_progress=None,
        holder: dict | None = None) -> tuple[int, str]:
    """跑命令，返回 ``(returncode, stderr 尾部)``。

    ``on_progress(percent)`` 会被高频调用（每 ~0.5 秒一次）。
    ``holder["proc"]`` 里能拿到进程句柄，供界面上的「停止」用。
    """
    err_file = tempfile.TemporaryFile(mode="w+b")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=err_file, text=True, bufsize=1,
                                creationflags=CREATE_NO_WINDOW)
        if holder is not None:
            holder["proc"] = proc
        total = max(1, int(duration_ms))
        for line in proc.stdout or []:
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                raw = line.split("=", 1)[1]
                try:
                    us = int(raw)
                except ValueError:
                    continue
                if line.startswith("out_time_ms="):
                    us *= 1000        # 旧版 ffmpeg 这个字段名写的其实是微秒
                if on_progress:
                    on_progress(min(0.999, us / 1000.0 / total))
            elif line == "progress=end" and on_progress:
                on_progress(1.0)
        proc.wait()
        err_file.seek(0)
        tail = err_file.read().decode("utf-8", "replace").strip()
        return proc.returncode, tail[-2000:]
    finally:
        if holder is not None:
            holder.pop("proc", None)
        try:
            err_file.close()
        except OSError:
            pass


def stop(holder: dict) -> None:
    proc = holder.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


def sample_cmd(ffmpeg: str, src: str, dst: str, *, start: int, seconds: int,
               encoder: str, crf: int, max_bitrate: int,
               audio_bitrate: int) -> list[str]:
    """试算命令：只压中间一段，用来外推全片体积。

    ``-ss`` 放在 ``-i`` 之后（解码后切）保证切点落在帧边界；源是 MP4 时
    精确到帧，这也是全程用 MP4 而不是 FLV 的原因之一。
    """
    base = build(ffmpeg, src, dst, encoder=encoder, crf=crf,
                 max_bitrate=max_bitrate, audio_bitrate=audio_bitrate)
    j = base.index("-map")
    return base[:j] + ["-ss", str(start), "-t", str(seconds)] + base[j:]
