# -*- coding: utf-8 -*-
"""采集器列表：网页里增删，持久化在 data/rooms.json。

一个采集器对应一位 up 主：条目里既存 up 主 uid / 昵称 / 头像，也存解析出来的
直播间号，弹幕与 SC 仍按房间号（id）分表存放，历史数据不用迁移。

采集状态不在文件里（由 CaptureManager 持有），重启服务后所有采集器默认未采集；
需要的话在配置页打开「服务启动时自动采集」。

每个采集器可以选择采集哪些数据（collect_live / collect_dynamics）。
"""
from __future__ import annotations

import json
import os
import threading
import time

# 每间直播间可覆盖的采集参数（服务级配置 host/port/token 等不在此列）
PARAM_KEYS = ("protover", "heartbeat_seconds", "stale_seconds",
              "record_danmaku", "record_sc", "record_events", "record_guard",
              "record_gift", "record_red_pocket", "record_enter",
              "record_interact", "auto_record", "record_qn")
# 采集器可以选择采哪些数据
COLLECT_KEYS = ("collect_live", "collect_dynamics", "live_only")
# 动态定时抓取：按 up 主各存一份（默认关闭），dyn_last_ts 是内部记录的上次成功时间
SCHED_KEYS = ("dyn_auto", "dyn_hour", "dyn_minute", "dyn_last_ts")
# 推送开关：按 up 主各存一份，默认开（全局 webhook 为空时一律不推）
NOTIFY_KEYS = ("notify_live", "notify_dynamic")


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# 本地测试源（test_source.py 造的假房间）没有 up 主，uid 恒为 0。前端四处、
# 动态定时抓取、动态推送都按这个判据认它；后端任何要连 B 站的入口都先问一句
# is_test_room()，免得拿假房间去调接口刷日志。
def is_test_room(room):
    return _as_int((room or {}).get("uid")) <= 0


class RoomStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.items = []
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, list):
                seen = set()
                clean = []
                for x in items:
                    rid = _as_int(x.get("id") if isinstance(x, dict) else x)
                    if rid > 0 and rid not in seen:
                        seen.add(rid)
                        clean.append(self._normalize(x if isinstance(x, dict)
                                                     else {"id": rid}))
                with self.lock:
                    self.items = clean
        except (OSError, ValueError):
            pass

    @staticmethod
    def _normalize(item):
        rid = _as_int(item.get("id"))
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        return {"id": rid,
                "label": str(item.get("label") or ("房间 %s" % rid))[:80],
                "title": str(item.get("title") or "")[:200],
                "uid": _as_int(item.get("uid")),
                "short_id": _as_int(item.get("short_id")),
                "live_status": _as_int(item.get("live_status")),
                "live_time": _as_int(item.get("live_time")),
                "online": _as_int(item.get("online")),
                "uname": str(item.get("uname") or "")[:80],
                "face": str(item.get("face") or "")[:300],
                "collect_live": item.get("collect_live", True) is not False,
                "collect_dynamics": item.get("collect_dynamics", True) is not False,
                # 只在开播期间连弹幕服务器；没开播就只定时探状态
                "live_only": item.get("live_only", False) is True,
                # 定时抓取动态：每位 up 主单独设置，默认关闭
                "dyn_auto": item.get("dyn_auto", False) is True,
                "dyn_hour": min(23, max(0, _as_int(item.get("dyn_hour"), 8))),
                "dyn_minute": min(59, max(0, _as_int(item.get("dyn_minute"), 0))),
                "dyn_last_ts": _as_int(item.get("dyn_last_ts")),
                "notify_live": item.get("notify_live", True) is not False,
                "notify_dynamic": item.get("notify_dynamic", True) is not False,
                "params": {k: params[k] for k in PARAM_KEYS if k in params},
                "created_at": _as_int(item.get("created_at"))}

    def _save(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"items": self.items}, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def list(self):
        with self.lock:
            return {"ok": True, "items": [dict(x) for x in self.items]}

    def get(self, room_id):
        rid = _as_int(room_id)
        with self.lock:
            return next((dict(x) for x in self.items if x["id"] == rid), None)

    def add(self, room_id, label="", brief=None):
        rid = _as_int(room_id)
        if rid <= 0:
            raise ValueError("房间号必须是正整数")
        brief = brief or {}
        title = str(brief.get("title") or "")
        uname = str(brief.get("uname") or "")
        given = str(label or "").strip()
        display = given or (uname[:24] or title[:24] or "房间 %s" % rid)
        with self.lock:
            for item in self.items:
                if item["id"] == rid:
                    if given:
                        item["label"] = given[:80]
                    if title:
                        item["title"] = title[:200]
                    if brief:
                        self._apply_brief(item, brief)
                    self._save()
                    return dict(item), False
            item = self._normalize({
                "id": rid, "label": display, "title": title,
                "uid": brief.get("uid"), "short_id": brief.get("short_id"),
                "live_status": brief.get("live_status"),
                "live_time": brief.get("live_time"),
                "online": brief.get("online"),
                "uname": brief.get("uname"), "face": brief.get("face"),
                "created_at": int(time.time() * 1000)})
            self.items.append(item)
            self._save()
            return dict(item), True

    def find_by_uid(self, uid):
        uid = _as_int(uid)
        with self.lock:
            return next((dict(x) for x in self.items if uid and x["uid"] == uid),
                        None)

    def collect_targets(self, key="collect_dynamics"):
        """返回打开了某个采集开关的采集器（用于按 up 主拉动态）。"""
        with self.lock:
            return [dict(x) for x in self.items if x.get(key, True)]

    def auto_dyn_rooms(self):
        """打开了「采集动态 + 每天定时拉取」的采集器，各带自己的时/分。"""
        with self.lock:
            return [dict(x) for x in self.items
                    if x.get("collect_dynamics", True) and x.get("dyn_auto")]

    @staticmethod
    def _apply_brief(item, brief):
        for key in ("uid", "short_id", "live_status", "live_time", "online"):
            item[key] = _as_int(brief.get(key))
        if brief.get("title"):
            item["title"] = str(brief["title"])[:200]
        if brief.get("uname"):
            item["uname"] = str(brief["uname"])[:80]
        if brief.get("face"):
            item["face"] = str(brief["face"])[:300]

    def settings(self, room_id, patch):
        """合并保存某个采集器的开关与采集参数；不存在返回 None。"""
        rid = _as_int(room_id)
        with self.lock:
            for item in self.items:
                if item["id"] == rid:
                    params = item.setdefault("params", {})
                    for key, value in (patch or {}).items():
                        if key in PARAM_KEYS:
                            params[key] = value
                        elif key in COLLECT_KEYS:
                            item[key] = bool(value)
                        elif key in NOTIFY_KEYS:
                            item[key] = bool(value)
                        elif key == "dyn_auto":
                            item["dyn_auto"] = bool(value)
                        elif key == "dyn_hour":
                            item["dyn_hour"] = min(23, max(0, _as_int(value, 8)))
                        elif key == "dyn_minute":
                            item["dyn_minute"] = min(59,
                                                     max(0, _as_int(value, 0)))
                        elif key == "dyn_last_ts":
                            item["dyn_last_ts"] = _as_int(value)
                    self._save()
                    return dict(item)
        return None

    def refresh(self, room_id, brief):
        rid = _as_int(room_id)
        with self.lock:
            for item in self.items:
                if item["id"] == rid:
                    self._apply_brief(item, brief or {})
                    self._save()
                    return dict(item)
        return None

    def remove(self, room_id):
        rid = _as_int(room_id)
        with self.lock:
            before = len(self.items)
            self.items = [x for x in self.items if x["id"] != rid]
            changed = before != len(self.items)
            if changed:
                self._save()
            return changed
