"""配置与日志缓冲：从旧 server.py 抽出来，行为保持不变。

`DEFAULTS` 是唯一可信的配置白名单：`Config.save()` 只接受这里出现的键，
所以新增配置项必须先加进 DEFAULTS。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Any

DEFAULTS: dict[str, Any] = {
    "protover": 2,                 # 2=zlib (no extra deps), 3=brotli
    "heartbeat_seconds": 30,
    "stale_seconds": 120,
    "http_timeout": 10,
    "record_danmaku": True,
    "record_sc": True,
    "record_events": True,
    "host": "0.0.0.0",
    "port": 8765,
    "token": "",
    "auto_start": True,
    "dyn_mid": 0,                  # 旧版全局动态 uid，仅在没有采集器时兜底
}

SECRET_KEYS = ("token",)
BOOL_KEYS = ("record_danmaku", "record_sc", "record_events", "auto_start")
INT_BOUNDS = {
    "protover": (2, 3),
    "heartbeat_seconds": (10, 120),
    "stale_seconds": (30, 600),
    "http_timeout": (3, 60),
    "port": (1, 65535),
    "dyn_mid": (0, 999999999),
}
VALID_KINDS = ("danmaku", "sc", "events")

_TRUE = {"1", "true", "yes", "on"}


def as_int(value: Any, default: int = 0) -> int:
    """宽松取整数：接口参数来自 query string，脏数据不该变成 500。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    """query string 里的布尔：接受 1/true/yes/on 与 Python 真值。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    return text in _TRUE


class Config:
    """data/config.json 的读写封装，所有变更先落内存再原子替换文件。"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = dict(DEFAULTS)
        self.load()

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                saved = json.load(handle)
            if isinstance(saved, dict):
                with self.lock:
                    self.data.update({k: v for k, v in saved.items()
                                      if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        return self.get()

    def get(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.data)

    def save(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("配置必须是 JSON 对象")
        clean: dict[str, Any] = {}
        for key, value in patch.items():
            if key not in DEFAULTS:
                continue
            if key in BOOL_KEYS:
                clean[key] = bool(value)
            elif key in INT_BOUNDS:
                low, high = INT_BOUNDS[key]
                try:
                    num = int(value)
                except (TypeError, ValueError):
                    raise ValueError("%s 必须是数字" % key)
                if not low <= num <= high:
                    raise ValueError("%s 需要在 %s–%s 之间" % (key, low, high))
                clean[key] = num
            elif key in ("host", "token"):
                clean[key] = str(value).strip()
            else:
                clean[key] = value
        with self.lock:
            self.data.update(clean)
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return dict(self.data)

    @staticmethod
    def masked(config: dict[str, Any]) -> dict[str, Any]:
        """对外展示时把口令打码，只暴露「有没有设置」。"""
        out = dict(config)
        for key in SECRET_KEYS:
            value = str(out.get(key) or "")
            out[key + "_set"] = bool(value)
            if value:
                out[key] = (value[:2] + "…" + value[-2:]) if len(value) > 6 else "****"
        return out


class LogBuffer:
    """环形日志：既给页面 `/api/logs` 用，也把同一条写到 stdout。"""

    def __init__(self, capacity: int = 1000):
        self.capacity = capacity
        self.lock = threading.RLock()
        self.items: list[dict[str, Any]] = []
        self._logger = logging.getLogger("livecapture")

    def add(self, level: str, message: Any) -> None:
        text = str(message)
        with self.lock:
            self.items.append({"ts": int(time.time() * 1000),
                               "level": level, "msg": text})
            if len(self.items) > self.capacity:
                self.items = self.items[-self.capacity:]
        self._logger.log(
            {"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO),
            text)

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.items[-max(1, min(1000, as_int(limit, 100))):])


def lan_ip() -> str:
    """局域网地址：连一个公网 IP 拿本机出口，不真的发包。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()
