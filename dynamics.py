# -*- coding: utf-8 -*-
"""Fetch a Bilibili user's public dynamics feed (WBI-signed).

The space dynamic feed is WBI-signed since 2023: unsigned requests get 412 /
-352. This module implements the standard public WBI flow:

    1. GET /x/web-interface/nav  -> img_key + sub_key (from wbi_img urls)
    2. mixin key = permutation of (img_key+sub_key) by a fixed table, first 32
    3. params + wts, strip !'()* , sort, urlencode, append w_rid = md5(q+mixin)

Anonymous requests work on a clean home network but are frequently hit by
B站 412 risk control (shared/cloud egress especially). Pass ``cookies`` with a
valid SESSDATA to send them as a logged-in user; the scheduler does this from
the active account. The cookie is optional: with none, behaviour is unchanged
(fully anonymous) so offline tests and cookie-less installs still work.

All network access goes through ``urlopen`` so tests can inject a fake.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# 抓动态真正用得上的几个 cookie；带上整包无关项反而容易被风控盯上。
LOGIN_COOKIE_KEYS = ("SESSDATA", "DedeUserID", "DedeUserID__ckMd5",
                     "bili_jct", "buvid3", "buvid4", "sid")

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


class DynamicsFetchError(RuntimeError):
    """动态接口没拿到可信数据（网络异常 / code!=0 / WBI key 缺失）。

    必须和「接口正常但确实 0 条」区分开：调用方不能把失败当成功写入
    dyn_last_ts，否则当天不再重试，一次抖动就变成整天没数据。
    """


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
    def __init__(self, urlopen=None, timeout=10, ua=DEFAULT_UA, cookies=None):
        # urlopen(url, timeout=...) -> object with .read(); injectable for tests
        self._urlopen = urlopen
        self.timeout = timeout
        self.ua = ua
        # 可选登录态：有 SESSDATA 就带上，避免匿名请求被 412 风控。
        # 只挑白名单里的键，避免把无关 cookie 甩给接口。
        self.cookies = {k: v for k, v in (cookies or {}).items()
                        if k in LOGIN_COOKIE_KEYS and v}
        self.logged_in = bool(self.cookies.get("SESSDATA"))
        # 诊断用：记下最近一次失败的一句话，调度器写进日志。
        self.last_diag = ""
        self.cookiejar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar),
            urllib.request.HTTPSHandler(context=_ssl_context()))

    def _headers(self, referer):
        h = {"User-Agent": self.ua, "Referer": referer,
             "Accept": "application/json, text/plain, */*",
             "Accept-Language": "zh-CN,zh;q=0.9"}
        if self.cookies:
            h["Cookie"] = "; ".join("%s=%s" % kv for kv in self.cookies.items())
        return h

    def _fetch_json(self, url, referer="https://www.bilibili.com/"):
        req = urllib.request.Request(url, headers=self._headers(referer))
        try:
            if self._urlopen is not None:
                with self._urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            with self.opener.open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 412 是风控特征码，把诊断信息带上，便于日志区分「匿名被拦」和「真异常」
            self.last_diag = "HTTP %s（%s）%s" % (
                exc.code, "疑似风控拦截" if exc.code == 412 else "接口错误",
                "" if self.logged_in else "·匿名请求")
            raise

    def _warmup(self):
        """Establish session (buvid3) before the signed request."""
        try:
            req = urllib.request.Request(
                "https://www.bilibili.com/", headers=self._headers("https://www.bilibili.com/"))
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
        """Fetch normalized dynamics for ``mid``. Returns (items, has_more).

        B站对「同一签名短时间内重复请求」偶发返回 code=0 但 items=[]，
        因此空结果时换新 wbi key + 新 wts 重试一次。

        Raises ``DynamicsFetchError`` when the request itself failed (network /
        code!=0 / no WBI key). A clean empty result still returns ``([], False)``
        — the caller must treat that as "not a successful pull" too, so a
        transient empty response never gets recorded as done for the day.
        """
        mid = _as_int(mid)
        if not mid:
            raise DynamicsFetchError("uid 无效：%r" % (mid,))
        last_error = None
        for attempt in range(2):
            try:
                items, has_more = self._fetch_once(mid, offset)
                last_error = None
            except DynamicsFetchError as exc:
                items, has_more = [], False
                last_error = exc
            if items:
                return items, has_more
            if attempt == 0:
                time.sleep(1.2)
        if last_error is not None:
            raise last_error
        return [], False

    def _fetch_once(self, mid, offset=""):
        self._warmup()
        img, sub = self.wbi_keys()
        if not img or not sub:
            raise DynamicsFetchError("WBI key 为空，无法签名动态接口")
        params = {"host_mid": mid, "timezone_offset": -480,
                  "features": "itemOpusStyle", "platform": "web"}
        if offset:
            params["offset"] = offset
        query = wbi_sign(params, img, sub)
        url = ("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?"
               + query)
        try:
            data = self._fetch_json(url,
                                    "https://space.bilibili.com/%d/dynamic" % mid)
        except urllib.error.HTTPError as exc:
            # 412 是风控特征码，直接在异常处判定，不依赖 _fetch_json 的副作用
            # （测试里 _fetch_json 常被替换掉）。一眼区分「匿名被拦」和「真异常」。
            hint = "HTTP %s（%s）" % (
                exc.code, "疑似风控拦截" if exc.code == 412 else "接口错误")
            raise DynamicsFetchError("动态接口 %s（登录态：%s）" % (
                hint, "有" if self.logged_in else "匿名")) from exc
        except Exception as exc:
            raise DynamicsFetchError("动态接口请求失败：%s（登录态：%s）" % (
                exc, "有" if self.logged_in else "匿名")) from exc
        if data.get("code") != 0:
            raise DynamicsFetchError("动态接口 code=%s %s（登录态：%s）" % (
                data.get("code"), data.get("message"),
                "有" if self.logged_in else "匿名"))
        payload = data.get("data") or {}
        raw_items = payload.get("items") or []
        return ([normalize(f, mid) for f in raw_items],
                bool(payload.get("has_more")))


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
