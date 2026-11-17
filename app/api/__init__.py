"""Blueprint 注册入口：一个业务域一个模块，路由不再堆在一个 if-elif 里。"""
from __future__ import annotations

from flask import Flask, current_app


def rt():
    """取运行期容器（store / manager / rooms ...）。"""
    return current_app.extensions["live"]


def register(app: Flask) -> None:
    from . import (accounts, admin, capture, dynamics, feed, overview, public,
                   recordings, rooms)

    for module in (public, overview, rooms, capture, feed, recordings,
                   dynamics, accounts, admin):
        app.register_blueprint(module.bp)
