# -*- coding: utf-8 -*-
"""Lightweight device status for the dashboard (no third-party deps).

Reads /proc on Linux/Android(Termux) where available and degrades gracefully
elsewhere (Windows dev boxes just report what it can). Everything is
best-effort: a missing file yields 0 / "" rather than raising.
"""
from __future__ import annotations

import os
import shutil
import socket
import time


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def cpu_times():
    """Return (total_ticks, idle_ticks) from /proc/stat, or (0, 0)."""
    line = _read("/proc/stat").split("\n", 1)[0]
    if not line.startswith("cpu"):
        return 0, 0
    parts = line.split()[1:8]
    try:
        vals = [int(x) for x in parts]
    except ValueError:
        return 0, 0
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def proc_cpu_ticks():
    """本进程累计 CPU ticks（utime + stime），读 /proc/self/stat。

    系统级 /proc/stat 在 Android 上对普通应用可能是拒绝访问的，这时候只能
    退到「本服务自己用了多少 CPU」——对这台只跑采集服务的机器反而更直观。
    """
    text = _read("/proc/self/stat")
    if not text:
        return 0
    try:
        # comm 字段可能带空格和括号，从最后一个 ")" 之后开始数
        rest = text[text.rindex(")") + 1:].split()
        return int(rest[11]) + int(rest[12])
    except (ValueError, IndexError):
        return 0


class CpuSampler:
    """CPU% needs two samples; keep the previous one between calls."""

    def __init__(self):
        self._prev = cpu_times()
        self._prev_proc = (proc_cpu_ticks(), time.monotonic())
        self._last_pct = 0.0

    def usage(self):
        """返回 {percent, scope}：scope=system 是全机占用，process 是本服务。"""
        total, idle = cpu_times()
        pt, pi = self._prev
        self._prev = (total, idle)
        dt, di = total - pt, idle - pi
        if dt > 0:
            return {"percent": round(max(0.0, min(100.0, (dt - di) * 100.0 / dt)), 1),
                    "scope": "system"}
        # 系统级读数拿不到（Android 常见）：改报本进程的单核占用
        ticks, now = proc_cpu_ticks(), time.monotonic()
        prev_ticks, prev_now = self._prev_proc
        span = now - prev_now
        if span < 0.5:
            # 两次请求挨得太近（网页 4 秒轮询撞上手动刷新），除出来的数没意义，
            # 保留上一次的读数，也不推进基准点。
            return {"percent": self._last_pct, "scope": "process"}
        self._prev_proc = (ticks, now)
        if ticks < prev_ticks:
            return {"percent": 0.0, "scope": "process"}
        pct = (ticks - prev_ticks) / 100.0 / span * 100.0  # USER_HZ 恒为 100
        self._last_pct = round(max(0.0, min(100.0, pct)), 1)
        return {"percent": self._last_pct, "scope": "process"}


def memory():
    """Return {total, used, percent} in bytes from /proc/meminfo."""
    text = _read("/proc/meminfo")
    if not text:
        return {"total": 0, "used": 0, "percent": 0.0}
    info = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            info[key.strip()] = int(parts[0]) * 1024  # kB -> bytes
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable")
    if avail is None:
        avail = (info.get("MemFree", 0) + info.get("Buffers", 0) +
                 info.get("Cached", 0))
    used = max(0, total - avail)
    pct = round(used * 100.0 / total, 1) if total else 0.0
    return {"total": total, "used": used, "percent": pct}


def disk(path):
    try:
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "used": usage.used,
                "free": usage.free,
                "percent": round(usage.used * 100.0 / usage.total, 1)
                if usage.total else 0.0}
    except OSError:
        return {"total": 0, "used": 0, "free": 0, "percent": 0.0}


def loadavg():
    try:
        one, five, fifteen = os.getloadavg()
        return {"1": round(one, 2), "5": round(five, 2),
                "15": round(fifteen, 2)}
    except (OSError, AttributeError):
        return {"1": 0, "5": 0, "15": 0}


def network():
    """Report LAN IP + whether the default route looks reachable."""
    ip = "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1.0)
        sock.connect(("223.5.5.5", 80))
        ip = sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()
    # 粗略判断联网：尝试解析一个公共域名
    online = False
    try:
        socket.gethostbyname("api.bilibili.com")
        online = True
    except OSError:
        online = False
    return {"lan_ip": ip, "online": online}


def uptime():
    text = _read("/proc/uptime").split()
    try:
        return int(float(text[0])) if text else 0
    except ValueError:
        return 0


def snapshot(data_dir, cpu_sampler=None, app_started_at=0):
    """Assemble the full device status dict for the dashboard."""
    cpu_sampler = cpu_sampler or CpuSampler()
    mem = memory()
    db = os.path.join(data_dir, "capture.db")
    db_size = 0
    if os.path.isfile(db):
        try:
            db_size = os.path.getsize(db)
        except OSError:
            db_size = 0
    cpu = cpu_sampler.usage()
    return {
        "ts": int(time.time() * 1000),
        "cpu": {"percent": cpu["percent"], "scope": cpu["scope"],
                "cores": os.cpu_count() or 0},
        "memory": mem,
        "disk": disk(data_dir),
        "load": loadavg(),
        "network": network(),
        "uptime": uptime(),
        "db_size": db_size,
        "app_uptime": (int(time.time() * 1000) - app_started_at)
        if app_started_at else 0,
    }
