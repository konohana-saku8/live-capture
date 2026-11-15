"""全局配置与数据清空。"""
from __future__ import annotations

from flask import Blueprint

from ..http import ApiError, body
from ..settings import DEFAULTS, Config, as_int
from . import rt

bp = Blueprint("admin", __name__, url_prefix="/api")


@bp.get("/config")
def read_config():
    r = rt()
    return {"ok": True, "config": Config.masked(r.config.get()),
            "defaults": DEFAULTS}


@bp.post("/config")
def write_config():
    r = rt()
    data = body()
    before = r.config.get()
    after = r.config.save(data)
    r.logs.add("info", "配置已保存")
    changed = any(before.get(k) != after.get(k) for k in DEFAULTS)
    return {"ok": True, "config": Config.masked(after),
            "restart_required": changed and r.manager.snapshot()["running"]}


@bp.post("/clear")
def clear_data():
    r = rt()
    data = body()
    if data.get("confirm") != "yes":
        raise ApiError('需要 {"confirm":"yes"}')
    rid = as_int(data.get("room_id"))
    mid = as_int(data.get("mid"))
    if rid and not mid:
        mid = as_int((r.rooms.get(rid) or {}).get("uid"))
    r.store.clear(rid or None, mid)
    r.logs.add("warn", "数据已清空：%s" % ("采集器 %s" % rid if rid else "全部"))
    return {"ok": True}
