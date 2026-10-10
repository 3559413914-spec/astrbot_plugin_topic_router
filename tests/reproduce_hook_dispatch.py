"""证明 AstrBot 的 handler 注册表会"按名字复用旧条目"，以及改名为什么能修好。

背景（实机现象）：插件从 v0.1.0 一路升到 v0.1.4，AstrBot 每次都加载了新代码
（日志里能看到 `模块已导入 v0.1.4`），但 `on_llm_request` 始终报

    TypeError: TopicRouterPlugin.on_llm_request() missing 1 required positional argument: 'req'

而 v0.1.4 里那个方法的签名是 `(self, event, *args, **kwargs)`——不可能缺参，且函数
第一行就会打日志，日志里却一次都没出现。也就是说：**被调用的不是当前类里的方法**。

根因在 AstrBot 4.28.1 的 `core/star/register/star_handler.py:38`：

    handler_full_name = f"{awaitable.__module__}_{awaitable.__name__}"
    md = star_handlers_registry.get_handler_by_full_name(handler_full_name)
    if md:
        return md          # 命中就复用旧元数据，新函数根本不注册

这个键**不含插件版本**。卸载重装时模块路径与方法名都没变，于是命中的是**第一次
安装**留下的那条记录，里面存着旧的函数对象。

本脚本用官方注册代码把这件事跑一遍：
1. 先注册一个同名（full name 相同）的旧函数 → 模拟磁盘上残留的记录；
2. 再让新版方法走同一个装饰器 → 断言拿到的仍是旧函数（复现 bug）；
3. 换一个从未注册过的名字 → 断言拿到的是新方法（证明改名可修）。

用法：
    python tests/reproduce_hook_dispatch.py
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _astrbot_paths() -> list[Path]:
    """猜出本机 AstrBot 的 import 路径。

    不写死绝对路径（那样别人 clone 下来跑不了）。查找顺序：

    1. 环境变量 `ASTRBOT_APP_PATH` / `ASTRBOT_SITE_PACKAGES`（自己指定最稳）；
    2. 桌面版默认位置（Windows / macOS / Linux）；
    3. pip 安装的 AstrBot（这时它已经在 site-packages 里，什么都不用加）。

    Returns:
        存在的候选路径列表。
    """
    candidates: list[Path] = []
    for env_key in ("ASTRBOT_APP_PATH", "ASTRBOT_SITE_PACKAGES"):
        value = os.environ.get(env_key)
        if value:
            candidates.append(Path(value))
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA", "")
    candidates += [
        Path(local) / "AstrBot" / "backend" / "app" if local else None,
        Path(local) / "AstrBot" / "backend" / "python" / "Lib" / "site-packages"
        if local
        else None,
        home / ".astrbot" / "data" / "site-packages",
        Path("/Applications/AstrBot.app/Contents/Resources/app"),
        home / ".local" / "share" / "astrbot",
    ]
    return [c for c in candidates if c is not None and c.is_dir()]


for candidate in _astrbot_paths():
    if str(candidate) not in sys.path:
        sys.path.append(str(candidate))

FAILED: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录断言结果。

    Args:
        name: 用例名。
        ok: 是否通过。
        detail: 失败补充信息。
    """
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [ok]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def main() -> int:
    """跑完复现与修复验证。

    Returns:
        退出码。
    """
    import astrbot  # noqa: PLC0415
    from astrbot.core.star.register import star_handler as reg  # noqa: PLC0415
    from astrbot.core.star.star_handler import (  # noqa: PLC0415
        EventType,
        star_handlers_registry,
    )

    print(f"astrbot: {astrbot.__file__}")
    print(f"register: {reg.__file__}")

    # ---------------------------------------------------------------- 1. 旧记录
    class OldVersion:
        """模拟"第一次安装时"留在注册表里的那条记录。"""

        async def on_llm_request(self, event, req) -> None:
            """旧版签名：req 是必填位置参数。

            Args:
                event: 消息事件。
                req: 请求对象。
            """

    old_bound = OldVersion().on_llm_request
    full_name = reg.get_handler_full_name(old_bound)
    print(f"\n旧函数的注册键: {full_name}")

    md_old = reg.get_handler_or_create(old_bound, EventType.OnLLMRequestEvent)
    print(f"旧记录已注册: {md_old.handler!r}")
    check(
        "旧记录拿到的是绑定方法（__self__ 非空）",
        getattr(md_old.handler, "__self__", None) is not None,
    )

    # ------------------------------------------------- 2. 新类同名方法被复用
    class NewVersion:
        """模拟"新装版本"里的同名方法。"""

        def __init__(self) -> None:
            """初始化。"""
            self.calls = 0

        async def on_llm_request(self, *args, **kwargs) -> None:
            """新版签名：不再有必填的 req。

            Args:
                *args: 位置参数。
                **kwargs: 关键字参数。
            """
            self.calls += 1

    # 让新类的方法与旧函数拥有同一个 full name（模块 + 函数名）
    NewVersion.on_llm_request.__module__ = OldVersion.on_llm_request.__module__
    new = NewVersion()
    md_new = reg.get_handler_or_create(new.on_llm_request, EventType.OnLLMRequestEvent)
    check(
        "复现：同名方法命中的是旧记录（新旧是同一个对象）",
        md_new is md_old,
        f"md_new is md_old -> {md_new is md_old}",
    )
    check(
        "复现：注册表里存的仍是旧签名（仍要求 req）",
        "req" in str(inspect.signature(md_new.handler)),
        str(inspect.signature(md_new.handler)),
    )
    print(f"  → 命中旧条目后，注册表里的签名: {inspect.signature(md_new.handler)}")

    # ------------------------------------------- 3. 换个全新名字：拿到新方法
    class Renamed:
        """模拟改名后的新版方法。"""

        def __init__(self) -> None:
            """初始化。"""
            self.calls = 0

        async def hook_llm_request_v015(self, event, *args, **kwargs) -> None:
            """从未注册过的新名字。

            Args:
                event: 消息事件。
                *args: 位置参数。
                **kwargs: 关键字参数。
            """
            self.calls += 1

    renamed = Renamed()
    md_fresh = reg.get_handler_or_create(
        renamed.hook_llm_request_v015, EventType.OnLLMRequestEvent
    )
    fresh_sig = inspect.signature(md_fresh.handler)
    check("改名后注册到的是新方法", md_fresh is not md_old)
    check(
        "改名后 __self__ 指向新实例",
        getattr(md_fresh.handler, "__self__", None) is renamed,
        str(type(getattr(md_fresh.handler, "__self__", None))),
    )
    check(
        "改名后签名里没有必填的 req",
        "req" not in str(fresh_sig),
        str(fresh_sig),
    )
    print(f"  → 新条目的签名: {fresh_sig}")

    # 真派发一次：新的能跑，旧的进不了函数体
    import asyncio  # noqa: PLC0415

    class _Event:
        """最小事件对象。"""

        unified_msg_origin = "test:test:test"
        plugins_name = None

        def is_stopped(self) -> bool:
            """是否被停止。

            Returns:
                False。
            """
            return False

        def get_extra(self, key, default=None):
            """取额外数据。

            Args:
                key: 键。
                default: 默认值。

            Returns:
                默认值。
            """
            return default

    async def _dispatch(handler_md) -> str:
        """按官方方式调用一次。

        Args:
            handler_md: 注册表条目。

        Returns:
            结果描述。
        """
        try:
            await handler_md.handler(_Event())
            return "ok"
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    result_old = asyncio.run(_dispatch(md_new))
    result_new = asyncio.run(_dispatch(md_fresh))
    print(f"\n  旧条目被派发（只传 event）: {result_old}")
    print(f"  新条目被派发（只传 event）: {result_new}")
    check("旧条目被派发时抛 TypeError（实机现象一致）", "TypeError" in result_old, result_old)
    check("新条目被派发时正常执行", result_new == "ok", result_new)
    check("新方法确实被执行了", renamed.calls == 1, f"calls={renamed.calls}")

    print(f"\n通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
