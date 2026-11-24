# -*- coding: utf-8 -*-
"""配置与外部依赖（ffmpeg）定位。"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

APP_DIR = Path.home() / ".live-capture-pc"
CONFIG_PATH = APP_DIR / "config.json"

# vendor 里放一份静态 ffmpeg（不在 git 里），优先于 PATH 使用，
# 这样版本固定，不会因为用户机器上装着别的 build 而行为漂移。
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg" / "bin"

DEFAULTS = {
    "host": "127.0.0.1:8765",              # 走 adb forward 时的地址
    "fallback_host": "",                   # adb 不可用时回落的 Wi-Fi 地址
    "token": "",
    "ffmpeg_path": "",                     # 手动指定；空则自动探测
    "encoder": "auto",                     # auto | nvenc | qsv | amf | x264
    "crf": 23,                             # x264 的 crf / 硬件编码的 cq
    "max_bitrate": 1600,                   # kbps，0 表示不限峰值
    "audio_bitrate": 96,                   # kbps
    "min_bitrate": 1200,                   # 源码率低于此值（kbps）不建议压
    "min_part_mb": 20,                     # 分片小于这个体积直接跳过（断流碎片）
    "chunk_mb": 8,                         # 上传分块大小
    "keep_days": 7,                        # 本地保留原件天数
    "pause_when_live": True,               # 开播时拒绝/暂停高 IO 任务
    "check_ssim": True,                    # 压完做整片 SSIM 比对
    "work_dir": str(APP_DIR / "work"),
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def find_ffmpeg(cfg: dict | None = None) -> tuple[str, str]:
    """按 配置 → 环境变量 → vendor → PATH 找到 ``(ffmpeg, ffprobe)``。

    找不到时返回 ``("", "")``，由调用方提示用户放一份到 vendor 目录。
    """
    exe = ".exe" if sys.platform.startswith("win") else ""
    names = ("ffmpeg" + exe, "ffprobe" + exe)

    def pair(folder: str) -> tuple[str, str]:
        a = os.path.join(folder, names[0]) if folder else ""
        b = os.path.join(folder, names[1]) if folder else ""
        if a and b and os.path.isfile(a) and os.path.isfile(b):
            return a, b
        return "", ""

    cfg = cfg or {}
    manual = str(cfg.get("ffmpeg_path") or "").strip()
    if manual:
        folder = manual if os.path.isdir(manual) else os.path.dirname(manual)
        got = pair(folder)
        if got[0]:
            return got

    env = os.environ.get("FFMPEG_PATH", "").strip()
    if env:
        folder = env if os.path.isdir(env) else os.path.dirname(env)
        got = pair(folder)
        if got[0]:
            return got

    got = pair(str(VENDOR_DIR))
    if got[0]:
        return got

    a = shutil.which("ffmpeg")
    b = shutil.which("ffprobe")
    if a and b:
        return a, b
    return "", ""


def vendor_hint() -> str:
    """给界面/日志用的一句话说明。"""
    return ("把 ffmpeg 静态包解压到 %s（需要同时有 ffmpeg 和 ffprobe）"
            % VENDOR_DIR)
