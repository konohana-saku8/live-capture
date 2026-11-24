# -*- coding: utf-8 -*-
"""手机服务的 HTTP 客户端：列表、Range 下载、分块上传、提交。

只依赖标准库。下载和上传都支持断点续传——1 GB 的文件走 Wi-Fi 要三分钟，
中途断一次不该从头再来。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Aborted(RuntimeError):
    """用户点了停止。"""


class Client:
    def __init__(self, host: str, token: str = "", timeout: int = 60):
        self.host = host.strip().rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.token = token or ""
        self.timeout = timeout

    # ---------- 底层 ----------

    def _open(self, path: str, headers: dict | None = None, data=None,
              method: str | None = None, timeout: int | None = None):
        url = self.host + path
        hdrs = dict(headers or {})
        if self.token:
            hdrs["X-Access-Token"] = self.token
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except OSError:
                pass
            msg = ""
            try:
                msg = (json.loads(body or b"{}") or {}).get("error") or ""
            except ValueError:
                msg = body[:200].decode("utf-8", "replace")
            raise ApiError(msg or ("HTTP %d" % exc.code), exc.code)
        except urllib.error.URLError as exc:
            raise ApiError("连不上 %s：%s" % (self.host, exc.reason))

    def _json(self, path: str, **kw) -> dict:
        with self._open(path, **kw) as resp:
            return json.loads(resp.read() or b"{}")

    def _post(self, path: str, **kw) -> dict:
        return self._json(path, method="POST", **kw)

    # ---------- 读 ----------

    def health(self) -> bool:
        try:
            return bool(self._json("/api/health").get("ok"))
        except ApiError:
            return False

    def recordings(self, room_id: int = 0) -> list[dict]:
        q = "?room_id=%d" % room_id if room_id else ""
        return self._json("/api/recordings" + q).get("items") or []

    def live(self) -> tuple[bool, list[str]]:
        """返回 ``(是否有房间在播, 在播的房间名)``。"""
        try:
            data = self._json("/api/dashboard")
        except ApiError:
            return False, []
        rooms = data.get("rooms") or {}
        # 实际结构是 {"active": 1, "items": [...]}；早先版本直接给列表
        items = rooms.get("items") if isinstance(rooms, dict) else rooms
        names = []
        for room in items or []:
            if not isinstance(room, dict):
                continue
            try:
                if int(room.get("live_status") or 0) == 1:
                    names.append(room.get("uname") or room.get("label")
                                 or str(room.get("id")))
            except (TypeError, ValueError):
                continue
        return bool(names), names

    def part_url(self, rec_id: int, part: int) -> str:
        return "/api/recordings/%d/media?kind=mp4&part=%d" % (rec_id, part)

    def incoming(self, rec_id: int, part: int) -> dict:
        return self._json("/api/recordings/%d/parts/%d/incoming" % (rec_id, part))

    # ---------- 下载 ----------

    def download(self, rec_id: int, part: int, dest: str,
                 on_progress=None, stop=None) -> str:
        """Range 续传下载到 ``dest``；已存在的部分不会重下。"""
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        headers = {"Range": "bytes=%d-" % have} if have else {}
        with self._open(self.part_url(rec_id, part), headers=headers,
                        timeout=120) as resp:
            if resp.status != 206:      # 服务端不支持 Range，只能重下
                have = 0
            rest = int(resp.headers.get("Content-Length") or 0)
            total = have + rest if rest else 0
            mode = "ab" if have else "wb"
            done = have
            with open(dest, mode) as fh:
                while True:
                    if stop and stop():
                        raise Aborted("已停止")
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        return dest

    # ---------- 上传 ----------

    def upload(self, rec_id: int, part: int, path: str, chunk_mb: int = 8,
               on_progress=None, stop=None) -> dict:
        """分块上传后直接 commit，返回服务端的 commit 结果。"""
        total = os.path.getsize(path)
        chunk_bytes = max(1, int(chunk_mb)) * 1024 * 1024
        offset = int(self.incoming(rec_id, part).get("offset") or 0)
        if offset > total:              # 脏数据（比如换了文件），清掉重来
            self.abort(rec_id, part)
            offset = 0
        with open(path, "rb") as fh:
            while offset < total:
                if stop and stop():
                    raise Aborted("已停止")
                fh.seek(offset)
                blob = fh.read(chunk_bytes)
                if not blob:
                    break
                try:
                    data = self._post(
                        "/api/recordings/%d/parts/%d/upload" % (rec_id, part),
                        data=blob,
                        headers={"X-Chunk-Offset": str(offset),
                                 "Content-Type": "application/octet-stream"})
                    offset = int(data.get("offset") or (offset + len(blob)))
                except ApiError as exc:
                    if exc.status != 409:
                        raise
                    offset = int(self.incoming(rec_id, part).get("offset") or 0)
                if on_progress:
                    on_progress(offset, total)
        return self._post("/api/recordings/%d/parts/%d/commit" % (rec_id, part),
                          headers={"X-Total-Size": str(total)})

    def abort(self, rec_id: int, part: int) -> None:
        try:
            self._post("/api/recordings/%d/parts/%d/abort" % (rec_id, part))
        except ApiError:
            pass


# ---------- 链路 ----------

def adb_forward(local_port: int = 8765, device_port: int = 8765) -> bool:
    """尽量把 adb 端口转发建起来（幂等）；没有 adb 就返回 False。"""
    exe = shutil.which("adb")
    if not exe:
        return False
    try:
        subprocess.run([exe, "start-server"], capture_output=True, timeout=30,
                       creationflags=CREATE_NO_WINDOW)
        out = subprocess.run(
            [exe, "forward", "tcp:%d" % local_port, "tcp:%d" % device_port],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def connect(host: str, fallback: str = "", token: str = "") -> tuple[Client, str]:
    """按 adb → 直连 的顺序找一个能用的地址，返回 ``(client, 说明)``。"""
    port = 8765
    if ":" in host:
        try:
            port = int(host.rsplit(":", 1)[1])
        except ValueError:
            pass
    tried = []
    if host.startswith("127.0.0.1") or host.startswith("localhost"):
        if adb_forward(port):
            client = Client(host, token)
            if client.health():
                return client, "adb（USB）"
            tried.append(host)
        else:
            tried.append(host + "（adb 转发失败）")
    client = Client(host, token)
    if client.health():
        return client, host
    if fallback:
        alt = Client(fallback, token)
        if alt.health():
            return alt, fallback
        tried.append(fallback)
    raise ApiError("连不上服务：" + "、".join(tried or [host]))


def human_size(size: int) -> str:
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%.1f %s" % (size, unit)) if unit != "B" else ("%d B" % size)
        size /= 1024.0
    return "%.1f GB" % size


def now_ms() -> int:
    return int(time.time() * 1000)
