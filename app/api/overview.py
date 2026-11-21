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

    页面是给写代码的人看的，所以一条日志除了时间与文本，还带 `src`
    （文件:行号）和 error 的 `tb`（完整 traceback）。
    """
    r = rt()
    items = r.logs.tail(int_arg("limit", 300), arg("level"), arg("q"),
                        int_arg("since", 0))
    return {"ok": True, "items": items, "stats": r.logs.stats(),
            "diagnostics": r.diagnostics()}


@bp.get("/logs/file")
def logs_file():
    """下载应用自己写的完整日志（data/logs/service.log，按大小轮转）。"""
    r = rt()
    path = os.path.join(r.data_dir, "logs", "service.log")
    if not os.path.isfile(path):
        raise ApiError("还没有日志文件", 404)
    return send_file(path, mimetype="text/plain; charset=utf-8",
                     as_attachment=True, download_name="service.log")
