# -*- coding: utf-8 -*-
"""``python -m pctools`` → 图形界面。"""
from __future__ import annotations

import sys

SETUP_HINT = """没有装 PySide6。两种办法：

  1) 建独立环境（不碰 .venv-host）：
       python -m venv .venv-pc
       .venv-pc\\Scripts\\python -m pip install -r pctools/requirements.txt
       .venv-pc\\Scripts\\python -m pctools

  2) 不用界面，直接命令行：
       python -m pctools.cli --scan
       python -m pctools.cli --run
"""


def main() -> int:
    try:
        from .ui.main import main as gui_main
    except ImportError as exc:
        if "PySide6" in str(exc) or "shiboken" in str(exc):
            sys.stderr.write(SETUP_HINT)
            return 2
        raise
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
