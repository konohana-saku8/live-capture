# -*- coding: utf-8 -*-
"""Cookie 账号：上传 CSV、增删、启用，供采集连接时带上登录态。

兼容 danmaku-it (旧 sc_collector) 的两种 cookie 导出：
  - Netscape 7 列，Tab 分隔：domain flag path secure expiry name value
  - Chrome 扩展 13 列，Tab 分隔：name value domain path expiry ...
也接受常见的「name,value」两列逗号 CSV。判定规则与 danmaku-it/sender.py 一致：
首列含 "." 且列数 >= 7 视为 Netscape，否则取前两列。

凭证只写在 data/accounts.json（data/ 已被 .gitignore 忽略），不写进日志；
对外 API 只返回 SESSDATA 掩码，不返回任何 cookie 明文。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import threading
import time
import urllib.request

MAX_CSV_CHARS = 256 * 1024
LOGIN_KEY = "SESSDATA"
PROFILE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def fetch_profile(cookies, timeout=6):
    """取账号昵称与头像（header 展示用）。

    优先用 cookie 里的 uid 调 card 公开接口 —— 不依赖登录态，cookie 过期也能
    拿到头像昵称；拿不到 uid 才退回 nav（需要有效 SESSDATA）。失败返回 None。
    selftest 里会被替换成假实现，保持零联网。
    """
    cookies = cookies or {}
    uid = _uid(cookies)
    if uid:
        try:
            req = urllib.request.Request(
                "https://api.bilibili.com/x/web-interface/card?mid=%d" % uid,
                headers={"User-Agent": PROFILE_UA,
                         "Referer": "https://www.bilibili.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            card = (data.get("data") or {}).get("card") or {}
            if card:
                return {"uid": uid, "uname": str(card.get("name") or ""),
                        "face": str(card.get("face") or "")}
        except Exception:
            pass
    keep = {k: v for k, v in cookies.items()
            if k in ("SESSDATA", "DedeUserID", "buvid3") and v}
    if not keep.get("SESSDATA"):
        return None
    cookie = "; ".join("%s=%s" % kv for kv in keep.items())
    req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"User-Agent": PROFILE_UA,
                 "Referer": "https://www.bilibili.com/",
                 "Cookie": cookie})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        return None
    payload = data.get("data") or {}
    try:
        uid = int(payload.get("mid") or 0)
    except (TypeError, ValueError):
        uid = 0
    return {"uid": uid, "uname": str(payload.get("uname") or ""),
            "face": str(payload.get("face") or "")}


def parse_cookie_csv(text):
    """把上传的 cookie 文本解析成 name -> value；找不到任何条目时抛 ValueError。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("文件内容为空")
    if len(text) > MAX_CSV_CHARS:
        raise ValueError("文件过大（上限 256 KB）")
    out = {}
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        # Netscape 的 HttpOnly 行有前缀，去掉后照常解析；其余 # 开头是注释
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        else:
            try:
                parts = next(csv.reader([line]))
            except (csv.Error, StopIteration):
                continue
            parts = [p.strip() for p in parts]
        if len(parts) >= 7 and "." in parts[0]:
            name, value = parts[5], parts[6]
        elif len(parts) >= 2:
            name, value = parts[0], parts[1]
        else:
            continue
        name = name.strip()
        if not name or name.lower() in ("name", "cookie name", "key"):
            continue
        out[name] = value.strip()
    if not out:
        raise ValueError("读不出 cookie（空文件或格式不对）")
    return out


def _mask(value):
    if not value:
        return ""
    return (value[:2] + "…" + value[-2:]) if len(value) > 6 else "****"


def _uid(cookies):
    raw = str(cookies.get("DedeUserID") or "").strip()
    return int(raw) if raw.isdigit() else 0


class AccountStore:
    """一个上传的 CSV = 一个账号；同一名称再次上传会覆盖。"""

    def __init__(self, path):
        self.path = path
        # 原始 CSV 落盘目录：按 uid 命名，同名覆盖（data/accounts/<uid>.csv）
        self.files_dir = os.path.join(os.path.dirname(os.path.abspath(path)),
                                      "accounts")
        self.lock = threading.RLock()
        self.active_id = ""
        self.items = []
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                with self.lock:
                    self.active_id = str(data.get("active_id") or "")
                    self.items = [x for x in (data.get("items") or [])
                                  if isinstance(x, dict) and x.get("id")]
        except (OSError, ValueError):
            pass

    def _save(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"active_id": self.active_id, "items": self.items},
                      handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def public(self, item):
        cookies = item.get("cookies") or {}
        sessdata = str(cookies.get(LOGIN_KEY) or "")
        error = item.get("error") or ""
        profile = item.get("profile") or {}
        uid = _uid(cookies)
        if not uid:
            try:
                uid = int(profile.get("uid") or 0)
            except (TypeError, ValueError):
                uid = 0
        return {"id": item["id"], "label": item.get("label", ""),
                "uid": uid, "sessdata_mask": _mask(sessdata),
                "has_login": bool(sessdata), "error": error,
                "cookie_count": len(cookies),
                "file_name": item.get("file_name", ""),
                "active": item["id"] == self.active_id,
                "profile": item.get("profile") or None,
                "created_at": item.get("created_at", 0)}

    def _refresh_profile(self, item):
        """尽力取一次账号昵称头像；失败静默（结果只影响展示）。"""
        if item.get("error"):
            return
        try:
            profile = fetch_profile(item.get("cookies") or {})
        except Exception:
            profile = None
        if profile:
            item["profile"] = profile

    def list(self):
        with self.lock:
            # 历史账号（老版本添加的）没有 profile，补拉一次；带节流避免每次轮询都联网。
            now = time.time()
            for item in self.items:
                if (not item.get("profile") and not item.get("error")
                        and now - float(item.get("_profile_try", 0)) > 60):
                    item["_profile_try"] = now
                    self._refresh_profile(item)
            return {"ok": True, "active_id": self.active_id,
                    "items": [self.public(x) for x in self.items]}

    def _save_raw(self, uid, text):
        """把上传的原始 CSV 按 uid 落盘，返回文件名（只用 basename）。"""
        if not uid or not text:
            return ""
        try:
            os.makedirs(self.files_dir, exist_ok=True)
            name = "%s.csv" % uid
            with open(os.path.join(self.files_dir, name), "w",
                      encoding="utf-8", newline="") as handle:
                handle.write(text)
            return name
        except OSError:
            return ""

    def add(self, name, text, activate=True):
        cookies = parse_cookie_csv(text)
        label = (str(name or "").strip() or "账号")[:60]
        sessdata = str(cookies.get(LOGIN_KEY) or "")
        error = "" if sessdata else "没有 SESSDATA，不能作为登录账号"
        with self.lock:
            existing = next((x for x in self.items
                             if x.get("label") == label), None)
            if existing:
                item = existing
                item["cookies"] = cookies
                item["error"] = error
                created = False
            else:
                item = {"id": hashlib.sha1(
                    (label + str(time.time())).encode("utf-8")).hexdigest()[:12],
                    "label": label, "cookies": cookies, "error": error,
                    "created_at": int(time.time() * 1000)}
                self.items.append(item)
                created = True
            if activate and not error:
                self.active_id = item["id"]
            # 先补资料拿到 uid，再按 uid 保存原始文件（文件名用 uid）
            self._refresh_profile(item)
            uid = _uid(cookies)
            if not uid:
                try:
                    uid = int((item.get("profile") or {}).get("uid") or 0)
                except (TypeError, ValueError):
                    uid = 0
            saved = self._save_raw(uid, text)
            if saved:
                item["file_name"] = saved
            self._save()
            return self.public(item), created, error

    def remove(self, item_id):
        with self.lock:
            before = len(self.items)
            self.items = [x for x in self.items if x["id"] != str(item_id)]
            if not any(x["id"] == self.active_id for x in self.items):
                self.active_id = ""
            self._save()
            return before != len(self.items)

    def activate(self, item_id):
        with self.lock:
            target = str(item_id or "")
            if not target:
                self.active_id = ""
                self._save()
                return self.list(), ""
            item = next((x for x in self.items if x["id"] == target), None)
            if not item:
                raise ValueError("账号不存在")
            if item.get("error"):
                raise ValueError(item["error"])
            self.active_id = target
            self._refresh_profile(item)
            self._save()
            return self.list(), item.get("label", "")

    def active(self):
        """给采集线程用的完整 cookie dict（内部调用，不经 API）。"""
        with self.lock:
            item = next((x for x in self.items
                         if x["id"] == self.active_id and not x.get("error")),
                        None)
            if not item:
                return None
            return {"id": item["id"], "label": item.get("label", ""),
                    "cookies": dict(item.get("cookies") or {})}
