"""离线测试：不启动 AstrBot，用假 context/event 跑通判定与动作层。

跑法（用 AstrBot 自带的 Python 运行时，才能 import 到真实的 astrbot 模块）：

    & "$env:LOCALAPPDATA\\AstrBot\\backend\\python\\python.exe" tests/run_tests.py

它做四件事：
1. 按 AstrBot 真实的加载方式（`data.plugins.<目录名>.main`）导入插件，验证相对导入、
   `metadata.yaml`、`_conf_schema.json` 都是 AstrBot 认的格式；
2. 用真实 `ProviderRequest` / `TextPart` 对象跑 mark / hint / auto 三种模式；
3. 覆盖两个入口的组合：消息事件（主判定）与 LLM 请求钩子（可选的请求改写）、
   同一条消息重复到达时的去重；
4. 覆盖状态作废、每小时上限、置信度门槛、模型异常/超时、`/topic` 指令等边界。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

# 每个用例换一个数据目录，避免状态文件串味（第一版就栽在这里）
_DATA_ROOT = Path(tempfile.mkdtemp(prefix="topic-router-data-"))
os.environ["ASTROBOT_DATA_PATH"] = str(_DATA_ROOT)

FAILED: list[str] = []
PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果。

    Args:
        name: 用例名。
        condition: 是否通过。
        detail: 失败时的补充信息。
    """
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [ok]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def fresh_data_root(label: str) -> Path:
    """给一个用例准备独立的数据目录。

    Args:
        label: 用例名，用于目录命名。

    Returns:
        新的数据目录。
    """
    path = _DATA_ROOT / label / f"run-{PASSED}-{len(FAILED)}"
    path.mkdir(parents=True, exist_ok=True)
    os.environ["ASTROBOT_DATA_PATH"] = str(path)
    reset_runtime()
    return path


def install_plugin_as_package(tmp_root: Path) -> None:
    """把插件按 AstrBot 的目录约定挂到临时目录里，便于用真实包路径导入。

    Args:
        tmp_root: 临时根目录（充当 AstrBot 的 data 目录）。
    """
    target = tmp_root / "data" / "plugins" / PLUGIN_DIR.name
    shutil.copytree(
        PLUGIN_DIR,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".git", "dist"),
    )
    sys.path.insert(0, str(tmp_root))


# --------------------------------------------------------------------------- 假对象


class FakeConversationManager:
    """只实现插件用到的那几个方法。"""

    def __init__(self) -> None:
        """初始化。"""
        self.conversations: dict[str, SimpleNamespace] = {}
        self.current: dict[str, str] = {}
        self.new_calls: list[dict] = []

    async def get_curr_conversation_id(self, umo: str) -> str | None:
        """取当前对话 ID。

        Args:
            umo: 统一消息来源。

        Returns:
            对话 ID。
        """
        return self.current.get(umo)

    async def new_conversation(
        self,
        umo: str,
        platform_id: str | None = None,
        content=None,
        title=None,
        persona_id=None,
    ) -> str:
        """新建对话。

        Args:
            umo: 统一消息来源。
            platform_id: 平台 ID。
            content: 初始内容。
            title: 标题。
            persona_id: 人格 ID。

        Returns:
            新对话 ID。
        """
        cid = f"conv-{len(self.conversations) + 1:04d}"
        self.conversations[cid] = SimpleNamespace(
            cid=cid, history="[]", persona_id=persona_id, title=title
        )
        self.current[umo] = cid
        self.new_calls.append(
            {
                "umo": umo,
                "platform_id": platform_id,
                "title": title,
                "persona_id": persona_id,
            }
        )
        return cid

    async def get_conversation(
        self, umo: str, cid: str, create_if_not_exists: bool = False
    ):
        """取对话对象。

        Args:
            umo: 统一消息来源。
            cid: 对话 ID。
            create_if_not_exists: 不存在时是否创建。

        Returns:
            对话对象或 None。
        """
        return self.conversations.get(cid)


class FakeProvider:
    """假的 LLM provider。"""

    def __init__(self, provider_id: str) -> None:
        """初始化。

        Args:
            provider_id: provider ID。
        """
        self.provider_config = {"id": provider_id}


class FakeProviderManager:
    """假的 provider 管理器。"""

    def __init__(self) -> None:
        """初始化。"""
        self.provider_insts = [
            FakeProvider("deepseek/deepseek-v4-flash"),
            FakeProvider("nvidia/deepseek-ai/deepseek-v4-pro"),
        ]


class FakeContext:
    """假的插件上下文。"""

    def __init__(self) -> None:
        """初始化。"""
        self.conversation_manager = FakeConversationManager()
        self.provider_manager = FakeProviderManager()
        self.calls: list[dict] = []
        self.answer = (
            '{"verdict":"new_topic","confidence":0.9,"topic_name":"电路板",'
            '"topic_summary":"在聊电子吧唧的板子","reason":"换了件事"}'
        )
        self.fail: Exception | None = None

    async def llm_generate(self, **kwargs):
        """假装调用模型。

        Args:
            kwargs: 调用参数。

        Returns:
            带 completion_text 的对象。

        Raises:
            Exception: 由 self.fail 指定时抛出。
        """
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(completion_text=self.answer)

    async def get_current_chat_provider_id(self, umo: str) -> str:
        """返回当前会话模型 ID。

        Args:
            umo: 统一消息来源。

        Returns:
            provider ID。
        """
        return "deepseek/deepseek-v4-flash"


class FakeEvent:
    """假的消息事件。"""

    _seq = 0

    def __init__(
        self,
        text: str,
        umo: str = "aiocqhttp:group:123",
        message_id: str | None = None,
        timestamp: int | None = None,
    ) -> None:
        """初始化。

        Args:
            text: 消息文本。
            umo: 统一消息来源。
            message_id: 平台消息 ID；不传时自动生成一个递增 ID。
            timestamp: 消息时间戳；不传时用当前时间。
        """
        FakeEvent._seq += 1
        self.message_str = text
        self.unified_msg_origin = umo
        self.sent: list[str] = []
        self.extras: dict = {}
        # 真实的 AstrMessageEvent 带 message_obj（含 message_id / timestamp），
        # 去重逻辑依赖它；这里照实模拟。
        self.message_obj = SimpleNamespace(
            message_id=message_id if message_id is not None else f"m{FakeEvent._seq}",
            timestamp=timestamp if timestamp is not None else FakeEvent._seq,
        )

    def get_platform_id(self) -> str:
        """平台 ID。

        Returns:
            平台标识。
        """
        return "aiocqhttp"

    def plain_result(self, text: str):
        """构造纯文本结果。

        Args:
            text: 文本。

        Returns:
            简单对象。
        """
        return {"text": text}

    async def send(self, result) -> None:
        """记录发送内容。

        Args:
            result: 结果对象。
        """
        self.sent.append(result)

    def set_extra(self, key: str, value) -> None:
        """存额外数据。

        Args:
            key: 键。
            value: 值。
        """
        self.extras[key] = value

    def get_extra(self, key: str, default=None):
        """取额外数据。

        Args:
            key: 键。
            default: 默认值。

        Returns:
            值。
        """
        return self.extras.get(key, default)


# --------------------------------------------------------------------------- 用例


def make_plugin(module, context: FakeContext, config: dict):
    """构造插件实例。

    Args:
        module: 插件模块。
        context: 假上下文。
        config: 配置。

    Returns:
        插件实例。
    """
    return module.TopicRouterPlugin(context, config)


MODULE = None


def state_store():
    """取当前模块级话题状态存储。

    新架构里 store 不再挂在插件实例上，测试也要跟着改。

    Returns:
        TopicStore 实例。
    """
    return MODULE.RUNTIME.store


def reset_runtime() -> None:
    """把模块级运行时状态清空。

    新架构的状态放在模块级单例 RUNTIME 里（不再挂在实例上，因为实机派发 handler
    时 self 可能不是插件实例），所以每个用例必须显式重置，否则会串味。
    """
    if MODULE is None:
        return
    module = MODULE
    data_dir = (
        Path(os.environ["ASTROBOT_DATA_PATH"]) / "plugin_data" / module.PLUGIN_NAME
    )
    module.RUNTIME.config = {}
    module.RUNTIME.store = module.TopicStore(data_dir / "topic_state.json")
    module.RUNTIME.recent = {}
    module.RUNTIME.pending_actions = {}


async def run_detect(plugin, event, req=None):
    """按实机的调用顺序驱动一次判定。

    实机上有两个入口：

    * 消息事件（`on_message`）—— 主判定入口，调用约定确定，永远只传 event；
    * LLM 请求钩子（`on_llm_request`）—— 只负责在能拿到 `ProviderRequest` 时
      执行 `hint` / `auto` 需要的请求改写。

    所以这里先跑消息事件，再（可选地）跑钩子，模拟真实顺序。

    Args:
        plugin: 插件实例。
        event: 消息事件。
        req: `ProviderRequest`，为 None 时只走消息事件路径。
    """
    async for _ in plugin.handle_message_v017(event):
        pass
    if req is not None:
        await plugin.hook_llm_request_v017(event, req)


async def test_metadata(module, tmp_root: Path) -> None:
    """校验 metadata 与配置 schema。

    Args:
        module: 插件模块。
        tmp_root: 临时根目录。
    """
    print("\n[1] 加载与元数据")
    check("按 data.plugins.<name>.main 导入成功", module is not None)

    import yaml

    meta = yaml.safe_load((PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    check(
        "metadata.name 与目录名一致",
        meta.get("name") == PLUGIN_DIR.name,
        f"got {meta.get('name')}",
    )
    check("metadata.version 存在", bool(meta.get("version")))
    check("插件导出 PLUGIN_VERSION", bool(getattr(module, "PLUGIN_VERSION", "")))

    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    check("schema 默认 mode 是 mark", schema["mode"]["default"] == "mark")
    for key, item in schema.items():
        ok = (
            isinstance(item, dict)
            and "description" in item
            and "default" in item
            and "type" in item
        )
        check(f"schema.{key} 字段合法", ok, str(item)[:80])
    check(
        "mode 选项包含四档",
        schema["mode"]["options"] == ["off", "mark", "hint", "auto"],
        str(schema["mode"].get("options")),
    )


async def test_heuristics(module) -> None:
    """校验零成本启发式层。

    Args:
        module: 插件模块。
    """
    print("\n[2] 启发式判定")
    detection = sys.modules["data.plugins.astrbot_plugin_topic_router.detection"]
    state = SimpleNamespace(
        label="电子吧唧的电路板设计",
        summary="在聊怎么画板子、选元件",
        last_user_text="那这个电容选多大的？",
        last_ts=1000.0,
    )

    first = detection.heuristic(
        "你好", SimpleNamespace(label="", summary="", last_user_text="", last_ts=0.0)
    )
    check("首个话题直接建档", first.verdict == detection.VERDICT_NEW_TOPIC and first.needs_llm)

    short = detection.heuristic("嗯", state, now=1001.0)
    check("短消息不调用模型", short.verdict == detection.VERDICT_CONTINUE and not short.needs_llm)

    same = detection.heuristic("那这个电容选多大的？还有电阻呢", state, now=1002.0)
    check(
        "高度重叠判延续",
        same.verdict == detection.VERDICT_CONTINUE and not same.needs_llm,
        same.reason,
    )

    other = detection.heuristic("帮我写个 Python 脚本把 csv 转成 excel", state, now=1002.0)
    check("几乎不重叠判新话题", other.verdict == detection.VERDICT_NEW_TOPIC, other.reason)

    marked = detection.heuristic("换个话题，帮我看看 NAS 的硬盘", state, now=1002.0)
    check("显式换话题词直接定性", marked.verdict == detection.VERDICT_NEW_TOPIC and not marked.needs_llm)

    cold = detection.heuristic("今天天气怎么样", state, now=1000.0 + 3600, time_gap_minutes=45)
    check("冷启动 + 低重叠 = 新话题", cold.verdict == detection.VERDICT_NEW_TOPIC, cold.reason)

    vague = detection.heuristic("那个东西再改改试试", state, now=1002.0, sim_continue=0.99)
    check(
        "模糊区间交给模型",
        vague.needs_llm and vague.verdict == detection.VERDICT_CONTINUE,
        vague.reason,
    )


async def test_json_parsing(module) -> None:
    """校验模型输出解析的健壮性。

    Args:
        module: 插件模块。
    """
    print("\n[3] 模型输出解析")
    detection = sys.modules["data.plugins.astrbot_plugin_topic_router.detection"]
    heur = detection.HeuristicResult(
        verdict=detection.VERDICT_CONTINUE,
        confidence=0.5,
        similarity=0.3,
        needs_llm=True,
        reason="x",
    )

    plain = detection.parse_verdict(
        '{"verdict":"new_topic","confidence":0.88,"topic_name":"NAS"}', heur
    )
    check("纯 JSON 可解析", plain is not None and plain.is_new_topic and plain.topic_label == "NAS")

    fenced = detection.parse_verdict(
        '好的，判定如下：\n```json\n{"verdict":"continue","confidence":0.7}\n```\n以上。', heur
    )
    check("被 markdown 包裹也能解析", fenced is not None and not fenced.is_new_topic)
    check("缺少 topic_name 不报错", fenced is not None and fenced.topic_label == "")

    bad = detection.parse_verdict("我觉得应该算延续吧", heur)
    check("没有 JSON 返回 None", bad is None)

    weird = detection.parse_verdict('{"verdict":"NEW_TOPIC","confidence":"0.9"}', heur)
    check("大写与字符串置信度可容错", weird is not None and weird.is_new_topic)

    out_of_range = detection.parse_verdict('{"verdict":"new_topic","confidence":9}', heur)
    check("置信度被夹到 0~1", out_of_range is not None and out_of_range.confidence == 1.0)

    merged = detection.combine(heur, plain)
    check("模型结果覆盖启发式", merged.is_new_topic and merged.source == "llm")

    # 不对称原则：启发式说延续时，模型必须很确定才能翻成新话题
    unsure = detection.parse_verdict('{"verdict":"new_topic","confidence":0.66}', heur)
    check("模型不够确定时能解析", unsure is not None and unsure.is_new_topic)
    check("启发式延续 + 模型半信半疑 = 延续", not detection.combine(heur, unsure).is_new_topic)

    sure = detection.parse_verdict('{"verdict":"new_topic","confidence":0.95}', heur)
    strong = detection.combine(heur, sure)
    check("模型非常确定时可以翻成新话题", strong.is_new_topic and strong.confidence <= 0.8)

    weak_new = detection.HeuristicResult(
        verdict=detection.VERDICT_NEW_TOPIC,
        confidence=0.2,
        similarity=0.1,
        needs_llm=True,
        reason="低重叠但没把握",
    )
    keep = detection.combine(weak_new, unsure)
    check(
        "两个信号一致时取较低置信度、能被阈值门挡住",
        keep.is_new_topic and keep.confidence < 0.6,
        f"{keep.verdict}/{keep.confidence}",
    )

    firm_new = detection.HeuristicResult(
        verdict=detection.VERDICT_NEW_TOPIC,
        confidence=0.7,
        similarity=0.05,
        needs_llm=True,
        reason="低重叠",
    )
    check("两个信号一致且模型有把握时判新话题", detection.combine(firm_new, plain).is_new_topic)


async def test_mark_mode(module) -> None:
    """mark 模式：只记录，绝不改请求。

    Args:
        module: 插件模块。
    """
    print("\n[4] mark 模式（默认）")
    fresh_data_root("mark")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "mark"})
    event = FakeEvent("帮我写个爬虫抓豆瓣")
    from astrbot.api.provider import ProviderRequest

    req = ProviderRequest(
        prompt=event.message_str, contexts=[{"role": "user", "content": "在聊电路板"}]
    )

    await run_detect(plugin, event, req)

    check("没有新建对话", context.conversation_manager.new_calls == [])
    check("contexts 未被改动", len(req.contexts) == 1)
    check("没有注入额外内容块", len(req.extra_user_content_parts) == 0)
    check("没有向用户发言", event.sent == [])
    check("状态里记下了话题名称", state_store().get(event.unified_msg_origin).label == "电路板")
    check("调用了判定模型", len(context.calls) == 1, str(context.calls))
    prompt = context.calls[0]["prompt"] if context.calls else ""
    check("提示词带上了最新消息", "豆瓣" in prompt, prompt[:120])
    check("判定了但没动上下文", state_store().get(event.unified_msg_origin).switches == 1)
    check(
        "同一条消息不会被判定两次（两个入口去重）",
        state_store().get(event.unified_msg_origin).turns == 1,
        f"turns={state_store().get(event.unified_msg_origin).turns}",
    )


async def test_auto_mode(module) -> None:
    """auto 模式：高置信度时切换对话。

    Args:
        module: 插件模块。
    """
    print("\n[5] auto 模式")
    fresh_data_root("auto")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "auto", "auto_threshold": 0.8})
    event = FakeEvent("帮我写个 Python 脚本")
    from astrbot.api.provider import ProviderRequest

    old = await context.conversation_manager.new_conversation(event.unified_msg_origin)
    context.calls.clear()
    calls_before = len(context.conversation_manager.new_calls)
    req = ProviderRequest(
        prompt=event.message_str,
        contexts=[{"role": "user", "content": "电路板怎么画"}],
        conversation=context.conversation_manager.conversations[old],
    )

    await run_detect(plugin, event, req)

    check("新建了一个对话", len(context.conversation_manager.new_calls) == calls_before + 1)
    new_cid = context.conversation_manager.current[event.unified_msg_origin]
    check("请求已挂到新对话", getattr(req.conversation, "cid", "") == new_cid)
    check("上下文被清空", req.contexts == [])
    check("向用户提示了换话题", len(event.sent) == 1, str(event.sent))
    check("旧对话仍在（没删）", old in context.conversation_manager.conversations)

    # 低置信度不切换
    fresh_data_root("auto-low")
    context2 = FakeContext()
    context2.answer = '{"verdict":"new_topic","confidence":0.62,"topic_name":"随便"}'
    plugin2 = make_plugin(module, context2, {"mode": "auto", "auto_threshold": 0.8})
    event2 = FakeEvent("帮我写个爬虫")
    await run_detect(plugin2, event2, ProviderRequest(prompt=event2.message_str))
    check("置信度不够时不切换", context2.conversation_manager.new_calls == [])

    # 阈值下限：低于 min_new_topic_confidence 直接压回延续
    fresh_data_root("auto-floor")
    context3 = FakeContext()
    context3.answer = '{"verdict":"new_topic","confidence":0.4,"topic_name":"随便"}'
    plugin3 = make_plugin(module, context3, {"mode": "auto", "min_new_topic_confidence": 0.6})
    event3 = FakeEvent("帮我写个爬虫")
    await run_detect(plugin3, event3, ProviderRequest(prompt=event3.message_str))
    check("低置信度被压回延续", context3.conversation_manager.new_calls == [])

    context3.answer = '{"verdict":"continue","confidence":0.9,"topic_name":"爬虫"}'
    same = "帮我写个爬虫，要支持代理"
    await run_detect(plugin3, FakeEvent(same), ProviderRequest(prompt=same))
    switches_before = state_store().get(event3.unified_msg_origin).switches
    context3.answer = '{"verdict":"new_topic","confidence":0.4,"topic_name":"随便"}'
    low = "帮我写个爬虫，再加个去重"
    await run_detect(plugin3, FakeEvent(low), ProviderRequest(prompt=low))
    check(
        "被压回的判定不计入新话题数",
        state_store().get(event3.unified_msg_origin).switches == switches_before,
        f"before={switches_before} after={state_store().get(event3.unified_msg_origin).switches}",
    )


async def test_hint_mode(module) -> None:
    """hint 模式：注入本轮临时提示，但不换对话。

    Args:
        module: 插件模块。
    """
    print("\n[6] hint 模式")
    fresh_data_root("hint")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "hint"})
    event = FakeEvent("帮我写个爬虫")
    from astrbot.api.provider import ProviderRequest

    req = ProviderRequest(
        prompt=event.message_str, contexts=[{"role": "user", "content": "旧话题"}]
    )
    await run_detect(plugin, event, req)

    check("注入了 1 个内容块", len(req.extra_user_content_parts) == 1)
    if req.extra_user_content_parts:
        part = req.extra_user_content_parts[0]
        check("注入的是临时内容（不进历史）", getattr(part, "_no_save", False) is True)
        check(
            "提示里点了新话题名",
            "电路板" in getattr(part, "text", ""),
            getattr(part, "text", "")[:80],
        )
    check("没有换对话", context.conversation_manager.new_calls == [])
    check("上下文保持原样", len(req.contexts) == 1)


async def test_request_hook_alone(module) -> None:
    """只有请求钩子被调用时（消息事件没到），判定也应该发生。

    Args:
        module: 插件模块。
    """
    print("\n[7] 只有 on_llm_request 被调用的路径")
    fresh_data_root("hook-only")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "hint"})
    umo = "aiocqhttp:group:hookonly"
    event = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    from astrbot.api.provider import ProviderRequest

    req = ProviderRequest(prompt=event.message_str)
    # 只走钩子：先挂起、再执行，等价于实机上"消息事件没触发、只有钩子"的情况
    await plugin.hook_llm_request_v017(event, req)
    # 钩子本身不做判定（判定属于消息事件），所以这里不应该有注入
    check("钩子单独调用不会凭空判定", len(req.extra_user_content_parts) == 0)

    # 走完消息事件后，挂起动作会在下一次钩子调用时执行
    async for _ in plugin.handle_message_v017(event):
        pass
    check("消息事件完成了判定", state_store().get(umo).turns == 1)
    req2 = ProviderRequest(prompt=event.message_str)
    await plugin.hook_llm_request_v017(event, req2)
    check("挂起的 hint 动作已执行", len(req2.extra_user_content_parts) == 1)

    # 同一条消息再走一遍消息事件，不应该被判定两次
    async for _ in plugin.handle_message_v017(event):
        pass
    check(
        "重复到达的消息被跳过",
        state_store().get(umo).turns == 1,
        f"turns={state_store().get(umo).turns}",
    )

    # 用户真的把同一句话又发了一遍（新的消息 id）→ 应该算新的一轮
    same_text_again = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    async for _ in plugin.handle_message_v017(same_text_again):
        pass
    check(
        "用户重发同样的话算新一轮",
        state_store().get(umo).turns == 2,
        f"turns={state_store().get(umo).turns}",
    )


async def test_state_lifecycle(module) -> None:
    """状态：对话作废、写盘、每小时上限、off 开关。

    Args:
        module: 插件模块。
    """
    print("\n[8] 状态生命周期")
    fresh_data_root("lifecycle")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "mark", "max_checks_per_hour": 2})
    umo = "aiocqhttp:group:999"
    from astrbot.api.provider import ProviderRequest

    await context.conversation_manager.new_conversation(umo)
    for text in ("帮我写个爬虫抓豆瓣", "再来个代理池", "换个话题，聊聊 NAS"):
        await run_detect(plugin, FakeEvent(text, umo), ProviderRequest(prompt=text))
    check("超过每小时上限后不再调模型", len(context.calls) == 2, f"calls={len(context.calls)}")

    state = state_store().get(umo)
    check("状态绑定了当前对话", state.conv_id == context.conversation_manager.current[umo])
    check("消息计入轮数", state.turns >= 3)
    check("三轮后话题名还在", bool(state.label), f"label={state.label!r}")

    # 换对话后旧话题作废（统计与开关要留着）
    switches_before = state_store().get(umo).switches
    context.conversation_manager.current[umo] = "manual-new"
    await run_detect(plugin, FakeEvent("全新的一句话", umo), ProviderRequest(prompt="全新的一句话"))
    check(
        "换对话后旧话题被清掉",
        state_store().get(umo).label != "电路板",
        f"label={state_store().get(umo).label}",
    )
    check("换对话不清空累计统计", state_store().get(umo).switches >= switches_before)

    # /topic off 后完全不动
    state_store().get(umo).disabled = True
    state_store().mark_dirty()
    state_store().save()
    before = len(context.calls)
    await run_detect(plugin, FakeEvent("再聊点别的", umo), ProviderRequest(prompt="再聊点别的"))
    check("off 之后不再调用模型", len(context.calls) == before)

    saved = json.loads(state_store().path.read_text(encoding="utf-8"))
    check("状态文件已写出", "states" in saved and umo in saved["states"])
    check("状态文件有版本号", saved.get("version") == 1)

    from data.plugins.astrbot_plugin_topic_router.state import TopicStore

    reloaded = TopicStore(state_store().path)
    check("重新载入后开关还在", reloaded.get(umo).disabled is True)
    check(
        "重新载入后话题还在",
        bool(reloaded.get(umo).label),
        f"内存 label={state_store().get(umo).label!r} "
        f"磁盘 label={saved['states'][umo].get('label')!r}",
    )


async def test_failure_paths(module) -> None:
    """判定模型异常时不能影响正常回复。

    Args:
        module: 插件模块。
    """
    print("\n[9] 失败路径")
    fresh_data_root("failure")
    from astrbot.api.provider import ProviderRequest

    for name, exc in (
        ("模型抛异常", RuntimeError("boom")),
        ("模型超时", TimeoutError()),
    ):
        fresh_data_root(f"failure-{name.replace(' ', '-')}")
        context = FakeContext()
        context.answer = '{"verdict":"continue","confidence":0.9,"topic_name":"爬虫"}'
        plugin = make_plugin(module, context, {"mode": "auto", "timeout_seconds": 5})
        umo = f"aiocqhttp:group:{name}"
        await run_detect(
            plugin,
            FakeEvent("帮我写个爬虫抓豆瓣", umo),
            ProviderRequest(prompt="帮我写个爬虫抓豆瓣"),
        )
        check(f"{name}前状态已建立", bool(state_store().get(umo).label))

        context.fail = exc
        context.answer = '{"verdict":"new_topic","confidence":0.95,"topic_name":"别的事"}'
        before_calls = len(context.conversation_manager.new_calls)
        event = FakeEvent("帮我写个爬虫抓豆瓣，顺便加个去重", umo)
        req = ProviderRequest(
            prompt=event.message_str, contexts=[{"role": "user", "content": "旧"}]
        )
        await run_detect(plugin, event, req)
        check(f"{name}时不换对话", len(context.conversation_manager.new_calls) == before_calls)
        check(f"{name}时不发消息", event.sent == [])
        check(f"{name}时上下文未被改动", len(req.contexts) == 1)
        check(
            f"{name}时按延续推进状态",
            state_store().get(umo).turns == 2,
            f"turns={state_store().get(umo).turns}",
        )

    fresh_data_root("failure-unparseable")
    context = FakeContext()
    context.answer = '{"verdict":"continue","confidence":0.9,"topic_name":"爬虫"}'
    plugin = make_plugin(module, context, {"mode": "auto"})
    umo = "aiocqhttp:group:unparseable"
    await run_detect(plugin, FakeEvent("帮我写个爬虫抓豆瓣", umo), ProviderRequest(prompt="帮我写个爬虫抓豆瓣"))
    context.answer = "我不知道该怎么判断"
    before_calls = len(context.conversation_manager.new_calls)
    event = FakeEvent("帮我写个爬虫抓豆瓣，再加个日志", umo)
    req = ProviderRequest(prompt=event.message_str, contexts=[{"role": "user", "content": "旧"}])
    await run_detect(plugin, event, req)
    check("输出无法解析时不换对话", len(context.conversation_manager.new_calls) == before_calls)
    check("输出无法解析时不改上下文", len(req.contexts) == 1)


async def test_topic_command(module) -> None:
    """/topic 指令。

    Args:
        module: 插件模块。
    """
    print("\n[10] /topic 指令")
    fresh_data_root("command")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "mark"})
    umo = "aiocqhttp:group:cmd"

    async def collect(message: str) -> list[str]:
        """跑一次指令并收集输出。

        Args:
            message: 指令文本。

        Returns:
            输出文本列表。
        """
        ev = FakeEvent(message, umo)
        out = []
        async for result in plugin.command_topic_v017(ev):
            out.append(result["text"])
        return out

    status = await collect("/topic")
    check("status 有输出", len(status) == 1 and "当前话题状态" in status[0])
    check("status 里带版本号", any(module.PLUGIN_VERSION in line for line in status))

    await collect("/topic new 手动话题")
    check("/topic new 建了新对话", len(context.conversation_manager.new_calls) == 1)
    check("/topic new 记下了话题名", state_store().get(umo).label == "手动话题")

    await collect("/topic off")
    check("/topic off 生效", state_store().get(umo).disabled is True)
    await collect("/topic on")
    check("/topic on 生效", state_store().get(umo).disabled is False)

    await collect("/topic forget")
    check("/topic forget 清空话题", state_store().get(umo).label == "")

    unknown = await collect("/topic 胡说八道")
    check("未知子命令给用法提示", "用法" in unknown[0])


async def test_entry_points(module) -> None:
    """两个入口的签名与兼容性。

    Args:
        module: 插件模块。
    """
    print("\n[11] 入口签名与调用方式")
    import inspect

    from astrbot.api.provider import ProviderRequest

    seen = inspect.signature(module.TopicRouterPlugin.handle_message_v017)
    seen_params = list(seen.parameters.values())
    seen_required = [
        p.name
        for p in seen_params
        if p.default is inspect.Parameter.empty
        and p.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    check(
        "消息处理器除了 self 不要求任何参数（self 绑错也不会报错）",
        seen_required == ["self"],
        str(seen_required),
    )
    check(
        "消息处理器用 *args/**kwargs 接住事件",
        any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in seen_params)
        and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in seen_params),
        str([(p.name, p.kind.name) for p in seen_params]),
    )

    hook = inspect.signature(module.TopicRouterPlugin.hook_llm_request_v017)
    params = list(hook.parameters.values())
    required = [
        p.name
        for p in params
        if p.default is inspect.Parameter.empty
        and p.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    check(
        "请求钩子除了 self 不要求任何参数（少传/多传都不会报错）",
        required == ["self"],
        str(required),
    )
    check(
        "请求钩子用 *args/**kwargs 兜住任何派发方式",
        any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params)
        and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params),
        str([(p.name, p.kind.name) for p in params]),
    )
    # 这条是给未来改代码的人（包括我自己）的警告：AstrBot 按"模块+函数名"复用
    # handler 元数据，方法名一旦叫回 on_llm_request，就会命中旧条目、新代码永不生效。
    hook_name = module.TopicRouterPlugin.hook_llm_request_v017.__name__
    check("请求钩子的方法名不是 on_llm_request", hook_name != "on_llm_request", hook_name)
    check(
        "请求钩子的方法名带版本号",
        any(ch.isdigit() for ch in hook_name),
        hook_name,
    )

    umo = "aiocqhttp:group:entries"

    # 只传 event 调钩子（实机报错的那条路径）
    fresh_data_root("entries-bare")
    context = FakeContext()
    plugin = make_plugin(module, context, {"mode": "mark"})
    await plugin.hook_llm_request_v017(FakeEvent("你好", umo))
    check("钩子只传 event 不抛异常", True)

    # 位置参数 / 关键字参数里的请求都能认出来
    fresh_data_root("entries-positional")
    ctx_pos = FakeContext()
    plugin_pos = make_plugin(module, ctx_pos, {"mode": "hint"})
    ev_pos = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    real_req = ProviderRequest(prompt=ev_pos.message_str)
    async for _ in plugin_pos.handle_message_v017(ev_pos):
        pass
    await plugin_pos.hook_llm_request_v017(ev_pos, real_req)
    check(
        "位置参数里的请求被认出来",
        len(real_req.extra_user_content_parts) == 1,
        str(real_req.extra_user_content_parts),
    )

    fresh_data_root("entries-keyword")
    ctx_kw = FakeContext()
    plugin_kw = make_plugin(module, ctx_kw, {"mode": "hint"})
    ev_kw = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    kw_req = ProviderRequest(prompt=ev_kw.message_str)
    async for _ in plugin_kw.handle_message_v017(ev_kw):
        pass
    await plugin_kw.hook_llm_request_v017(ev_kw, req=kw_req)
    check(
        "关键字参数里的请求被认出来",
        len(kw_req.extra_user_content_parts) == 1,
        str(kw_req.extra_user_content_parts),
    )

    # 能从事件 extra 里找回 ProviderRequest
    # 实机现象：某些派发方式下 self 被绑成了事件对象。此时钩子既不能炸，也不能误动作。
    fresh_data_root("entries-unbound")
    ctx_unbound = FakeContext()
    plugin_unbound = make_plugin(module, ctx_unbound, {"mode": "hint"})
    ev_unbound = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    req_unbound = ProviderRequest(prompt=ev_unbound.message_str)
    async for _ in plugin_unbound.handle_message_v017(ev_unbound):
        pass
    # 直接按"未绑定函数"的方式调用：第一个位置参数会落到 self 上
    try:
        await type(plugin_unbound).hook_llm_request_v017(ev_unbound, req_unbound)
        unbound_ok = True
    except Exception as exc:  # noqa: BLE001 - 就是要看它会不会炸
        unbound_ok = False
        print(f"       [debug] 未绑定调用抛了: {type(exc).__name__}: {exc}")
    check("self 被绑成事件时钩子不抛异常", unbound_ok)
    check(
        "self 异常时不会误改请求",
        len(req_unbound.extra_user_content_parts) == 0,
        str(req_unbound.extra_user_content_parts),
    )

    # 钩子只拿到事件（没有请求对象）时安全退出
    fresh_data_root("entries-no-request")
    ctx_noreq = FakeContext()
    plugin_noreq = make_plugin(module, ctx_noreq, {"mode": "hint"})
    ev_noreq = FakeEvent("帮我写个爬虫抓豆瓣", umo)
    async for _ in plugin_noreq.handle_message_v017(ev_noreq):
        pass
    await plugin_noreq.hook_llm_request_v017(ev_noreq)
    check("钩子只拿到事件时不抛异常", True)
    check("拿不到请求时不换对话", ctx_noreq.conversation_manager.new_calls == [])


async def main() -> int:
    """跑完所有用例。

    Returns:
        进程退出码。
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="topic-router-test-"))
    try:
        install_plugin_as_package(tmp_root)
        import data.plugins.astrbot_plugin_topic_router.main as plugin_main

        global MODULE
        MODULE = sys.modules["data.plugins.astrbot_plugin_topic_router.router"]

        await test_metadata(plugin_main, tmp_root)
        await test_heuristics(plugin_main)
        await test_json_parsing(plugin_main)
        await test_mark_mode(plugin_main)
        await test_auto_mode(plugin_main)
        await test_hint_mode(plugin_main)
        await test_request_hook_alone(plugin_main)
        await test_state_lifecycle(plugin_main)
        await test_failure_paths(plugin_main)
        await test_topic_command(plugin_main)
        await test_entry_points(plugin_main)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAILED.append("测试脚本本身抛异常")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        shutil.rmtree(_DATA_ROOT, ignore_errors=True)

    print(f"\n通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
