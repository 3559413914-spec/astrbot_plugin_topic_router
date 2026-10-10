"""插件入口。

AstrBot 按 `data.plugins.<目录名>.main` 加载插件，所以这里只做转出，实现都在 `router.py`。
拆成两个文件是为了让核心逻辑可以被离线测试直接 import，不用起整个 AstrBot。
"""

from __future__ import annotations

from .router import PLUGIN_NAME, PLUGIN_VERSION, TopicRouterPlugin

__all__ = ["PLUGIN_NAME", "PLUGIN_VERSION", "TopicRouterPlugin"]
