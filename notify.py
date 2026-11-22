# -*- coding: utf-8 -*-
"""飞书自定义机器人推送：开播 / 新动态。

只用标准库（urllib + hmac），手机 Termux 上不需要额外依赖。

飞书自定义机器人的两种可选安全设置都支持：
- 关键词 / IP 白名单：服务端配置，这里不用管；
- 签名校验：配置里填了 secret 就自动带上 timestamp + sign。

单独调试（不启动服务）：

    python notify.py --webhook https://open.feishu.cn/open-apis/bot/v2/hook/xxx
    python notify.py --webhook ... --secret ... --text "测试"
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
LIVE_URL = "https://live.bilibili.com/%d"
SPACE_URL = "https://space.bilibili.com/%d/dynamic"
TIMEOUT = 10


class NotifyError(RuntimeError):
    """推送失败（网络 / 飞书返回非 0）。"""


def sign_feishu(secret: str, ts: int | None = None) -> dict:
    """飞书签名：base64(hmac_sha256(key="<ts>\\n<secret>", msg=""))。"""
    stamp = int(ts if ts is not None else time.time())
    key = ("%d\n%s" % (stamp, secret)).encode("utf-8")
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    return {"timestamp": str(stamp),
            "sign": base64.b64encode(digest).decode("utf-8")}


def _card(template: str, title: str, lines: list[str],
          button: tuple[str, str] | None = None) -> dict:
    elements = [{"tag": "div",
                 "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    if button:
        text, url = button
        elements.append({"tag": "action", "actions": [{
            "tag": "button", "type": "primary",
            "text": {"tag": "plain_text", "content": text}, "url": url}]})
    return {"msg_type": "interactive",
            "card": {"config": {"wide_screen_mode": True},
                     "header": {"template": template,
                                "title": {"tag": "plain_text",
                                          "content": title}},
                     "elements": elements}}


def live_card(uname: str, room_id: int, title: str = "") -> dict:
    who = uname or ("房间 %s" % room_id)
    lines = ["**%s** 开播了" % who]
    if title:
        lines.append("标题：%s" % title)
    lines.append("房间：%s" % room_id)
    return _card("carmine", "%s 开播了" % who, lines,
                 ("进入直播间", LIVE_URL % room_id))


def dynamic_card(uname: str, mid: int, text: str, jump_url: str = "") -> dict:
    who = uname or ("uid %s" % mid)
    body = (text or "").strip()
    if len(body) > 500:
        body = body[:500] + "…"
    lines = ["**%s** 发了新动态" % who]
    if body:
        lines.append(body)
    return _card("blue", "%s 的新动态" % who, lines,
                 ("查看动态", jump_url or SPACE_URL % mid))


def _post(webhook: str, payload: dict, urlopen=None) -> dict:
    req = urllib.request.Request(
        webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": DEFAULT_UA},
        method="POST")
    try:
        opener = urlopen or urllib.request.urlopen
        with opener(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise NotifyError("飞书接口 HTTP %s" % exc.code) from exc
    except Exception as exc:                     # 网络不可达 / 超时
        raise NotifyError("飞书接口请求失败：%s" % exc) from exc
    try:
        data = json.loads(raw)
    except ValueError:
        raise NotifyError("飞书返回不是 JSON：%s" % raw[:200])
    # 新版返回 code/msg，老版返回 StatusCode/StatusMessage，两种都要认
    code = data.get("code", data.get("StatusCode"))
    if code not in (0, None):
        raise NotifyError("飞书返回 code=%s %s" % (
            code, data.get("msg") or data.get("StatusMessage") or ""))
    return data


def send(webhook: str, payload: dict, secret: str = "", urlopen=None) -> dict:
    """发一条消息。secret 非空时自动加签名。"""
    if not str(webhook or "").strip():
        raise NotifyError("没有配置飞书 webhook")
    body = dict(payload)
    if secret:
        body.update(sign_feishu(secret))
    return _post(webhook.strip(), body, urlopen=urlopen)


def send_text(webhook: str, text: str, secret: str = "", urlopen=None) -> dict:
    return send(webhook, {"msg_type": "text",
                          "content": {"text": text}}, secret, urlopen)


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书机器人推送自测")
    ap.add_argument("--webhook", required=True)
    ap.add_argument("--secret", default="")
    ap.add_argument("--text", default="live-capture 推送测试")
    args = ap.parse_args()
    try:
        send_text(args.webhook, args.text, args.secret)
    except NotifyError as exc:
        print("失败：%s" % exc)
        return 1
    print("已发送")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
