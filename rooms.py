# -*- coding: utf-8 -*-
"""直播间列表：网页里增删，持久化在 data/rooms.json。

采集状态不在文件里（由 CaptureManager 持有），重启服务后所有直播间默认未采集；
需要的话在配置页打开「服务启动时自动采集」。

每间直播间可带独立的采集参数（PARAM_KEYS），启动采集时覆盖全局默认值。
"""
from __future__ import annotations

import json
import os
import threading
import time

# 每间直播间可覆盖的采集参数（服务级配置 host/port/token 等不在此列）
PARAM_KEYS = ("protover", "heartbeat_seconds", "stale_seconds",
              "record_danmaku", "record_sc", "record_events")


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
        given = str(label or "").strip()
        display = given or (title[:24] if title else "房间 %s" % rid)
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
        """合并保存某间直播间的采集参数；房间不存在返回 None。"""
        rid = _as_int(room_id)
        with self.lock:
            for item in self.items:
                if item["id"] == rid:
                    params = item.setdefault("params", {})
                    for key, value in (patch or {}).items():
                        if key in PARAM_KEYS:
                            params[key] = value
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
