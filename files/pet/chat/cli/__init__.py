# -*- coding: utf-8 -*-
"""本地 CLI 模型服务（WorkBuddy 自带 codebuddy）：桌宠把 CLI 只当聊天模型。

子进程与协议见 ``bridge.py``；provider 适配见 ``provider.py``。
"""
from __future__ import annotations

from .bridge import (
    CliBridge,
    CliBridgeError,
    DEFAULT_MODEL,
    DISALLOWED_TOOLS,
    build_argv,
    discover_cli,
    map_event,
)
from .provider import CliProvider

__all__ = [
    "CliBridge",
    "CliBridgeError",
    "CliProvider",
    "DEFAULT_MODEL",
    "DISALLOWED_TOOLS",
    "build_argv",
    "discover_cli",
    "map_event",
]
