# -*- coding: utf-8 -*-
"""Fetch a Bilibili user's public dynamics feed (no login required).

The space dynamic feed is WBI-signed since 2023: unsigned requests get 412 /
-352. This module implements the standard public WBI flow:

    1. GET /x/web-interface/nav  -> img_key + sub_key (from wbi_img urls)
    2. mixin key = permutation of (img_key+sub_key) by a fixed table, first 32
    3. params + wts, strip !'()* , sort, urlencode, append w_rid = md5(q+mixin)

All network access goes through ``urlopen`` so tests can inject a fake. Nothing
here needs cookies, which keeps the daily pull reliable even if the captured
account's cookie expires.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import ssl
import time
import urllib.parse
import urllib.request

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# WBI mixin-key permutation (B站公开算法，固定不变)
MIXIN_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def mixin_key(img_key, sub_key):
    """Derive the 32-char WBI mixin key from img/sub keys."""
    raw = (img_key or "") + (sub_key or "")
    return "".join(raw[i] for i in MIXIN_TAB if i < len(raw))[:32]


def wbi_sign(params, img_key, sub_key, ts=None):
    """Return a query string with wts + w_rid added. Pure function (testable)."""
    p = dict(params)
    p["wts"] = int(ts) if ts is not None else int(time.time())
    p = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in p.items()}
    query = urllib.parse.urlencode(sorted(p.items()))
    rid = hashlib.md5((query + mixin_key(img_key, sub_key)).encode()).hexdigest()
    return query + "&w_rid=" + rid


class DynamicsFetcher:
    def __init__(self, urlopen=None, timeout=10, ua=DEFAULT_UA):
        # urlopen(url, timeout=...) -> object with .read(); injectable for tests
        self._urlopen = urlopen
        self.timeout = timeout
        self.ua = ua
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=_ssl_context()))

    def _fetch_json(self, url, referer="https://www.bilibili.com/"):
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua, "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9"})
        if self._urlopen is not None:
            with self._urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        with self.opener.open(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _warmup(self):
        """Establish anonymous session (buvid3) — reduces 412 risk."""
        try:
            req = urllib.request.Request("https://www.bilibili.com/",
                                         headers={"User-Agent": self.ua})
            if self._urlopen is not None:
                self._urlopen(req, timeout=self.timeout).read(1024)
            else:
                self.opener.open(req, timeout=self.timeout).read(1024)
        except Exception:
            pass

    def wbi_keys(self):
        data = self._fetch_json("https://api.bilibili.com/x/web-interface/nav")
        wi = (data.get("data") or {}).get("wbi_img") or {}
        img = str(wi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = str(wi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        return img, sub

    def fetch(self, mid, limit=20, offset=""):
        """Fetch normalized dynamics for ``mid``. Returns (items, has_more)."""
        mid = _as_int(mid)
        if not mid:
            return [], False
        self._warmup()
        img, sub = self.wbi_keys()
        if not img or not sub:
            return [], False
        params = {"host_mid": mid, "timezone_offset": -480,
                  "features": "itemOpusStyle", "platform": "web"}
        if offset:
            params["offset"] = offset
        query = wbi_sign(params, img, sub)
        url = ("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?"
               + query)
        data = self._fetch_json(url,
                                "https://space.bilibili.com/%d/dynamic" % mid)
        if data.get("code") != 0:
            return [], False
        payload = data.get("data") or {}
        raw_items = payload.get("items") or []
        return [normalize(f, mid) for f in raw_items], bool(payload.get("has_more"))


def _first_text(node):
    """Best-effort plain text from an opus summary / desc object."""
    if not isinstance(node, dict):
        return ""
    text = node.get("text")
    if text:
        return str(text)
    rich = node.get("rich_text_nodes") or []
    return "".join(str(n.get("text") or "") for n in rich
                   if isinstance(n, dict))


def normalize(frame, mid):
    """Convert one feed frame into a flat, storage-ready dict."""
    modules = frame.get("modules") or {}
    author = modules.get("module_author") or {}
    dynamic = modules.get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    major_type = str(major.get("type") or "")

    text = ""
    pics = []
    jump = ""

    if major_type == "MAJOR_TYPE_OPUS":
        opus = major.get("opus") or {}
        text = _first_text(opus.get("summary") or {})
        pics = [p.get("url") for p in (opus.get("pics") or [])
                if isinstance(p, dict) and p.get("url")]
        jump = str(opus.get("jump_url") or "")
    elif major_type == "MAJOR_TYPE_DRAW":
        text = _first_text(dynamic.get("desc") or {})
        draw = major.get("draw") or {}
        pics = [p.get("src") for p in (draw.get("items") or [])
                if isinstance(p, dict) and p.get("src")]
    elif major_type == "MAJOR_TYPE_ARTICLE":
        article = major.get("article") or {}
        text = str(article.get("title") or "") + "\n" + str(
            article.get("summary") or "")
        pics = [p for p in (article.get("pics") or []) if p]
        jump = str(article.get("jump_url") or "")
    else:
        text = _first_text(dynamic.get("desc") or {})

    # 动态正文为空但原文里有点赞语（如「今日もこの後から配信する！」）时，
    # 上面已经从 opus.summary 取到；这里只做兜底。
    dyn_id = str(frame.get("id_str") or frame.get("id") or "")
    return {
        "id": dyn_id,
        "mid": _as_int(author.get("mid") or mid),
        "pub_ts": _as_int(author.get("pub_ts")),
        "type": str(frame.get("type") or ""),
        "text": str(text).strip(),
        "pics": pics,
        "jump_url": jump,
        "raw_json": frame,
    }
