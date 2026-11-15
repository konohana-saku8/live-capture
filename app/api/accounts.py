"""B 站 cookie 账号池（CSV 粘贴导入）。"""
from __future__ import annotations

from flask import Blueprint

from ..http import ApiError, body
from . import rt

bp = Blueprint("accounts", __name__, url_prefix="/api")


@bp.get("/accounts")
def list_accounts():
    return rt().accounts.list()


@bp.post("/accounts")
def add_account():
    r = rt()
    data = body()
    try:
        item, created, error = r.accounts.add(
            data.get("name") or "", data.get("csv") or "",
            activate=data.get("activate", True) is not False)
    except ValueError as exc:
        raise ApiError(str(exc))
    r.logs.add("info", "%s cookie 账号：%s%s" % (
        "新增" if created else "更新", item["label"],
        "（%s）" % error if error else ""))
    return {"ok": True, "item": item, "accounts": r.accounts.list(),
            "restart_required": r.manager.snapshot()["running"]}


@bp.post("/accounts/remove")
def remove_account():
    r = rt()
    removed = r.accounts.remove(body().get("id") or "")
    r.logs.add("info", "删除 cookie 账号：%s" % ("成功" if removed else "账号不存在"))
    return {"ok": removed, "accounts": r.accounts.list(),
            "restart_required": r.manager.snapshot()["running"]}


@bp.post("/accounts/activate")
def activate_account():
    r = rt()
    try:
        out, label = r.accounts.activate(body().get("id") or "")
    except ValueError as exc:
        raise ApiError(str(exc))
    r.logs.add("info", "启用 cookie 账号：%s" % (label or "已停用（回退匿名采集）"))
    return {"ok": True, "accounts": out,
            "restart_required": r.manager.snapshot()["running"]}
