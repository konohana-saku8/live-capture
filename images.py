# -*- coding: utf-8 -*-
"""Same-origin image proxy for Bilibili emote pictures.

Bilibili's CDN answers 403 when the Referer is a third-party origin, which is
exactly what a browser sends when an <img> on the local dashboard loads a
cross-origin picture (``http://192.168.x.x:8765/``). The web UI therefore
first tries ``<img referrerpolicy="no-referrer">`` and, when that still fails,
falls back to this proxy, which fetches with an official Referer and caches.

Only Bilibili image CDN hosts are accepted, so this cannot be used as a
general-purpose proxy / SSRF vector. Standard library only (Termux friendly).
"""
from __future__ import annotations

import collections
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

ALLOWED_DOMAINS = ("hdslb.com", "bilibili.com", "biliimg.com")
MAX_URL = 2048
MAX_BYTES = 3 * 1024 * 1024
MAX_ENTRIES = 256
MAX_CACHE_BYTES = 16 * 1024 * 1024
TIMEOUT = 8
OK_TTL = 6 * 3600
FAIL_TTL = 30

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://live.bilibili.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
HEADERS_NO_REFERER = {"User-Agent": UA, "Accept": HEADERS["Accept"]}

_EXT_TYPES = {
    "png": "image/png", "webp": "image/webp",
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "apng": "image/apng",
}


def _transparent_png():
    """A valid 1x1 transparent PNG, used as the body of a 502 for <img>."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    raw = b"\x00\x00\x00\x00\x00"          # filter byte + RGBA(0,0,0,0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


FALLBACK_PNG = _transparent_png()


class ImageError(Exception):
    pass


def normalize_image_url(raw):
    """Return a safe CDN url, or None when the address is not allowed."""
    url = str(raw or "").strip()
    if not url or len(url) > MAX_URL:
        return None
    if url.startswith("//"):
        url = "https:" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    if parts.username or parts.password:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port not in (None, 80, 443):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
        return None
    if any(ord(ch) < 32 for ch in url):
        return None
    return url


def _content_type(resp, url):
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    ext = urllib.parse.urlsplit(url).path.lower().split(".")[-1]
    # Never serve SVG from our own origin: it can execute script.
    if "svg" in ctype or ext == "svg":
        raise ImageError("不支持 SVG 图片")
    if ctype.startswith("image/"):
        return ctype
    if ctype in ("", "application/octet-stream", "binary/octet-stream"):
        guessed = _EXT_TYPES.get(ext.split("@")[0])
        if guessed:
            return guessed
    raise ImageError("返回不是图片：%s" % (ctype or "未知类型"))


def _download(url, opener):
    """Fetch once, retrying without Referer when the CDN rejects it."""
    last = None
    for headers in (HEADERS, HEADERS_NO_REFERER):
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener(req, timeout=TIMEOUT) as resp:
                ctype = _content_type(resp, url)
                body = resp.read(MAX_BYTES + 1)
            if not body:
                raise ImageError("图片内容为空")
            if len(body) > MAX_BYTES:
                raise ImageError("图片超过大小上限")
            return ctype, body
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (401, 403):
                raise ImageError("CDN 返回 %s" % exc.code) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ImageError("无法读取图片：%s" % exc) from exc
    raise ImageError("CDN 拒绝访问（%s）" % (last.code if last else 403))


class EmoteProxy:
    """Small LRU cache of CDN images; negative results are cached briefly."""

    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen
        self.cache = collections.OrderedDict()
        self.lock = threading.Lock()
        self.bytes = 0

    def _drop(self, url):
        item = self.cache.pop(url, None)
        if item and item[2]:
            self.bytes -= len(item[2])

    def _store(self, url, ctype, body, expire):
        with self.lock:
            self._drop(url)
            self.cache[url] = (expire, ctype, body)
            if body:
                self.bytes += len(body)
            while len(self.cache) > MAX_ENTRIES or self.bytes > MAX_CACHE_BYTES:
                self._drop(next(iter(self.cache)))

    def get(self, raw):
        url = normalize_image_url(raw)
        if not url:
            raise ImageError("不被允许的图片地址")
        now = time.time()
        with self.lock:
            hit = self.cache.get(url)
            if hit and hit[0] > now:
                self.cache.move_to_end(url)
                if hit[1] is None:
                    raise ImageError("图片暂时不可用")
                return hit[1], hit[2]
            if hit:
                self._drop(url)
        try:
            ctype, body = _download(url, self.opener)
        except ImageError:
            self._store(url, None, None, now + FAIL_TTL)
            raise
        self._store(url, ctype, body, now + OK_TTL)
        return ctype, body

    def stats(self):
        with self.lock:
            return {"entries": len(self.cache), "bytes": self.bytes}
