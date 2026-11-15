# -*- coding: utf-8 -*-
"""Bilibili live danmaku / Super Chat collector.

Protocol details are ported from danmaku-it/collector (WBI signing, 16-byte
packet header, zlib/brotli nesting, SUPER_CHAT_MESSAGE_JPN translation).
The client asks for protover=2 (zlib) by default so Termux does not need to
compile brotli.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.cookiejar
import json
import struct
import threading
import time
import urllib.parse
import urllib.request
import zlib

try:
    import brotli
    HAVE_BROTLI = True
except ImportError:
    brotli = None
    HAVE_BROTLI = False

try:
    from websockets.asyncio.client import connect
    WS_IMPORT_ERROR = ""
except Exception as exc:                       # pragma: no cover - depends on pip
    connect = None
    WS_IMPORT_ERROR = str(exc)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

HEADER = struct.Struct(">IHHII")
HEADER_SIZE = 16
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8
PROTO_ZLIB = 2
PROTO_BROTLI = 3
CLIENT_PROTO_VER = 1


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Packet:
    __slots__ = ("operation", "proto_ver", "body")

    def __init__(self, operation, proto_ver, body):
        self.operation = operation
        self.proto_ver = proto_ver
        self.body = body


def pack_packet(operation, body=b"", header_ver=CLIENT_PROTO_VER, sequence=1):
    return HEADER.pack(HEADER_SIZE + len(body), HEADER_SIZE, header_ver,
                       operation, sequence) + body


def pack_auth(room_id, token, uid=0, protover=2):
    payload = {"uid": uid, "roomid": room_id, "protover": protover,
               "platform": "web", "type": 2, "key": token}
    return pack_packet(OP_AUTH,
                       json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def pack_heartbeat():
    return pack_packet(OP_HEARTBEAT, b"[object Object]")


def iter_packets(data):
    offset, total = 0, len(data)
    while offset + HEADER_SIZE <= total:
        packet_len, header_len, proto_ver, operation, _seq = \
            HEADER.unpack_from(data, offset)
        if packet_len < HEADER_SIZE or offset + packet_len > total:
            return
        yield Packet(operation, proto_ver,
                     data[offset + header_len: offset + packet_len])
        offset += packet_len


def _decompress(proto_ver, body):
    if proto_ver == PROTO_ZLIB:
        return zlib.decompress(body)
    if proto_ver == PROTO_BROTLI:
        if not HAVE_BROTLI:
            raise RuntimeError("brotli 未安装，把压缩协议改成 2 (zlib)")
        return brotli.decompress(body)
    return body


def _json_objects(body):
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return
    if isinstance(obj, dict):
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item


def load_messages(packet):
    if packet.proto_ver in (PROTO_ZLIB, PROTO_BROTLI):
        try:
            inflated = _decompress(packet.proto_ver, packet.body)
        except Exception:
            return
        nested = list(iter_packets(inflated))
        if nested:
            for sub in nested:
                if sub.proto_ver in (PROTO_ZLIB, PROTO_BROTLI):
                    yield from load_messages(sub)
                else:
                    yield from _json_objects(sub.body)
        else:
            yield from _json_objects(inflated)
        return
    yield from _json_objects(packet.body)


def parse_auth_reply(packet):
    try:
        obj = json.loads(packet.body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def parse_popularity(packet):
    if len(packet.body) == 4:
        return struct.unpack(">I", packet.body)[0]
    return None


class BiliApi:
    """Anonymous-capable HTTP client: buvid seeding, WBI signing, room/token info."""

    def __init__(self, config):
        self.user_agent = DEFAULT_UA
        self.timeout = float(config.get("http_timeout") or 10)
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self._mixin_key = None
        self._mixin_key_at = 0.0
        self._ready = False
        self.warning = ""
        for name, key in (("SESSDATA", "sessdata"),
                          ("DedeUserID", "dede_user_id"),
                          ("buvid3", "buvid3")):
            if config.get(key):
                self._set_cookie(name, str(config[key]))
        # 网页上传的 cookie CSV 全量注入；账号优先于手填的三项，
        # 与旧 sc_collector 里 sender.cookies() 交给采集连接的口径一致。
        extra = config.get("_cookies") or {}
        if isinstance(extra, dict):
            for name, value in extra.items():
                if name and value:
                    self._set_cookie(str(name), str(value))

    def _request(self, url, referer="https://live.bilibili.com/"):
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        })
        with self._opener.open(req, timeout=self.timeout) as resp:
            return resp.read()

    def _get_json(self, url, referer="https://live.bilibili.com/"):
        return json.loads(self._request(url, referer).decode("utf-8"))

    def _set_cookie(self, name, value, domain=".bilibili.com"):
        self._jar.set_cookie(http.cookiejar.Cookie(
            0, name, value, None, False, domain, False, False, "/", True,
            False, None, True, None, None, {},
        ))

    def cookies(self):
        return {c.name: c.value for c in self._jar}

    def cookie_header(self):
        return "; ".join("%s=%s" % (c.name, c.value) for c in self._jar)

    def ensure_cookies(self):
        if self._ready:
            return
        if "buvid3" not in self.cookies():
            try:
                self._request("https://www.bilibili.com/",
                              referer="https://www.bilibili.com/")
            except Exception:
                pass
        try:
            spi = self._get_json(
                "https://api.bilibili.com/x/frontend/finger/spi",
                referer="https://www.bilibili.com/")
            data = spi.get("data") or {}
            if data.get("b_3"):
                self._set_cookie("buvid3", data["b_3"])
            if data.get("b_4"):
                self._set_cookie("buvid4", data["b_4"])
        except Exception:
            pass
        if self.cookies().get("SESSDATA") and not self.cookies().get("DedeUserID"):
            try:
                nav = self._get_json(
                    "https://api.bilibili.com/x/web-interface/nav",
                    referer="https://www.bilibili.com/")
                mid = (nav.get("data") or {}).get("mid")
                if mid:
                    self._set_cookie("DedeUserID", str(mid))
            except Exception:
                pass
        self._ready = True

    def _refresh_mixin_key(self):
        nav = self._get_json(
            "https://api.bilibili.com/x/web-interface/nav",
            referer="https://www.bilibili.com/")
        wbi = (nav.get("data") or {}).get("wbi_img") or {}
        img_key = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub_key = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            raise RuntimeError("nav 未返回 wbi keys")
        raw = img_key + sub_key
        self._mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        self._mixin_key_at = time.time()
        return self._mixin_key

    def mixin_key(self):
        if self._mixin_key is None or time.time() - self._mixin_key_at > 1800:
            return self._refresh_mixin_key()
        return self._mixin_key

    def sign(self, params):
        try:
            key = self.mixin_key()
        except Exception as exc:
            self.warning = "WBI 签名不可用，改用无签名请求：%s" % exc
            plain = dict(params)
            plain["wts"] = int(time.time())
            return urllib.parse.urlencode(sorted(plain.items()))
        signed = dict(params)
        signed["wts"] = int(time.time())
        filtered = {k: "".join(ch for ch in str(v) if ch not in "!'()*")
                    for k, v in signed.items()}
        query = urllib.parse.urlencode(sorted(filtered.items()))
        filtered["w_rid"] = hashlib.md5((query + key).encode("utf-8")).hexdigest()
        return urllib.parse.urlencode(sorted(filtered.items()))

    def resolve_room(self, room_id):
        self.ensure_cookies()
        query = self.sign({"room_id": room_id, "web_location": "444.8"})
        url = ("https://api.live.bilibili.com/xlive/web-room/v1/index/"
               "getInfoByRoom?" + query)
        data = self._get_json(url)
        if data.get("code") != 0:
            raise RuntimeError("getInfoByRoom code=%s %s" %
                               (data.get("code"), data.get("message")))
        payload = data.get("data") or {}
        room = payload.get("room_info") or {}
        anchor = (payload.get("anchor_info") or {}).get("base_info") or {}
        return {"real_room_id": _as_int(room.get("room_id"), room_id),
                "title": room.get("title") or "",
                "uname": anchor.get("uname") or "",
                "live_status": _as_int(room.get("live_status")),
                "online": _as_int(room.get("online"))}

    def get_danmu_info(self, room_id):
        self.ensure_cookies()
        query = self.sign({"id": room_id, "type": 0, "web_location": "444.8"})
        url = ("https://api.live.bilibili.com/xlive/web-room/v1/index/"
               "getDanmuInfo?" + query)
        data = self._get_json(url)
        if data.get("code") != 0:
            raise RuntimeError("getDanmuInfo code=%s %s" %
                               (data.get("code"), data.get("message")))
        payload = data.get("data") or {}
        token = payload.get("token") or ""
        hosts = payload.get("host_list") or []
        if not token or not hosts:
            raise RuntimeError("getDanmuInfo 返回空 token/host_list")
        return token, hosts

    @staticmethod
    def websocket_urls(host_list):
        urls = []
        for host in host_list:
            name, port = host.get("host"), host.get("wss_port")
            if name and port:
                urls.append("wss://%s:%s/sub" % (name, port))
        fallback = "wss://broadcastlv.chat.bilibili.com:443/sub"
        if fallback not in urls:
            urls.append(fallback)
        return urls


def build_danmaku(room_id, info, now_ms):
    """Flatten one DANMU_MSG info array (no emoticon decoding)."""
    try:
        head = info[0] if isinstance(info[0], list) else []
        profile = info[2] if len(info) > 2 and isinstance(info[2], list) else []
        user = {}
        if len(head) > 15 and isinstance(head[15], dict):
            user = head[15].get("user") or {}
        base = user.get("base") or {}
        medal = user.get("medal") or {}
        uid = _as_int(user.get("uid")) or _as_int(profile[0] if profile else 0)
        uname = base.get("name") or (profile[1] if len(profile) > 1 else "") or ""
        event_time = _as_int(head[4]) // 1000 if len(head) > 4 else 0
        return {"room_id": room_id, "uid": uid, "uname": uname,
                "text": str(info[1] or ""), "event_time": event_time,
                "received_at": now_ms,
                "medal_name": medal.get("name") or "",
                "medal_level": _as_int(medal.get("level")),
                "guard_level": _as_int(medal.get("guard_level")),
                "is_admin": 1 if len(profile) > 2 and profile[2] else 0,
                "is_emoticon": 1 if len(head) > 13 and isinstance(head[13], dict) else 0}
    except (IndexError, TypeError, ValueError, AttributeError):
        return None


def extract_sc(room_id, data, now_ms):
    """Flatten one SUPER_CHAT_MESSAGE payload (mirrors danmaku-it)."""
    sc_id = data.get("id")
    if sc_id is None:
        return None
    user = data.get("user_info") or {}
    medal = data.get("medal_info") or {}
    start = _as_int(data.get("start_time")) or now_ms // 1000
    return {"sc_id": str(sc_id), "room_id": room_id,
            "uid": _as_int(data.get("uid")), "uname": user.get("uname") or "",
            "price": _as_int(data.get("price")), "message": data.get("message") or "",
            "message_trans": (data.get("message_trans") or "").strip() or None,
            "start_time": start, "end_time": _as_int(data.get("end_time")),
            "received_at": now_ms,
            "guard_level": _as_int(user.get("guard_level")),
            "medal_name": medal.get("medal_name") or "",
            "medal_level": _as_int(medal.get("medal_level")),
            "raw_json": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}


def event_row(room_id, kind, data, now_ms):
    data = data if isinstance(data, dict) else {}
    return {"room_id": room_id, "kind": kind,
            "uid": _as_int(data.get("uid")),
            "uname": data.get("uname") or data.get("username") or "",
            "detail": _event_detail(kind, data),
            "event_time": _as_int(data.get("start_time") or data.get("timestamp")),
            "received_at": now_ms,
            "raw_json": json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:4000]}


def _event_detail(kind, data):
    if kind == "guard":
        return "舰长 %s 级 ×%s" % (data.get("guard_level") or "?",
                                  data.get("num") or 1)
    if kind == "gift":
        gift = data.get("gift") or {}
        return "%s ×%s" % (gift.get("giftName") or data.get("gift_name") or "礼物",
                           gift.get("num") or data.get("num") or 1)
    if kind == "sc_delete":
        return "删除 SC：%s" % (data.get("ids") or data.get("id") or "")
    return kind


class CaptureManager:
    """Runs the collector in one background thread; HTTP side only reads state."""

    def __init__(self, store, config_getter, log_fn, accounts=None):
        self.store = store
        self.config_getter = config_getter
        self.log = log_fn
        self.accounts = accounts
        self.lock = threading.RLock()
        self._thread = None
        self._stop = None
        self._dm_buf = []
        self._pending_trans = {}
        self.state = self._blank_state()

    @staticmethod
    def _blank_state():
        return {"running": False, "room_id": 0, "real_room_id": 0,
                "title": "", "uname": "", "live_status": 0, "online": 0,
                "started_at": 0, "stopped_at": 0, "last_packet_at": 0,
                "last_error": "", "warning": "", "reconnects": 0,
                "popularity": 0,
                "counts": {"messages": 0, "danmaku": 0, "sc": 0,
                           "events": 0, "duplicates": 0, "sc_trans": 0}}

    def _set(self, **kwargs):
        with self.lock:
            self.state.update(kwargs)

    def _inc(self, key, amount=1):
        with self.lock:
            self.state["counts"][key] += amount

    def snapshot(self):
        with self.lock:
            out = dict(self.state)
            out["counts"] = dict(self.state["counts"])
        out["storage"] = self.store.counts()
        return out

    def start(self):
        with self.lock:
            if self._thread and self._thread.is_alive():
                return False, "采集已经在运行"
        if connect is None:
            return False, "缺少依赖 websockets：%s" % (WS_IMPORT_ERROR or "未安装")
        cfg = dict(self.config_getter())
        if self.accounts:
            acc = self.accounts.active()
            if acc:
                cookies = acc["cookies"]
                cfg["_cookies"] = cookies
                for name, key in (("SESSDATA", "sessdata"),
                                  ("DedeUserID", "dede_user_id"),
                                  ("buvid3", "buvid3")):
                    if cookies.get(name):
                        cfg[key] = cookies[name]
                self.log("info", "使用 cookie 账号：%s" % acc["label"])
        if not _as_int(cfg.get("room_id")):
            return False, "请先在网页里填写房间号"
        self._stop = threading.Event()
        self._dm_buf = []
        self._pending_trans = {}
        self.state = self._blank_state()
        self._set(running=True, started_at=int(time.time() * 1000),
                  room_id=_as_int(cfg.get("room_id")))
        self._thread = threading.Thread(target=self._thread_main, args=(cfg,),
                                        name="live-capture", daemon=True)
        self._thread.start()
        self.log("info", "采集已启动：房间 %s" % cfg.get("room_id"))
        return True, "已启动"

    def stop(self):
        event, thread = self._stop, self._thread
        if event:
            event.set()
        # 先落状态再等线程收尾：网页/API 立刻就能读到「已停止」，
        # 不用等 join 或下一次轮询。
        self._set(running=False, stopped_at=int(time.time() * 1000))
        if thread and thread.is_alive():
            thread.join(timeout=6)
        self.log("info", "采集已停止")
        return True, "已停止"

    def _thread_main(self, cfg):
        try:
            asyncio.run(self._main(cfg))
        except Exception as exc:
            self._set(last_error="采集线程异常：%s" % exc)
            self.log("error", "采集线程异常：%s" % exc)
        finally:
            self._flush_danmaku()
            # 只有「当前这一场」才有权收尾；旧线程退出时不能把新一场打回未启动
            with self.lock:
                if self._thread is threading.current_thread():
                    self.state["running"] = False
                    self.state["stopped_at"] = int(time.time() * 1000)

    async def _main(self, cfg):
        room = _as_int(cfg.get("room_id"))
        api = BiliApi(cfg)
        run_id = self.store.start_run(room, cfg)
        backoff = 1.0
        try:
            while not self._stop.is_set():
                try:
                    try:
                        info = await asyncio.to_thread(api.resolve_room, room)
                    except Exception as exc:
                        info = {"real_room_id": room, "title": "", "uname": "",
                                "live_status": 0, "online": 0}
                        self._set(warning="房间信息获取失败，直接用输入房间号：%s" % exc)
                    self._set(real_room_id=info["real_room_id"],
                              title=info.get("title", ""),
                              uname=info.get("uname", ""),
                              live_status=info.get("live_status", 0),
                              online=info.get("online", 0),
                              warning=api.warning)
                    token, hosts = await asyncio.to_thread(
                        api.get_danmu_info, info["real_room_id"])
                    urls = api.websocket_urls(hosts)
                    ws = await self._connect(api, info["real_room_id"],
                                             token, urls, cfg)
                    backoff = 1.0
                    self._set(last_error="")
                    await self._serve(ws, info["real_room_id"], run_id, cfg)
                except Exception as exc:
                    if self._stop.is_set():
                        break
                    with self.lock:
                        self.state["reconnects"] += 1
                    self._set(last_error=str(exc))
                    self.log("warn", "连接中断：%s，%.0f 秒后重连" % (exc, backoff))
                    await self._sleep(backoff)
                    backoff = min(60.0, backoff * 2)
        finally:
            self.store.end_run(run_id)

    async def _connect(self, api, room_id, token, urls, cfg):
        protover = _as_int(cfg.get("protover"), 2)
        if protover == 3 and not HAVE_BROTLI:
            protover = 2
            self._set(warning="brotli 未安装，已退回 zlib（protover=2）")
        headers = {"User-Agent": api.user_agent,
                   "Origin": "https://live.bilibili.com",
                   "Referer": "https://live.bilibili.com/"}
        cookie = api.cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        uid = _as_int(api.cookies().get("DedeUserID"))
        last = "无可用服务器"
        for url in urls:
            if self._stop.is_set():
                break
            ws = None
            try:
                ws = await connect(url, additional_headers=headers,
                                   open_timeout=10, close_timeout=3,
                                   ping_interval=None, max_size=None,
                                   compression=None)
                await ws.send(pack_auth(room_id, token, uid, protover))
                reply = await asyncio.wait_for(self._await_auth(ws), timeout=12)
                if reply and _as_int(reply.get("code"), -1) == 0:
                    self.log("info", "已连接 %s（protover=%s）" % (url, protover))
                    return ws
                last = "鉴权被拒：%s" % reply
            except Exception as exc:
                last = str(exc)
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            self.log("warn", "%s 连接失败：%s" % (url, last))
        raise ConnectionError("所有弹幕服务器都连不上：%s" % last)

    async def _await_auth(self, ws):
        while True:
            raw = await ws.recv()
            if not isinstance(raw, (bytes, bytearray)):
                continue
            for packet in iter_packets(raw):
                if packet.operation == OP_AUTH_REPLY:
                    return parse_auth_reply(packet)

    async def _serve(self, ws, room_id, run_id, cfg):
        heartbeat = asyncio.create_task(self._heartbeat(ws, cfg))
        stale = max(30, _as_int(cfg.get("stale_seconds"), 120))
        last = time.time()
        try:
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except (asyncio.TimeoutError, TimeoutError):
                    self._flush_danmaku()
                    if time.time() - last > stale:
                        raise ConnectionError("超过 %s 秒没有收到数据" % stale)
                    continue
                last = time.time()
                if isinstance(raw, (bytes, bytearray)):
                    self._handle_frame(raw, room_id, run_id, cfg)
        finally:
            heartbeat.cancel()
            self._flush_danmaku()
            try:
                await ws.close()
            except Exception:
                pass

    async def _heartbeat(self, ws, cfg):
        period = max(10, _as_int(cfg.get("heartbeat_seconds"), 30))
        ticks = 0
        while not self._stop.is_set():
            await asyncio.sleep(1)
            ticks += 1
            if ticks >= period:
                ticks = 0
                try:
                    await ws.send(pack_heartbeat())
                except Exception:
                    return

    async def _sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            await asyncio.sleep(min(1.0, max(0.1, end - time.time())))

    def _handle_frame(self, raw, room_id, run_id, cfg):
        for packet in iter_packets(raw):
            if packet.operation == OP_HEARTBEAT_REPLY:
                pop = parse_popularity(packet)
                if pop is not None:
                    self._set(popularity=pop)
                continue
            if packet.operation != OP_MESSAGE:
                continue
            for message in load_messages(packet):
                self._handle_message(message, room_id, run_id, cfg)

    def _handle_message(self, message, room_id, run_id, cfg):
        self._inc("messages")
        cmd = (message.get("cmd") or "").split(":", 1)[0]
        data = message.get("data")
        now = int(time.time() * 1000)
        self._set(last_packet_at=now)

        if cmd == "DANMU_MSG":
            if not cfg.get("record_danmaku", True):
                return
            row = build_danmaku(room_id, message.get("info"), now)
            if row:
                row["run_id"] = run_id
                row["raw_json"] = json.dumps(
                    message, ensure_ascii=False, separators=(",", ":"))[:4000]
                self._dm_buf.append(row)
                if len(self._dm_buf) >= 50:
                    self._flush_danmaku()
            return

        if not cfg.get("record_sc", True) and cmd.startswith("SUPER_CHAT"):
            return

        if cmd == "SUPER_CHAT_MESSAGE" and isinstance(data, dict):
            row = extract_sc(room_id, data, now)
            if row is None:
                return
            pending = self._pending_trans.pop(row["sc_id"], None)
            if pending and not row.get("message_trans"):
                row["message_trans"] = pending
            row["run_id"] = run_id
            if self.store.insert_sc(row):
                self._inc("sc")
                self.log("info", "SC ¥%s %s：%s" %
                         (row["price"], row["uname"], (row["message"] or "")[:40]))
            else:
                self._inc("duplicates")
            return

        if cmd == "SUPER_CHAT_MESSAGE_JPN":
            payload = data if isinstance(data, dict) else message
            sc_id = payload.get("id") or payload.get("sc_id")
            text = (payload.get("message_trans") or
                    payload.get("translation") or "").strip()
            if sc_id is None or not text:
                return
            if not self.store.update_sc_translation(sc_id, text):
                if len(self._pending_trans) > 200:
                    self._pending_trans.clear()
                self._pending_trans[str(sc_id)] = text
            self._inc("sc_trans")
            return

        if cmd == "SUPER_CHAT_MESSAGE_DELETE" and isinstance(data, dict):
            ids = data.get("ids") or ([data["id"]] if data.get("id") else [])
            self.store.mark_sc_deleted(ids)
            if cfg.get("record_events", True):
                row = event_row(room_id, "sc_delete", data, now)
                row["run_id"] = run_id
                self.store.insert_event(row)
                self._inc("events")
            return

        if cmd in ("GUARD_BUY", "SEND_GIFT") and cfg.get("record_events", True):
            kind = "guard" if cmd == "GUARD_BUY" else "gift"
            row = event_row(room_id, kind, data, now)
            row["run_id"] = run_id
            self.store.insert_event(row)
            self._inc("events")

    def _flush_danmaku(self):
        if not self._dm_buf:
            return
        rows, self._dm_buf = self._dm_buf, []
        try:
            self._inc("danmaku", self.store.insert_danmaku(rows))
        except Exception as exc:
            self.log("error", "弹幕入库失败：%s" % exc)
