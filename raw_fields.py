# -*- coding: utf-8 -*-
"""Raw payload archiving and structured field extraction.

Two responsibilities, kept together because both need the same view of a
raw payload:

* ``RawCache`` appends every incoming websocket message and pulled dynamic
  to ``data/cache/raw/raw-YYYY-MM-DD.jsonl`` before parsing, so a parser
  change or an unrecognised field can always be reconstructed later.
* ``extract()`` pulls the informative fields out of a payload into flat
  columns that are stored in SQLite. Pure presentation noise (avatar URLs,
  colour gradients, icon ids) is deliberately left in the cache file only.

The jsonl line format is ``{"t": ms, "r": room_id, "c": cmd, "m": message}``
for websocket messages; dynamics use ``cmd="DYNAMIC"`` and ``r=mid``.
"""
from __future__ import annotations

import json
import os
import threading
import time
import weakref

# --------------------------------------------------------------------------
# Structured field extraction
# --------------------------------------------------------------------------
# Each rule: column name -> (json path, value type).  A path is a dotted list
# of keys/indices, e.g. "info.0.4" or "user_info.medal_info.medal_level".
# ``int`` / ``str`` coerce; ``json`` keeps the original value serialised.
# ``extra`` means: json.loads() the string at that path first (danmaku puts
# most interesting fields inside an escaped JSON blob).

DM_FIELDS = {
    "client_ts": ("info.0.4", "int"),
    "dm_mode": ("info.0.15.extra.mode", "int"),
    "font_size": ("info.0.15.extra.font_size", "int"),
    "dm_color": ("info.0.15.extra.color", "int"),
    "dm_type": ("info.0.15.extra.dm_type", "int"),
    "player_mode": ("info.0.15.extra.player_mode", "int"),
    "show_player_type": ("info.0.15.extra.show_player_type", "int"),
    "send_from_me": ("info.0.15.extra.send_from_me", "int"),
    "dm_id": ("info.0.15.extra.id_str", "str"),
    "user_hash": ("info.0.15.extra.user_hash", "str"),
    "emoticon_unique": ("info.0.15.extra.emoticon_unique", "str"),
    "user_level": ("info.4.3", "str"),
    "name_color": ("info.0.15.user.base.name_color", "str"),
    "is_mystery": ("info.0.15.user.base.is_mystery", "int"),
    "medal_id": ("info.0.15.user.medal.id", "int"),
    "medal_score": ("info.0.15.user.medal.score", "int"),
    "title_id": ("info.0.15.user.title.title_css_id", "int"),
    "is_audited": ("info.0.15.extra.is_audited", "int"),
    "dm_score": ("info.0.15.extra.dm_score", "int"),
    "reply_mid": ("info.0.15.extra.reply_mid", "int"),
    "reply_uname": ("info.0.15.extra.reply_uname", "str"),
    "reply_type": ("info.0.15.extra.reply_type_enum", "int"),
    "animation": ("info.0.15.extra.animation", "json"),
    "direction": ("info.0.15.extra.direction", "int"),
    "voice": ("info.0.15.extra.voice", "json"),
}

SC_FIELDS = {
    "gift_id": ("gift.gift_id", "int"),
    "gift_name": ("gift.gift_name", "str"),
    "gift_num": ("gift.num", "int"),
    "face": ("user_info.face", "str"),
    "rate": ("rate", "int"),
    "dmscore": ("dmscore", "int"),
    "token": ("token", "str"),
    "duration": ("time", "int"),
    "trans_mark": ("trans_mark", "int"),
    "is_ranked": ("is_ranked", "int"),
    "is_mystery": ("user_info.is_mystery", "int"),
    "medal_color": ("medal_info.medal_color", "str"),
    "medal_special": ("medal_info.special", "str"),
    "message_font_color": ("message_font_color", "str"),
    "background_color": ("background_color", "str"),
}

EVENT_FIELDS = {
    "gift_id": ("giftId", "int"),
    "gift_name": ("giftName", "str"),
    "gift_num": ("num", "int"),
    "price": ("price", "int"),
    "coin_type": ("coin_type", "str"),
    "tid": ("tid", "str"),
    "medal_name": ("medal_name", "str"),
    "medal_level": ("medal_level", "int"),
}

DYN_FIELDS = {
    "comment_id": ("basic.comment_id_str", "str"),
    "comment_type": ("basic.comment_type", "int"),
    "rid": ("basic.rid_str", "str"),
    "author_name": ("modules.module_author.name", "str"),
    "author_face": ("modules.module_author.face", "str"),
    "pub_action": ("modules.module_author.pub_action", "str"),
    "pub_location": ("modules.module_author.pub_location_text", "str"),
    "official_desc": ("modules.module_author.official_verify.desc", "str"),
    "views_text": ("modules.module_author.views_text", "str"),
    "orig": ("orig", "json"),
}

FIELDS = {"danmaku": DM_FIELDS, "sc": SC_FIELDS,
          "events": EVENT_FIELDS, "dynamics": DYN_FIELDS}


def _compile(fields):
    """把字段表编译成 (列名, 类型, extra 之前的路径, extra 之后的路径)。

    弹幕有 18 个字段共用同一个 ``info.0.15.extra`` 转义 JSON 串。原来每个
    字段各走一次 ``json.loads``，在手机上 1000 条要 2.1 秒；拆成前后两段
    后，一个 payload 只解一次（1000 条约 120 ms）。``post`` 为 None 表示
    该字段不在 extra 里。
    """
    out = []
    for col, (path, kind) in fields.items():
        parts = tuple(path.split("."))
        if "extra" in parts:
            cut = parts.index("extra")
            # pre 要含 extra 这一段，_walk 才正好停在那个转义串上
            out.append((col, kind, parts[:cut + 1], parts[cut + 1:]))
        else:
            out.append((col, kind, parts, None))
    return tuple(out)


COMPILED = {table: _compile(fields) for table, fields in FIELDS.items()}


def _walk(cur, parts):
    """按路径取子节点；类型对不上就当不存在。"""
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _decode(node):
    """extra 节点可能是转义 JSON 串，也可能已经是对象。"""
    if isinstance(node, str):
        try:
            return json.loads(node)
        except (ValueError, TypeError):
            return None
    if isinstance(node, (dict, list)):
        return node
    return None


def _coerce(value, kind):
    if kind == "int":
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0
    if kind == "str":
        if value is None or isinstance(value, (dict, list)):
            return ""
        return str(value)
    if kind == "json":
        if value in (None, "", {}, []):
            return ""
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (ValueError, TypeError):
            return str(value)
    return value


def extract(table, payload):
    """Return {column: value} for every configured field of ``table``."""
    if not isinstance(payload, (dict, list)):
        return {}
    spec = COMPILED.get(table)
    if not spec:
        return {}
    decoded = {}
    out = {}
    for col, kind, pre, post in spec:
        if post is None:
            val = _walk(payload, pre)
        else:
            if pre not in decoded:
                decoded[pre] = _decode(_walk(payload, pre))
            node = decoded[pre]
            val = _walk(node, post) if node is not None else None
        out[col] = _coerce(val, kind)
    return out


# --------------------------------------------------------------------------
# Raw cache files
# --------------------------------------------------------------------------
# Dynamics are fetched over HTTP outside any RoomWorker; a process-wide
# instance lets that path archive raw frames with the same file layout.
_default_cache = None
_default_lock = threading.Lock()


def configure_default(data_dir, log=None):
    """Set the process-wide cache used by the dynamics fetch path."""
    global _default_cache
    with _default_lock:
        _default_cache = RawCache(data_dir, log=log)


def archive(ts_ms, room_id, cmd, message):
    """Write to the process-wide cache if configured (no-op otherwise)."""
    cache = _default_cache
    if cache is not None:
        cache.write(ts_ms, room_id, cmd, message)


def flush_default():
    cache = _default_cache
    if cache is not None:
        cache.flush()


_live_caches = weakref.WeakSet()


def flush_all():
    """退出前把每个房间缓冲里的尾巴落盘（采集线程是 daemon，来不及自己 flush）。"""
    for cache in list(_live_caches):
        try:
            cache.flush()
        except Exception:
            pass


class RawCache:
    """Append raw messages to per-day jsonl files, buffered and thread-safe.

    Writes happen at the very start of message handling, before parsing, so
    failed / discarded messages are preserved too. Any I/O error is logged and
    swallowed -- archiving must never break capture.
    """

    def __init__(self, data_dir, log=None, flush_every=50, flush_seconds=2.0):
        self.dir = os.path.join(data_dir, "cache", "raw")
        self.log = log or (lambda level, msg: None)
        self.flush_every = max(1, int(flush_every))
        self.flush_seconds = float(flush_seconds)
        self._buf = []
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self._day = ""
        _live_caches.add(self)

    def write(self, ts_ms, room_id, cmd, message):
        line = json.dumps({"t": int(ts_ms), "r": int(room_id or 0),
                           "c": str(cmd or ""), "m": message},
                          ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._buf.append(line)
            if (len(self._buf) >= self.flush_every or
                    time.time() - self._last_flush >= self.flush_seconds):
                self._flush_locked()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self._buf:
            self._last_flush = time.time()
            return
        lines = self._buf
        self._buf = []
        self._last_flush = time.time()
        try:
            day = time.strftime("%Y-%m-%d", time.localtime())
            if day != self._day:
                os.makedirs(self.dir, exist_ok=True)
                self._day = day
            path = os.path.join(self.dir, "raw-%s.jsonl" % day)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except Exception as exc:
            self.log("warn", "原始报文缓存写入失败：%s" % exc)
