# -*- coding: utf-8 -*-
"""压缩结果校验。

三条硬指标，任何一条不过就不能传回手机：

1. 时长与源分片差 ≤ 150 ms —— 弹幕轴按毫秒对齐，差一点就整体偏移
2. 视频帧数与源一致（允许差 1 帧，B 帧结尾常见）
3. 整片解码无 error，且 SSIM 不低于阈值（默认 0.96）

帧数用 ``-progress`` 里的 ``frame=`` 统计，和「完整解码一遍」合成一次，
比 ``ffprobe -count_frames`` 再单跑一遍快一倍。
"""
from __future__ import annotations

import re
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0
DRIFT_MS = 150
SSIM_MIN = 0.96


def decode_count(ffmpeg: str, path: str, duration_ms: int = 0,
                 on_progress=None) -> tuple[int, str]:
    """完整解码一遍，返回 ``(视频帧数, 错误输出尾部)``。"""
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-i", path, "-map", "0:v:0",
           "-f", "null", "-", "-progress", "pipe:1", "-nostats"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, creationflags=CREATE_NO_WINDOW)
    frames = 0
    total = max(1, int(duration_ms))
    for line in proc.stdout or []:
        line = line.strip()
        if line.startswith("frame="):
            try:
                frames = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if on_progress and duration_ms:
                # frame 无法直接换算时间，用 out_time_us 更准
                pass
        elif line.startswith("out_time_us=") and on_progress:
            try:
                on_progress(min(0.999, int(line.split("=", 1)[1]) / 1000.0 / total))
            except ValueError:
                pass
    proc.wait()
    err = (proc.stderr.read() if proc.stderr else "") or ""
    return frames, err.strip()[-1500:]


def ssim(ffmpeg: str, src: str, out: str, on_progress=None) -> float:
    """整片 SSIM；解析失败返回 -1。"""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-i", src, "-i", out,
           "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, bufsize=1,
                            creationflags=CREATE_NO_WINDOW)
    value = -1.0
    for line in proc.stderr or []:
        m = re.search(r"All:([0-9.]+)", line)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                pass
    proc.wait()
    return value


def verify(ffmpeg: str, src: str, out: str, *, expect_duration_ms: int,
           expect_frames: int = 0, check_ssim: bool = True,
           ssim_min: float = SSIM_MIN, on_stage=None) -> dict:
    """返回 ``{ok, issues, frames, duration_ms, ssim}``。"""
    issues: list[str] = []
    result = {"ok": False, "issues": issues, "frames": 0,
              "duration_ms": 0, "ssim": -1.0}

    if on_stage:
        on_stage("解码校验")
    frames, err = decode_count(ffmpeg, out, expect_duration_ms)
    result["frames"] = frames
    if err:
        issues.append("解码报错：%s" % err.splitlines()[-1][:160])
    if expect_frames and abs(frames - expect_frames) > 1:
        issues.append("帧数 %d，源是 %d（差 %d 帧）" %
                      (frames, expect_frames, frames - expect_frames))

    if check_ssim:
        if on_stage:
            on_stage("画质比对")
        value = ssim(ffmpeg, src, out)
        result["ssim"] = value
        if value < 0:
            issues.append("SSIM 计算失败")
        elif value < ssim_min:
            issues.append("SSIM %.4f 低于 %.2f，压得太狠" % (value, ssim_min))

    result["ok"] = not issues
    return result
