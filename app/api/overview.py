"""总览类接口：状态、仪表盘、系统负载、运行日志。"""
from __future__ import annotations

import os

from flask import Blueprint, send_file

from ..http import ApiError, int_arg
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
            "timeline": data["timeline"],
            "last_received_at": data["last_received_at"],
            "rooms": r.rooms_payload(),
            "capture": r.manager.snapshot(),
            "service": r.service(),
            "config": Config.masked(r.config.get())}


@bp.get("/system")
def system():
    return {"ok": True, "system": rt().sys_snapshot()}


@bp.get("/logs")
def logs():
    return {"ok": True, "items": rt().logs.tail(int_arg("limit", 100))}


@bp.get("/logs/file")
def logs_file():
    """下载应用自己写的完整日志（data/logs/service.log，按大小轮转）。"""
    r = rt()
    path = os.path.join(r.data_dir, "logs", "service.log")
    if not os.path.isfile(path):
        raise ApiError("还没有日志文件", 404)
    return send_file(path, mimetype="text/plain; charset=utf-8",
                     as_attachment=True, download_name="service.log")
