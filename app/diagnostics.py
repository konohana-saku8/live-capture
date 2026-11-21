"""运行期诊断：给日志页提供「服务现在是什么状态」的硬信息。

日志只回答“发生过什么”，诊断回答“现在健康吗”——线程还在不在、磁盘还剩
多少、数据库涨到多大。手机上没有 top / df，页面上能直接看到这些最省事。
全部只读，任何一项取不到就退成 0 / 空，不影响其它字段。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

import sysinfo

_FFMPEG_CACHE: dict[str, Any] | None = None


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def rss_bytes() -> int:
    """/proc/self/statm 第二列是常驻页数；Windows 上取不到就返回 0。"""
    parts = _read("/proc/self/statm").split()
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1]) * (os.sysconf("SC_PAGE_SIZE")
                                if hasattr(os, "sysconf") else 4096)
    except (ValueError, OSError):
        return 0


def ffmpeg_info() -> dict[str, Any]:
    """ffmpeg 路径与版本，只探测一次（remux 全靠它，报错先看这里）。"""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None:
        return _FFMPEG_CACHE
    path = shutil.which("ffmpeg") or ""
    version = ""
    if path:
        try:
            out = subprocess.run([path, "-version"], capture_output=True,
                                 text=True, timeout=5)
            version = (out.stdout or "").split("\n", 1)[0].strip()[:120]
        except (OSError, subprocess.SubprocessError):
            version = "无法执行"
    _FFMPEG_CACHE = {"path": path, "version": version, "ok": bool(path and version
                                                                 and version != "无法执行")}
    return _FFMPEG_CACHE


def threads_snapshot(runtime) -> dict[str, Any]:
    """线程清单 + 该活的采集线程是否还在。

    `missing` 非空就是最有价值的信号：某个房间的采集线程已经死了，
    但它既没抛异常也没写日志的时候，只有这里能看出来。
    waitress 的 16 个工作线程单独计数，不然清单里全是它。
    """
    alive = threading.enumerate()
    business = [t for t in alive if not t.name.startswith("waitress")]
    items = [{"name": t.name, "alive": t.is_alive(), "daemon": t.daemon}
             for t in sorted(business, key=lambda x: x.name)]
    expected: list[str] = []
    missing: list[str] = []
    try:
        for worker in (runtime.manager.workers or {}).values():
            name = "capture-%s" % getattr(worker, "room_id", "?")
            expected.append(name)
            thread = getattr(worker, "thread", None)
            if thread is not None and not thread.is_alive():
                missing.append(name)
    except Exception:
        pass
    return {"count": len(alive), "items": items,
            "pool": len(alive) - len(business),
            "expected_capture": expected, "missing": missing}


def snapshot(runtime) -> dict[str, Any]:
    data_dir = runtime.data_dir
    db_path = os.path.join(data_dir, "capture.db")
    logs = runtime.logs.stats()
    return {
        "ts": int(time.time() * 1000),
        "pid": os.getpid(),
        "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
        "platform": "%s %s" % (platform.system(), platform.machine()),
        "uptime_ms": int(time.time() * 1000) - int(getattr(runtime, "started_at", 0)),
        "threads": threads_snapshot(runtime),
        "disk": sysinfo.disk(data_dir),
        "memory": sysinfo.memory(),
        "process": {"rss": rss_bytes()},
        "db": {"path": db_path, "bytes": _file_size(db_path),
               "wal_bytes": _file_size(db_path + "-wal"),
               "shm_bytes": _file_size(db_path + "-shm")},
        "logs": {"buffered": logs["buffered"], "counts": logs["counts"],
                 "last_error_at": logs["last_error_at"],
                 "journal_bytes": logs["journal_bytes"],
                 "journal": logs["journal"]},
        "ffmpeg": ffmpeg_info(),
        "websockets": bool(getattr(runtime, "ws_ok", True)),
    }
