"""配置与日志缓冲：从旧 server.py 抽出来，行为保持不变。

`DEFAULTS` 是唯一可信的配置白名单：`Config.save()` 只接受这里出现的键，
所以新增配置项必须先加进 DEFAULTS。
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
import threading
import time
import traceback
from typing import Any

DEFAULTS: dict[str, Any] = {
    "protover": 2,                 # 2=zlib (no extra deps), 3=brotli
    "heartbeat_seconds": 30,
    "stale_seconds": 120,
    "http_timeout": 10,
    "record_danmaku": True,
    "record_sc": True,
    # 事件总开关；下面按事件细类再拆，均可按采集器单独覆盖
    "record_events": True,
    "record_guard": True,
    "record_gift": True,
    "record_red_pocket": True,
    # 入场事件量大且噪音多，默认不记；关注 / 分享等其他互动仍然记
    "record_enter": False,
    "record_interact": True,
    "host": "0.0.0.0",
    "port": 8765,
    "token": "",
    "auto_start": True,
    "dyn_mid": 0,                  # 旧版全局动态 uid，仅在没有采集器时兜底
    "keep_flv": True,              # 合并稳定前保留 FLV 原片作兜底
    # 飞书自定义机器人：webhook 为空 = 不推送
    "feishu_webhook": "",
    "feishu_secret": "",           # 机器人开了「签名校验」时填，否则留空
    "notify_live_seconds": 60,     # 开播探测间隔
    "notify_dyn_seconds": 300,     # 动态轮询间隔
}

SECRET_KEYS = ("token", "feishu_webhook", "feishu_secret")
BOOL_KEYS = ("record_danmaku", "record_sc", "record_events", "record_guard",
             "record_gift", "record_red_pocket", "record_enter",
             "record_interact", "auto_record", "auto_start", "keep_flv")
# 采集内容开关：采集器清单里返回「全局默认 + 该采集器覆盖」后的有效值
CONTENT_KEYS = ("record_danmaku", "record_sc", "record_events", "record_guard",
                "record_gift", "record_red_pocket", "record_enter",
                "record_interact")
INT_BOUNDS = {
    "protover": (2, 3),
    "heartbeat_seconds": (10, 120),
    "stale_seconds": (30, 600),
    "http_timeout": (3, 60),
    "port": (1, 65535),
    "dyn_mid": (0, 999999999),
    "record_qn": (80, 10000),
    "notify_live_seconds": (20, 3600),
    "notify_dyn_seconds": (60, 3600),
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
            elif key in ("host", "token", "feishu_webhook", "feishu_secret"):
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


LEVELS = ("error", "warn", "info")
_LEVEL_MAP = {"error": logging.ERROR, "warn": logging.WARNING, "info": logging.INFO}
_THIS_FILE = os.path.abspath(__file__)
TB_LIMIT = 8000

# 噪音：确实会发生，但排查时毫无信息量，进页面只会把真正的错误挤上去。
NOISE = ("Client disconnected while serving",
         "Serving on http://",
         "WebSocket closed")


class NoiseFilter(logging.Filter):
    """丢掉纯噪音的第三方日志行（播放器每次拖进度条都会断一次连接）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not any(x in message for x in NOISE)


def format_exc(exc: Any) -> str:
    """把异常格式化成完整 traceback（含调用栈、文件行号）。"""
    try:
        if isinstance(exc, BaseException):
            text = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
        else:
            text = str(exc or "")
    except Exception:
        return ""
    return text.strip()[:TB_LIMIT]


def caller_src(depth: int = 2) -> str:
    """调用方的 `文件名:行号`，跳过本文件内部的包装层。

    日志里带行号，看的人才知道是哪段代码在报错，不用全文搜字符串。
    """
    try:
        frame = sys._getframe(depth)
    except ValueError:
        return ""
    while frame is not None:
        name = os.path.abspath(frame.f_code.co_filename)
        if name != _THIS_FILE:
            return "%s:%d" % (os.path.basename(name), frame.f_lineno)
        frame = frame.f_back
    return ""


class _RootLogHandler(logging.Handler):
    """把 root / waitress / Flask 的日志也收进页面，不重复记 livecapture。

    第三方 logger 的 record 常带 exc_info（Flask 处理 500 就是这样），只取
    getMessage() 会把 traceback 丢掉——而那正是排查时最需要的部分。
    """

    def __init__(self, buffer: "LogBuffer"):
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "livecapture":
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        tb = ""
        if record.exc_info:
            try:
                tb = "".join(traceback.format_exception(*record.exc_info))
            except Exception:
                tb = ""
        level = {"warning": "warn", "critical": "error"}.get(
            record.levelname.lower(), record.levelname.lower())
        if level not in LEVELS:
            level = "info"
        if level == "info" and any(x in message for x in NOISE):
            return
        self.buffer.append(level, message,
                           int((record.created or time.time()) * 1000),
                           src="%s:%d" % (record.name, record.lineno), tb=tb)


class LogBuffer:
    """环形日志：页面 `/api/logs`、root 日志捕获，同时写到 stdout / 文件。

    比旧版多三件事，都是为了“出事时能定位”：
    - 每条带 `src`（产生日志的 文件:行号）与 `tb`（error 的完整 traceback）；
    - 连续重复的同一行只留一条并累计 `n`，刷屏的 4xx 不会挤掉别的错误；
    - warn/error 额外追加到 `data/logs/journal.jsonl`，服务重启后回填到页面，
      不用翻下载的 service.log 才知道刚才为什么挂了。
    """

    JOURNAL_MAX = 1_000_000          # 单日文件约 1MB，超了重写成最后 800 条
    JOURNAL_KEEP = 800
    DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    def __init__(self, capacity: int = 4000):
        self.capacity = capacity
        self.lock = threading.RLock()
        self.items: list[dict[str, Any]] = []
        self.journal_path = ""       # 兼容旧版：单文件路径
        self.journal_dir = ""        # 按天模式：日志目录
        self.dropped = 0             # 被噪音规则丢掉的条数
        self._journal_size = 0
        self._journal_day = ""
        self._journal_ok = True
        self._logger = logging.getLogger("livecapture")
        self._root_handler: logging.Handler | None = None

    # ---------- 写入 ----------

    def bind_journal(self, path: str) -> None:
        """绑定日志目录，按天写 ``journal-YYYY-MM-DD.jsonl``。

        传旧的单文件路径（journal.jsonl）时取其目录，并保留原路径用于
        兼容读取——手机上已有历史文件，不能因为升级就读不到。
        """
        if path.lower().endswith(".jsonl"):
            self.journal_path = path
            path = os.path.dirname(os.path.abspath(path))
        self.journal_dir = os.path.abspath(path)
        try:
            os.makedirs(self.journal_dir, exist_ok=True)
        except OSError:
            self._journal_ok = False
        self._journal_day = self._local_day()
        self._journal_size = self._day_size(self._journal_day)

    @staticmethod
    def _local_day(ts: int | None = None) -> str:
        """用本地时区把毫秒时间戳转成 YYYY-MM-DD。"""
        return time.strftime("%Y-%m-%d", time.localtime((ts or time.time() * 1000) / 1000))

    def _day_path(self, day: str) -> str:
        return os.path.join(self.journal_dir, "journal-%s.jsonl" % day)

    def _day_size(self, day: str) -> int:
        try:
            return os.path.getsize(self._day_path(day))
        except OSError:
            return 0

    def days(self) -> list[str]:
        """列出有日志的日期（新到旧），含旧版单文件里的记录。"""
        found: set[str] = set()
        if self.journal_dir:
            try:
                for name in os.listdir(self.journal_dir):
                    m = re.fullmatch(r"journal-(\d{4}-\d{2}-\d{2})\.jsonl", name)
                    if m:
                        found.add(m.group(1))
            except OSError:
                pass
        found.update(self._legacy_days())
        return sorted(found, reverse=True)

    def _legacy_days(self) -> set[str]:
        """旧 journal.jsonl 里混着多天的 warn/error，逐条按时间戳归类。"""
        days: set[str] = set()
        if not self.journal_path or not os.path.isfile(self.journal_path):
            return days
        try:
            with open(self.journal_path, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return days
        for line in lines:
            row = self._parse_row(line)
            if row and row["ts"]:
                days.add(self._local_day(row["ts"]))
        return days

    def read_day(self, day: str, limit: int = 2000) -> list[dict[str, Any]]:
        """读某一天的完整日志（所有级别），新到旧按时间正序返回。"""
        if not self.DAY_RE.match(day or ""):
            return []
        paths = []
        if self.journal_path and day in self._legacy_days():
            paths.append(self.journal_path)   # 旧单文件里那天的 warn/error
        paths.append(self._day_path(day))
        rows: list[dict[str, Any]] = []
        for path in paths:
            try:
                with open(path, encoding="utf-8") as handle:
                    lines = handle.readlines()
            except OSError:
                continue
            # 单日文件超限时 _journal_rotate 只保留尾部，这里再截一次 limit
            for line in lines[-max(1, limit):]:
                row = self._parse_row(line)
                if row:
                    # 旧单文件里可能混着别的天，按日期再过一遍
                    if path == self.journal_path and self._local_day(row["ts"]) != day:
                        continue
                    rows.append(row)
        rows.sort(key=lambda x: x["ts"])
        return rows[-max(1, limit):]

    @staticmethod
    def _parse_row(line: str) -> dict[str, Any] | None:
        try:
            row = json.loads(line)
        except ValueError:
            return None
        if not isinstance(row, dict) or not row.get("msg"):
            return None
        return {"ts": as_int(row.get("ts")),
                "level": str(row.get("level") or "info"),
                "msg": str(row.get("msg")),
                "src": str(row.get("src") or ""),
                "tb": str(row.get("tb") or ""),
                "n": as_int(row.get("n"), 1) or 1}

    def append(self, level: str, message: Any, ts: int | None = None,
               src: str = "", tb: str = "", merge: bool = True) -> None:
        text = str(message)
        stamp = int(ts or time.time() * 1000)
        with self.lock:
            last = self.items[-1] if self.items else None
            if (merge and last and last["level"] == level and last["msg"] == text
                    and last.get("src") == src):
                last["n"] = int(last.get("n", 1)) + 1
                last["ts"] = stamp
                return
            row = {"ts": stamp, "level": level, "msg": text,
                   "src": src, "tb": tb or "", "n": 1}
            self.items.append(row)
            if len(self.items) > self.capacity:
                self.items = self.items[-self.capacity:]
            self._journal_write(row)

    def add(self, level: str, message: Any, exc: Any = None,
            src: str = "") -> None:
        """记一条日志。`exc` 传异常实例时自动带上完整 traceback。"""
        text = str(message)
        tb = format_exc(exc) if exc is not None else ""
        self.append(level, text, src=src or caller_src(2), tb=tb)
        self._logger.log(_LEVEL_MAP.get(level, logging.INFO), text,
                         exc_info=exc if isinstance(exc, BaseException) else None,
                         stacklevel=2)

    def error(self, message: Any, exc: Any = None) -> None:
        self.add("error", message, exc=exc)

    def warn(self, message: Any, exc: Any = None) -> None:
        self.add("warn", message, exc=exc)

    def info(self, message: Any) -> None:
        self.add("info", message)

    # ---------- 持久化 ----------

    def _journal_write(self, row: dict[str, Any]) -> None:
        if not self.journal_dir or not self._journal_ok:
            return
        # 所有级别都落盘：按天查看要能看到当天发生了什么，只留 warn/error
        # 会让历史天几乎空白。info 量大，靠单日 1MB 上限兜住。
        day = self._local_day(row["ts"])
        path = self._day_path(day)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        try:
            if day != self._journal_day:
                self._journal_day = day
                self._journal_size = self._day_size(day)
            if self._journal_size + len(line) > self.JOURNAL_MAX:
                self._journal_rotate(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
            self._journal_size += len(line)
        except OSError:
            # 磁盘满 / 无权限：只关掉写盘，内存日志照常，不要递归报错
            self._journal_ok = False

    def _journal_rotate(self, path: str) -> None:
        keep: list[str] = []
        try:
            with open(path, encoding="utf-8") as handle:
                keep = handle.readlines()[-self.JOURNAL_KEEP:]
        except OSError:
            keep = []
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(keep)
        self._journal_size = sum(len(x) for x in keep)

    def replay(self, limit: int = 300) -> int:
        """服务启动时把上次的日志读回内存，重启不丢错误现场。

        只读今天的（按本地日期），历史天从页面按天查看即可；旧版单文件
        journal.jsonl 仍作为补充来源，升级后不会丢掉旧错误。
        """
        today = self._local_day()
        paths = [self._day_path(today)]
        if self.journal_path and os.path.isfile(self.journal_path):
            paths.append(self.journal_path)
        restored: list[dict[str, Any]] = []
        for path in paths:
            try:
                with open(path, encoding="utf-8") as handle:
                    lines = handle.readlines()
            except OSError:
                continue
            for line in lines[-max(1, limit):]:
                row = self._parse_row(line)
                if not row:
                    continue
                if path == self.journal_path and self._local_day(row["ts"]) != today:
                    continue
                row["replay"] = True
                restored.append(row)
        # 同一条（ts + 文本）可能在新旧文件里各有一份，去重
        seen: set[tuple[int, str, str]] = set()
        unique: list[dict[str, Any]] = []
        for row in restored:
            key = (row["ts"], row["msg"], row["tb"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        restored = unique
        if not restored:
            return 0
        with self.lock:
            self.items = restored + self.items
            if len(self.items) > self.capacity:
                self.items = self.items[-self.capacity:]
        return len(restored)

    # ---------- 读取 ----------

    def install_root(self) -> None:
        """把 waitress / Flask / 第三方日志收进同一个环形缓冲。"""
        if self._root_handler is not None:
            return
        handler = _RootLogHandler(self)
        logging.getLogger().addHandler(handler)
        self._root_handler = handler

    def close(self) -> None:
        if self._root_handler is None:
            return
        logging.getLogger().removeHandler(self._root_handler)
        self._root_handler = None

    def tail(self, limit: int = 200, level: str = "", q: str = "",
             since: int = 0) -> list[dict[str, Any]]:
        want = {x.strip() for x in str(level or "").split(",") if x.strip()}
        needle = str(q or "").strip().lower()
        limit = max(1, min(self.capacity, as_int(limit, 200) or 200))
        with self.lock:
            items = self.items
            if want and "all" not in want:
                items = [x for x in items if x["level"] in want]
            if since:
                items = [x for x in items if x["ts"] > since]
            if needle:
                items = [x for x in items
                         if needle in x["msg"].lower()
                         or needle in (x.get("tb") or "").lower()
                         or needle in (x.get("src") or "").lower()]
            return [dict(x) for x in items[-limit:]]

    def stats(self) -> dict[str, Any]:
        with self.lock:
            counts = {lv: 0 for lv in LEVELS}
            last_error_at = 0
            for row in self.items:
                counts[row["level"]] = counts.get(row["level"], 0) + 1
                if row["level"] == "error" and row["ts"] > last_error_at:
                    last_error_at = row["ts"]
            return {"counts": counts, "buffered": len(self.items),
                    "capacity": self.capacity, "last_error_at": last_error_at,
                    "journal": self._day_path(self._journal_day) if self.journal_dir
                               else self.journal_path,
                    "journal_bytes": self._journal_size,
                    "days": self.days()}


def install_excepthooks(buffer: LogBuffer) -> None:
    """线程/主线程里没被 catch 的异常也要进日志，带 traceback。

    采集线程、录制线程、定时线程全是后台线程，任何一个悄悄死掉在旧版里
    只会在终端打一行——手机上看不到，页面更看不到。
    """
    prev_thread = threading.excepthook

    def _thread_hook(args) -> None:
        name = getattr(args.thread, "name", "?") if args.thread else "?"
        buffer.add("error", "线程 %s 未捕获异常：%s: %s" %
                   (name, getattr(args.exc_type, "__name__", "?"),
                    args.exc_value),
                   exc=args.exc_value)
        try:
            prev_thread(args)
        except Exception:
            pass

    threading.excepthook = _thread_hook

    prev_main = sys.excepthook

    def _main_hook(exc_type, exc_value, exc_tb) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            buffer.add("error", "主线程未捕获异常：%s: %s" %
                       (exc_type.__name__, exc_value), exc=exc_value)
        prev_main(exc_type, exc_value, exc_tb)

    sys.excepthook = _main_hook


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
