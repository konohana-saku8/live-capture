"""总览类接口：状态、仪表盘、系统负载、运行日志。"""
from __future__ import annotations

import os

from flask import Blueprint, send_file

from ..http import ApiError, arg, int_arg
from ..settings import Config
from . import rt

bp = Blueprint("overview", __name__, url_prefix="/api")


@bp.get("/status")
def status():
    r = rt()
    return {"ok": True,
            "service": r.service(),
            "capture": r.manager.snapshot(),
            "rooms": r.rooms_payload(),
            "config": Config.masked(r.config.get())}


@bp.get("/dashboard")
def dashboard():
    r = rt()
    data = r.dashboard()
    return {"ok": True,
            "totals": data["totals"], "by_room": data["by_room"],
            "top_sc": data["top_sc"], "recent": data["recent"],
            "data_status": data.get("data_status"),
            "timeline": data["timeline"],
            "last_received_at": data["last_received_at"],
            "rooms": r.rooms_payload(),
            "capture": r.manager.snapshot(),
            "service": r.service(),
            "config": Config.masked(r.config.get())}


@bp.get("/data/rooms")
def data_rooms():
    """数据页的房间选择：采集器 + 只剩数据的房间（采集器已删但数据还在）。"""
    return rt().data_rooms_payload()


@bp.get("/system")
def system():
    return {"ok": True, "system": rt().sys_snapshot()}


@bp.get("/logs")
def logs():
    """运行日志：支持按级别 / 关键词过滤，附带级别统计与诊断快照。

    传 `day=YYYY-MM-DD` 时从当天的 journal 文件读完整历史（不占内存缓冲
    ，重启也在）；不传则是内存里的实时日志。页面是给写代码的人看的，
    所以一条日志除了时间与文本，还带 `src`（文件:行号）和 error 的 `tb`。
    """
    r = rt()
    day = arg("day")
    if day and r.logs.DAY_RE.match(day):
        items = r.logs.read_day(day, int_arg("limit", 2000))
        # 文件查询也支持级别 / 关键词，和实时视图保持一致的筛选口径
        want = {x.strip() for x in arg("level").split(",") if x.strip()}
        needle = arg("q").strip().lower()
        if want and "all" not in want:
            items = [x for x in items if x["level"] in want]
        if needle:
            items = [x for x in items
                     if needle in x["msg"].lower()
                     or needle in (x.get("tb") or "").lower()
                     or needle in (x.get("src") or "").lower()]
        items = items[-int_arg("limit", 2000):]
    else:
        items = r.logs.tail(int_arg("limit", 300), arg("level"), arg("q"),
                            int_arg("since", 0))
    return {"ok": True, "items": items, "stats": r.logs.stats(),
            "day": day, "diagnostics": r.diagnostics()}


@bp.get("/logs/file")
def logs_file():
    """下载日志：当天是 service.log，历史天是 service.log.YYYY-MM-DD。"""
    r = rt()
    day = arg("day")
    name = "service.log"
    if day and r.logs.DAY_RE.match(day):
        # 当天的还在 service.log 里（TimedRotatingFileHandler 跨天才换名）
        name = ("service.log." + day
                if day != r.logs._local_day() else "service.log")
    path = os.path.join(r.data_dir, "logs", name)
    if not os.path.isfile(path):
        raise ApiError("这一天还没有日志文件", 404)
    return send_file(path, mimetype="text/plain; charset=utf-8",
                     as_attachment=True, download_name=name)
