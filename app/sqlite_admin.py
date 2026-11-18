"""把 sqlite-web 数据管理页挂到 /sqlite，与采集服务共享同一个端口。

设计取舍：
- sqlite-web 是模块级全局 Flask app，`initialize_app()` 只能在进程里调一次；
  用 DispatcherMiddleware 把它挂在主 app 前面，/sqlite/* 交给它、其余照旧。
- 登录口令复用 services 的访问码（config.json 的 token），只记一个密码。
- 依赖缺失或数据库不存在时静默降级，绝不影响采集主服务启动。
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from werkzeug.middleware.dispatcher import DispatcherMiddleware

MOUNT = "/sqlite"


def mount_sqlite_admin(app: Any, data_dir: str, token: str = "") -> Any:
    """返回包装后的 WSGI app；不可用时返回原 app。"""
    db_path = os.path.abspath(os.path.join(data_dir, "capture.db"))
    if not os.path.isfile(db_path):
        return app
    try:
        from sqlite_web import sqlite_web as sw
    except Exception:
        return app

    try:
        if not getattr(sw, "_lc_initialized", False):
            # session 需要一个跨重启稳定的 secret_key，否则每次重启都要重新登录
            seed = b"live-capture-sqlite-admin|" + db_path.encode("utf-8")
            if token:
                seed += b"|" + token.encode("utf-8")
            sw.app.secret_key = hashlib.sha256(seed).hexdigest()
            sw.initialize_app([db_path], password=token or None)
            sw._lc_initialized = True
        return DispatcherMiddleware(app, {MOUNT: sw.app})
    except Exception:
        # 管理页初始化失败也不能拖垮采集服务
        return app
