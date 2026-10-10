"""astrbot_plugin_topic_router —— 让机器人自己判断"这是新话题还是上一个话题"。

## 这个文件的写法是被实机逼出来的，改之前请先读完这段

正常情况下 AstrBot 的插件 handler 是**绑定方法**，方法体里用 `self` 拿插件状态。
但在实机（AstrBot 4.28.1 + webchat）上观测到：派发插件 handler 时实例没有被正确
绑定，`self` 拿到的是**事件对象**。证据（来自真实日志）：

    hook_llm_request_v016 被调用 | self=WebChatMessageEvent | has_event=False
    AttributeError: 'WebChatMessageEvent' object has no attribute '_request_from_event'

后果很阴险：只要方法体里写 `self.store` / `self.config`，就会抛 `AttributeError`；
而 AstrBot 的 `star_request.py` 会 `event.stop_event()`，**同一条消息排在后面的
handler 全部不再执行**——表现就是"插件明明加载了，判定日志一条都没有"。

所以本模块的约定是：

1. **状态一律放模块级单例 `RUNTIME`**，方法体只读模块级变量，绝不 `self.xxx`；
2. 共享逻辑写成**模块级函数**，上下文作为第一个参数显式传入（`ctx`）；
3. 三个 handler（`handle_message_v017` / `hook_llm_request_v017` / `command_topic_v017`）
   只做"从参数里认出对象 → 转调模块函数"，参数识别只用 `isinstance`；
4. handler 方法名**带版本号**：AstrBot 的 `get_handler_or_create` 用
   `f"{模块}_{函数名}"` 当注册表键（4.28.1 `core/star/register/star_handler.py:38`），
   键里不含版本，命中旧条目就直接复用——改名是唯一能让新代码真正生效的办法；
5. handler 内部**不允许把异常抛出去**：一旦抛出就会 `stop_event()`，连累同一条
   消息上的其它插件（这个坑我们踩过）。所有可能出错的地方自己兜住并记日志。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:  # 4.24+ 才有 TextPart.mark_as_temp
    from astrbot.core.agent.message import TextPart
except ImportError:  # pragma: no cover - 老版本降级
    TextPart = None  # type: ignore[assignment]

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    def _data_root() -> Path:
        """取 AstrBot 数据目录。

        `ASTROBOT_DATA_PATH` 优先，方便离线测试和把状态文件放到别处。

        Returns:
            数据目录路径。
        """
        override = os.environ.get("ASTROBOT_DATA_PATH")
        if override:
            return Path(override)
        return Path(get_astrbot_data_path())
except ImportError:  # pragma: no cover - 兜底

    def _data_root() -> Path:
        """取 AstrBot 数据目录（老版本兜底）。

        Returns:
            数据目录路径。
        """
        override = os.environ.get("ASTROBOT_DATA_PATH")
        if override:
            return Path(override)
        return Path.home() / ".astrbot" / "data"


from .detection import (  # noqa: E402
    CLASSIFIER_SYSTEM_PROMPT,
    VERDICT_CONTINUE,
    VERDICT_NEW_TOPIC,
    Verdict,
    build_user_prompt,
    combine,
    derive_label,
    heuristic,
    make_hint,
    parse_verdict,
)
from .state import TopicStore  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_topic_router"

MODE_OFF = "off"
MODE_MARK = "mark"
MODE_HINT = "hint"
MODE_AUTO = "auto"

# 送给判定模型的近期用户消息条数上限
RECENT_KEEP = 6


def _read_plugin_version() -> str:
    """从 metadata.yaml 读版本号。

    作用不只是好看：日常排错时"跑的是哪一版"必须能一眼确认。

    Returns:
        版本号字符串，读不到时返回 `unknown`。
    """
    try:
        text = (Path(__file__).resolve().parent / "metadata.yaml").read_text(
            encoding="utf-8"
        )
    except OSError:
        return "unknown"
    for line in text.splitlines():
        if line.strip().startswith("version:"):
            return line.split(":", 1)[1].strip().strip("'\"") or "unknown"
    return "unknown"


PLUGIN_VERSION = _read_plugin_version()


def _write_import_marker(payload: str) -> None:
    """在插件数据目录里落一个"我在这里"的标记文件。

    为什么除了日志还要文件：排查"包是新的、跑的却是旧模块"这类问题时，日志可能被
    级别、轮转或复制粘贴截断；文件是死的证据，看时间戳就行。

    Args:
        payload: 写进文件的内容。
    """
    try:
        marker_dir = _data_root() / "plugin_data" / PLUGIN_NAME
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "imported.txt").write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} {payload}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


class _Runtime:
    """插件的全部运行时状态。

    刻意放在模块级而不是 `self` 上：见文件头"这个文件的写法是被实机逼出来的"。
    """

    config: dict[str, Any] = {}
    store: TopicStore | None = None
    recent: dict[str, list[str]] = {}
    pending_actions: dict[str, tuple[str, Verdict]] = {}


RUNTIME = _Runtime()

# 插件上下文（AstrBot 构造插件时注入）。存在模块级而不是 `self` 上：实机上 handler
# 的 `self` 可能不是插件实例，见文件头说明。
_CTX: Context | None = None


def _ctx() -> Context | None:
    """取当前插件上下文。

    Returns:
        插件上下文或 None。
    """
    return _CTX



# 模块导入时无条件打一行"身份证"日志 + 落一个标记文件。排错时"到底加载的是哪个
# 文件、哪一版"必须能一眼确认。
logger.info(
    f"[{PLUGIN_NAME}] 模块已导入 {PLUGIN_VERSION} | file={Path(__file__).resolve()}"
)
_write_import_marker(f"version={PLUGIN_VERSION} file={Path(__file__).resolve()}")


# --------------------------------------------------------------------------- 工具


def _looks_like_event(candidate: Any) -> bool:
    """判断一个参数是不是消息事件。

    模块级函数，不依赖 `self`：实机上 `self` 可能就是事件对象本身。

    Args:
        candidate: 待判断的参数。

    Returns:
        True 表示看起来像消息事件。
    """
    return hasattr(candidate, "unified_msg_origin") and hasattr(
        candidate, "message_str"
    )


def _extract(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
    """从一堆参数里认出（事件, ProviderRequest）。

    只按类型/特征判断，不假设谁在第几个位置——实机上出现过 `self` 是事件、
    请求在 `args` 里、甚至零参数调用（`on_astrbot_loaded` 就是 `handler()`）的情况。
    所以这里允许零参数，认不出就返回 (None, None)。

    Args:
        *args: 任意位置参数。
        **kwargs: 关键字参数。

    Returns:
        `(event, request)`，认不出的位置是 None。
    """
    event: Any = None
    req: Any = None
    for candidate in args:
        if isinstance(candidate, ProviderRequest):
            req = req if req is not None else candidate
        elif event is None and _looks_like_event(candidate):
            event = candidate
    for key in ("event", "req", "request", "provider_request"):
        candidate = kwargs.get(key)
        if isinstance(candidate, ProviderRequest):
            req = req if req is not None else candidate
        elif event is None and _looks_like_event(candidate):
            event = candidate
    return event, req


def _mode() -> str:
    """当前生效的模式。

    Returns:
        `off` / `mark` / `hint` / `auto` 之一。
    """
    raw = str(RUNTIME.config.get("mode", MODE_MARK) or MODE_MARK).strip().lower()
    return raw if raw in {MODE_OFF, MODE_MARK, MODE_HINT, MODE_AUTO} else MODE_MARK


def _flag(key: str, default: bool) -> bool:
    """读取布尔配置。

    Args:
        key: 配置键。
        default: 缺省值。

    Returns:
        布尔值。
    """
    value = RUNTIME.config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(default if value is None else value)


def _num(key: str, default: float, cast: type = float) -> float:
    """读取数值配置，坏值退回默认。

    Args:
        key: 配置键。
        default: 缺省值。
        cast: 转换类型。

    Returns:
        数值。
    """
    try:
        return cast(RUNTIME.config.get(key, default))
    except (TypeError, ValueError):
        return cast(default)


def _umo(event: Any) -> str:
    """取统一消息来源。

    Args:
        event: 消息事件。

    Returns:
        UMO 字符串。
    """
    return str(getattr(event, "unified_msg_origin", "") or "unknown")


def _store() -> TopicStore:
    """取话题状态存储，必要时懒建。

    Returns:
        TopicStore 实例。
    """
    if RUNTIME.store is None:
        data_dir = _data_root() / "plugin_data" / PLUGIN_NAME
        RUNTIME.store = TopicStore(data_dir / "topic_state.json")
    return RUNTIME.store


def _batch_key(event: Any, message: str) -> str | None:
    """算一条消息的"批次键"，用于两个入口之间的去重。

    不能只用消息正文：用户完全可能连着发两遍一样的话，那是两轮真实对话。
    优先用平台给的消息 id / 时间戳；拿不到时返回 None（跳过去重）。

    Args:
        event: 消息事件。
        message: 消息正文。

    Returns:
        批次键或 None。
    """
    obj = getattr(event, "message_obj", None)
    if obj is not None:
        msg_id = getattr(obj, "message_id", "") or ""
        stamp = getattr(obj, "timestamp", 0) or 0
        if msg_id or stamp:
            return f"{msg_id}|{stamp}|{message[:60]}"
    return None


def _state(ctx: Context, umo: str, conv_id: str) -> Any:
    """取话题状态，必要时作废过期状态。

    Args:
        ctx: 插件上下文。
        umo: 统一消息来源。
        conv_id: 当前对话 ID。

    Returns:
        TopicState。
    """
    store = _store()
    state = store.get(umo)
    if conv_id and state.is_stale_for(conv_id):
        logger.info(
            f"[{PLUGIN_NAME}] 会话 {umo} 的对话已变更（{str(state.conv_id)[:8]} -> "
            f"{str(conv_id)[:8]}），旧话题作废"
        )
        # 只清「当前在聊什么」：累计判定次数和用户设的开关都要留着
        store.clear_topic(umo)
        RUNTIME.recent[umo] = []
        state = store.get(umo)
    if conv_id:
        state.conv_id = conv_id
    return state


async def _conversation_id(ctx: Context | None, event: Any) -> str:
    """取当前对话 ID，取不到时返回空串。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。

    Returns:
        对话 ID。
    """
    manager = getattr(ctx, "conversation_manager", None)
    if manager is None:
        return ""
    try:
        cid = await manager.get_curr_conversation_id(_umo(event))
    except Exception as exc:  # noqa: BLE001 - 会话服务异常不能影响主流程
        logger.debug(f"[{PLUGIN_NAME}] 读取当前对话失败: {exc}")
        return ""
    return str(cid or "")


def _recent_for(umo: str) -> list[str]:
    """取某会话的近期用户消息。

    Args:
        umo: 统一消息来源。

    Returns:
        消息列表（旧到新）。
    """
    return RUNTIME.recent.setdefault(umo, [])


async def _resolve_provider(ctx: Context | None, event: Any) -> tuple[str, str]:
    """决定判定用哪个模型。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。

    Returns:
        `(provider_id, 说明)`；provider_id 为空表示不可用。
    """
    want = str(RUNTIME.config.get("provider_id", "") or "").strip()
    if want:
        return want, "配置指定"
    getter = getattr(ctx, "get_current_chat_provider_id", None)
    if getter is None:
        return "", "上下文不可用"
    try:
        pid = await getter(umo=_umo(event))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[{PLUGIN_NAME}] 读取当前 provider 失败: {exc}")
        return "", "当前会话模型不可用"
    if not pid:
        return "", "当前会话没有可用模型"
    return str(pid), "跟随当前会话"


async def _ask_llm(
    ctx: Context | None,
    event: Any,
    message: str,
    state: Any,
    heuristic_result: Any,
) -> Verdict | None:
    """问一次判定模型。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。
        message: 用户最新消息。
        state: 当前话题状态。
        heuristic_result: 启发式结果（用于兜底相似度）。

    Returns:
        Verdict 或 None（任何失败都返回 None，调用方按延续处理）。
    """
    provider_id, note = await _resolve_provider(ctx, event)
    if not provider_id:
        logger.warning(f"[{PLUGIN_NAME}] 无法确定判定模型：{note}，本轮跳过模型判定")
        return None
    generate = getattr(ctx, "llm_generate", None)
    if generate is None:
        logger.warning(f"[{PLUGIN_NAME}] 上下文不支持 llm_generate，跳过模型判定")
        return None
    timeout = max(1.0, _num("timeout_seconds", 8.0))
    prompt = build_user_prompt(
        message,
        state,
        _recent_for(_umo(event)),
        int(_num("history_turns", 3, int)),
    )
    try:
        resp = await asyncio.wait_for(
            generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=CLASSIFIER_SYSTEM_PROMPT,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning(
            f"[{PLUGIN_NAME}] 判定模型 {provider_id} 超时（{timeout}s），本轮按延续处理"
        )
        return None
    except Exception as exc:  # noqa: BLE001 - 判定失败绝不影响正常回复
        logger.warning(f"[{PLUGIN_NAME}] 判定模型调用失败（{provider_id}）: {exc}")
        return None
    text = getattr(resp, "completion_text", "") or ""
    parsed = parse_verdict(text, heuristic_result)
    if parsed is None:
        logger.warning(
            f"[{PLUGIN_NAME}] 判定模型输出无法解析，按延续处理：{text[:120]!r}"
        )
    return parsed


def _apply_threshold(verdict: Verdict) -> Verdict:
    """把置信度门槛应用到判定结果上。

    低于 `min_new_topic_confidence` 的新话题判定会被压回「延续」：宁可不切。

    Args:
        verdict: 原始判定。

    Returns:
        处理后的判定。
    """
    floor = _num("min_new_topic_confidence", 0.6)
    if verdict.is_new_topic and verdict.confidence < floor:
        return Verdict(
            verdict=VERDICT_CONTINUE,
            confidence=1.0 - verdict.confidence,
            source=verdict.source,
            similarity=verdict.similarity,
            reason=(
                f"判为新话题但置信度 {verdict.confidence:.2f} < {floor:.2f}，"
                f"保守按延续处理（原判：{verdict.reason}）"
            ),
            topic_label=verdict.topic_label,
            topic_summary=verdict.topic_summary,
            details=verdict.details,
        )
    return verdict


def _under_hourly_cap(state: Any) -> bool:
    """检查本会话是否还有模型判定额度。

    Args:
        state: 话题状态。

    Returns:
        True 表示可以调用模型。
    """
    cap = int(_num("max_checks_per_hour", 30, int))
    if cap <= 0:
        return True
    used = state.checks_last_hour()
    if used >= cap:
        logger.info(
            f"[{PLUGIN_NAME}] 本会话本小时已判定 {used} 次，达到上限 {cap}，本轮只用启发式"
        )
        return False
    return True


def _log_verdict(umo: str, message: str, verdict: Verdict, state: Any) -> None:
    """把判定明细写进日志。

    日志里刻意不用 emoji：Windows 上部分控制台是 GBK，写 emoji 会让日志处理器报
    UnicodeEncodeError（在线程里静默刷屏），纯文本在任何编码下都安全。

    Args:
        umo: 统一消息来源。
        message: 用户消息。
        verdict: 判定结果。
        state: 话题状态。
    """
    if not _flag("debug_log", True):
        return
    tag = "NEW-TOPIC" if verdict.is_new_topic else "CONTINUE"
    preview = message.replace("\n", " ")[:60]
    logger.info(
        f"[{PLUGIN_NAME}] {tag} | {umo} | confidence={verdict.confidence:.2f} "
        f"| source={verdict.source} | topic={state.label or '(none)'} "
        f"| msg={preview} | {verdict.reason} | {verdict.details}"
    )


async def _detect(ctx: Context | None, event: Any, message: str) -> None:
    """对一个用户消息做话题判定并推进状态。

    全程不碰 `self`：见文件头说明。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。
        message: 用户消息正文。
    """
    if not _flag("enabled", True) or _mode() == MODE_OFF:
        return

    umo = _umo(event)
    conv_id = await _conversation_id(ctx, event)
    state = _state(ctx, umo, conv_id)
    if state.disabled:
        logger.info(f"[{PLUGIN_NAME}] 本会话已被 /topic off 关闭，跳过判定")
        return

    batch_key = _batch_key(event, message)
    if batch_key is not None and state.last_detected == batch_key:
        logger.debug(f"[{PLUGIN_NAME}] 这条消息已判定过（另一个入口先到了），跳过")
        return
    if batch_key is not None:
        state.last_detected = batch_key

    started = time.perf_counter()
    result = heuristic(
        message,
        state,
        sim_continue=_num("sim_continue", 0.5),
        sim_new_topic=_num("sim_new_topic", 0.12),
        time_gap_minutes=int(_num("time_gap_minutes", 45, int)),
        min_message_length=int(_num("min_message_length", 6, int)),
    )

    llm_verdict: Verdict | None = None
    spend_llm = _flag("always_llm", False) or result.needs_llm
    if spend_llm and _under_hourly_cap(state):
        llm_verdict = await _ask_llm(ctx, event, message, state, result)
        state.checks.append(time.time())

    verdict = combine(result, llm_verdict)
    verdict.details = {
        "heuristic": result.verdict,
        "heuristic_confidence": round(result.confidence, 3),
        "needs_llm": result.needs_llm,
        "llm_used": llm_verdict is not None,
        "mode": _mode(),
        "cost_ms": int((time.perf_counter() - started) * 1000),
    }

    verdict = _apply_threshold(verdict)
    _log_verdict(umo, message, verdict, state)

    # 状态推进：无论判定结果如何，这条消息都成为「上文」
    recent = _recent_for(umo)
    recent.append(message)
    if len(recent) > RECENT_KEEP:
        del recent[: len(recent) - RECENT_KEEP]
    state.last_user_text = message
    state.last_ts = time.time()
    state.turns += 1
    if verdict.is_new_topic:
        state.switches += 1
        # 模型没参与时用消息本身当话题名，保证「当前话题」永远不是空的
        state.label = verdict.topic_label or derive_label(message) or state.label
        if verdict.topic_summary:
            state.summary = verdict.topic_summary
    elif verdict.topic_label:
        # 模型顺手刷新了当前话题的名称/摘要，采纳它
        state.label = verdict.topic_label
        state.summary = verdict.topic_summary or state.summary
    _store().mark_dirty()
    _store().maybe_save()

    if verdict.is_new_topic and _mode() in {MODE_HINT, MODE_AUTO}:
        # 需要改写请求：等请求钩子拿到 ProviderRequest 再执行
        RUNTIME.pending_actions[umo] = (message, verdict)
        logger.info(
            f"[{PLUGIN_NAME}] {_mode()} 的请求改写已挂起，等请求钩子执行"
        )


async def _apply_request_actions(ctx: Context | None, event: Any, req: Any) -> None:
    """执行挂起的请求改写（`hint` / `auto`）。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。
        req: 即将发出的请求。
    """
    umo = _umo(event)
    pending = RUNTIME.pending_actions.pop(umo, None)
    if pending is None:
        return
    message, verdict = pending
    state = _store().get(umo)
    if not verdict.is_new_topic:
        return
    logger.info(
        f"[{PLUGIN_NAME}] 执行挂起的动作 | mode={_mode()} "
        f"| msg={message[:40]} | confidence={verdict.confidence:.2f}"
    )
    await _act(ctx, event, req, umo, state, verdict)


async def _act(
    ctx: Context | None,
    event: Any,
    req: Any,
    umo: str,
    state: Any,
    verdict: Verdict,
) -> None:
    """按模式和判定结果执行动作。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。
        req: 即将发出的请求。
        umo: 统一消息来源。
        state: 话题状态。
        verdict: 最终判定。
    """
    if not verdict.is_new_topic or req is None:
        return

    if _mode() == MODE_AUTO:
        threshold = _num("auto_threshold", 0.8)
        if verdict.confidence < threshold:
            logger.info(
                f"[{PLUGIN_NAME}] 判为新话题但置信度 {verdict.confidence:.2f} "
                f"< auto_threshold {threshold:.2f}，不自动切换"
            )
            return
        await _switch_conversation(ctx, event, req, umo, verdict)
        return

    if _mode() == MODE_HINT:
        hint = make_hint(verdict)
        if hint and TextPart is not None:
            try:
                req.extra_user_content_parts.append(
                    TextPart(text=hint).mark_as_temp()
                )
                logger.info(
                    f"[{PLUGIN_NAME}] 已给本轮注入新话题提示"
                    f"（{verdict.topic_label or '未命名'}）"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 注入提示失败: {exc}")
        return

    # mark：什么都不改，日志已经写了


async def _switch_conversation(
    ctx: Context | None,
    event: Any,
    req: Any,
    umo: str,
    verdict: Verdict,
) -> None:
    """新建一个对话并把本轮请求挂过去。

    Args:
        ctx: 插件上下文，可能为 None。
        event: 消息事件。
        req: 即将发出的请求。
        umo: 统一消息来源。
        verdict: 最终判定。
    """
    manager = getattr(ctx, "conversation_manager", None)
    if manager is None:
        logger.warning(f"[{PLUGIN_NAME}] 没有会话管理器，无法切换对话")
        return
    persona_id = None
    try:
        if req.conversation is not None:
            persona_id = getattr(req.conversation, "persona_id", None)
    except Exception:  # noqa: BLE001
        persona_id = None
    try:
        new_cid = await manager.new_conversation(
            umo,
            event.get_platform_id(),
            title=(verdict.topic_label or None),
            persona_id=persona_id,
        )
    except Exception as exc:  # noqa: BLE001 - 切换失败时保持原对话继续
        logger.error(f"[{PLUGIN_NAME}] 新建对话失败，本轮保持原上下文: {exc}")
        return

    # 让本轮消息存进新对话，并从空上下文开始
    try:
        conversation = await manager.get_conversation(umo, new_cid)
        if conversation is not None:
            req.conversation = conversation
        req.contexts = []
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[{PLUGIN_NAME}] 新对话 {str(new_cid)[:8]} 已创建，但绑定请求失败: {exc}"
        )

    state = _store().get(umo)
    state.conv_id = str(new_cid)
    state.label = verdict.topic_label or state.label
    state.summary = verdict.topic_summary or ""
    state.turns = 0
    RUNTIME.recent[umo] = [state.last_user_text] if state.last_user_text else []
    _store().mark_dirty()

    logger.info(
        f"[{PLUGIN_NAME}] 已自动开启新对话 {str(new_cid)[:8]}"
        f"（话题：{verdict.topic_label or '未命名'}，置信度 {verdict.confidence:.2f}）"
    )

    if _flag("new_conversation_notice", True):
        try:
            await event.send(
                event.plain_result(
                    f"检测到新话题：{verdict.topic_label or '未命名'}，已为你开一段新对话。"
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 发送新话题提示失败: {exc}")


# --------------------------------------------------------------------------- 插件


class TopicRouterPlugin(Star):
    """话题路由器插件本体。

    三个 handler 都只负责"认参数 → 转调模块级函数"，方法体里不出现任何 `self.xxx`
    （原因见文件头）。判定逻辑全在模块级函数里，因此可离线测试。
    """

    def __init__(self, context: Context | None = None, config: dict | None = None) -> None:
        """初始化插件。

        Args:
            context: AstrBot 注入的插件上下文。
            config: 插件配置（来自 WebUI）。
        """
        try:
            super().__init__(context)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - 构造失败也要能给出线索
            logger.warning(f"[{PLUGIN_NAME}] Star 基类初始化异常: {exc}")

        RUNTIME.config = config if isinstance(config, dict) else {}
        # 上下文也放模块级：handler 的 self 可能不是本实例
        global _CTX
        _CTX = context
        data_dir = _data_root() / "plugin_data" / PLUGIN_NAME
        RUNTIME.store = TopicStore(data_dir / "topic_state.json")
        RUNTIME.recent = {}
        RUNTIME.pending_actions = {}

        logger.info(
            f"[{PLUGIN_NAME}] 实例已构造 {PLUGIN_VERSION} "
            f"| file={Path(__file__).resolve()} | mode={_mode()}"
        )

    async def terminate(self) -> None:
        """插件卸载/停用时保存状态。"""
        try:
            _store().save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 退出时保存状态失败: {exc}")


# ===========================================================================
# 下面四个 handler 是**模块级函数**，故意不写成类的方法。
#
# 为什么（实机证据，别再改回方法）：
#
#   core.core_lifecycle:372  hook(on_astrbot_loaded) -> None - on_loaded_v017
#   TypeError: TopicRouterPlugin.on_loaded_v017() missing 1 required positional argument: 'self'
#
# AstrBot 的调用是 `await handler.handler()`（`core/core_lifecycle.py:375`），**不传
# self**。正常环境里注册表存的是**绑定方法**（self 已绑定），所以没事；但在这个环境
# 里注册进去的是**未绑定的类函数**，于是：
#
#   * 要求 self 的 handler 直接报 missing 'self'（上面那条）；
#   * `(self, event, req)` 这种签名会报 missing 'req'（event 落进 self）；
#   * `(self, *args)` 会把事件塞进 self（日志里 self=WebChatMessageEvent）。
#
# 模块级函数没有 self 这个位置，AstrBot 怎么调都落对：调用方多传的 self 会被
# `*args/**kwargs` 吃掉，事件与请求由 `_extract` 按类型认出来。
# ===========================================================================


@filter.event_message_type(filter.EventMessageType.ALL)
async def handle_message_v017(*args: Any, **kwargs: Any):
    """主判定入口：消息事件。

    **入口日志是必需的**：AstrBot 的 `star_request.py` 是
    `for handler in activated_handlers: if event.is_stopped(): break`，任何一个
    handler 抛异常都会 `stop_event()`，排在后面的 handler 全部不执行。所以这里
    既要在入口留下证据，也要自己兜住所有异常。

    Args:
        *args: 派发方给的位置参数（事件，或先来一个被误当 self 的对象）。
        **kwargs: 关键字形式的情况。

    Yields:
        None: 只做判定与记录，不占用消息（不回复、不阻断）。
    """
    event, _req = _extract(*args, **kwargs)
    message = (getattr(event, "message_str", "") or "").strip()
    stopped = "?"
    try:
        stopped = str(event.is_stopped())
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        f"[{PLUGIN_NAME}] handle_message_v017 被调用 | {PLUGIN_VERSION} "
        f"| umo={getattr(event, 'unified_msg_origin', '?')} "
        f"| msg={message[:40]!r} | stopped={stopped} "
        f"| args={[type(a).__name__ for a in args]}"
    )
    if event is not None and message:
        try:
            await _detect(_ctx(), event, message)
        except Exception as exc:  # noqa: BLE001
            # 判定层绝不能把异常抛出去：会被 stop_event()，连累同一条消息上的
            # 其它插件（这个坑我们踩过）。
            logger.error(
                f"[{PLUGIN_NAME}] 判定异常（已兜住，不影响其它插件）：{exc}",
                exc_info=True,
            )
    return
    yield  # pragma: no cover - 让它是异步生成器，框架两种形态都接受


@filter.on_llm_request()
async def hook_llm_request_v017(*args: Any, **kwargs: Any) -> None:
    """可选增强：能拿到 `ProviderRequest` 时执行 `hint` / `auto` 的请求改写。

    判定不依赖这里（见 `handle_message_v017`）。拿不到请求就安静返回：实机上这个
    钩子只拿到 `ProviderRequest`、拿不到事件（`has_event=False`）。

    Args:
        *args: 派发方给的位置参数。
        **kwargs: 关键字形式的情况。
    """
    event, req = _extract(*args, **kwargs)
    logger.info(
        f"[{PLUGIN_NAME}] hook_llm_request_v017 被调用 | {PLUGIN_VERSION} "
        f"| has_event={event is not None} | has_request={req is not None} "
        f"| args={[type(a).__name__ for a in args]} | kwargs={sorted(kwargs)}"
    )
    if event is None or req is None:
        return
    try:
        await _apply_request_actions(_ctx(), event, req)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[{PLUGIN_NAME}] 请求改写被跳过：{type(exc).__name__}: {exc}")


@filter.command("topicweave", alias={"话题织机"})
async def command_topic_v017(*args: Any, **kwargs: Any):
    """查看/管理当前会话的话题状态。

    指令名为什么不是 `topic`：`topic` 太通用，AstrBot 的「检测到指令冲突」会把它和
    别的插件判成同一指令、一次触发多个插件响应。别名只留一个中文长名（`话题织机`）：
    `topicrouter` 这类英文常用词同样会撞名（实测踩过）。

    Args:
        *args: 派发方给的位置参数（事件）。

    Yields:
        指令回复。
    """
    event, _req = _extract(*args, **kwargs)
    if event is None:
        return
    try:
        result = await _topic_status(_ctx(), event)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[{PLUGIN_NAME}] /topicweave 执行失败: {exc}", exc_info=True)
        return
    yield event.plain_result(result)


@filter.on_astrbot_loaded()
async def on_loaded_v017(*args: Any, **kwargs: Any) -> None:
    """启动后把可用的模型 ID 打进日志，方便填 provider_id。

    签名不带任何必填参数：实机上是 `await handler.handler()` 直接调用的（连 self 都
    不传），多一个必填参数就会 `missing ...` 报错。

    Args:
        *args: 派发方给的位置参数。
        **kwargs: 关键字形式的情况。
    """
    ids: list[str] = []
    try:
        manager = getattr(_ctx(), "provider_manager", None)
        for item in list(getattr(manager, "provider_insts", []) or []):
            conf = getattr(item, "provider_config", {}) or {}
            pid = str(conf.get("id", "") or "")
            if pid:
                ids.append(pid)
    except Exception as exc:  # noqa: BLE001 - 只是诊断信息
        logger.debug(f"[{PLUGIN_NAME}] 枚举 provider 失败: {exc}")

    logger.info(
        f"[{PLUGIN_NAME}] 已加载 {PLUGIN_VERSION} | 模式 {_mode()} | 判定模型 "
        f"{RUNTIME.config.get('provider_id') or '（跟随当前会话）'} "
        f"| 状态文件 {_store().path}"
    )
    if ids:
        logger.info(
            f"[{PLUGIN_NAME}] 可用的模型 ID（可直接填进 provider_id）："
            + "、".join(ids[:20])
        )


# --------------------------------------------------------------------- 工具


async def _topic_status(ctx: Context | None, event: Any) -> str:
    """处理 `/topic` 指令并返回要发送的文本。

    Args:
        ctx: 插件上下文。
        event: 消息事件。

    Returns:
        回复文本。
    """
    raw = (getattr(event, "message_str", "") or "").strip()
    parts = raw.split(maxsplit=2)
    action = parts[1].strip().lower() if len(parts) > 1 else ""
    argument = parts[2].strip() if len(parts) > 2 else ""
    umo = _umo(event)
    conv_id = await _conversation_id(ctx, event)
    state = _state(ctx, umo, conv_id)
    store = _store()

    if action in {"", "status", "状态"}:
        return "\n".join(
            [
                f"当前话题状态（插件 {PLUGIN_VERSION}）",
                f"话题：{state.label or '（还没建立）'}",
                f"摘要：{state.summary or '（无）'}",
                f"当前话题已聊：{state.turns} 轮；累计判定新话题：{state.switches} 次",
                f"本小时模型判定：{state.checks_last_hour()} 次",
                f"本会话自动判断：{'已关闭' if state.disabled else '开启'}",
                f"模式：{_mode()}",
            ]
        )

    manager = getattr(ctx, "conversation_manager", None)

    if action in {"new", "切换", "新话题"}:
        label = argument or "手动切换的话题"
        if manager is None:
            return "没有可用的会话管理器，无法新建对话。"
        cid = await manager.new_conversation(umo, event.get_platform_id())
        state = store.reset(umo)
        state.conv_id = str(cid)
        state.label = label
        state.last_ts = time.time()
        RUNTIME.recent[umo] = []
        store.mark_dirty()
        store.save()
        return f"已开始新对话（{str(cid)[:8]}），话题：{label}"

    if action in {"forget", "忘记", "clear"}:
        store.reset(umo)
        RUNTIME.recent[umo] = []
        store.save()
        return "已忘记当前话题，下一条消息会重新建档。"

    if action in {"off", "关闭"}:
        state.disabled = True
        store.mark_dirty()
        store.save()
        return "本会话已关闭话题自动判断。"

    if action in {"on", "开启"}:
        state.disabled = False
        store.mark_dirty()
        store.save()
        return "本会话已开启话题自动判断。"

    if action in {"conflicts", "冲突"}:
        return _command_conflicts()

    return (
        "用法：/topicweave 查看状态；/topicweave new [名称] 手动翻篇；"
        "/topicweave forget 忘记当前话题；/topicweave off|on 本会话开关；"
        "/topicweave conflicts 列出指令冲突。"
    )


def _command_conflicts() -> str:
    """列出注册表里所有指令冲突（含是哪个插件注册的）。

    AstrBot 面板会提示"检测到指令冲突"，但不一定方便复制。这个函数直接读内存里的
    handler 注册表，把冲突双方的**插件名 + 指令名**打出来，便于定位该改谁。

    返回内容同时写进日志（面板提示往往看不到细节）。

    Returns:
        可读的文本报告。
    """
    try:
        from astrbot.core.star.filter.command import CommandFilter
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import star_handlers_registry
    except Exception as exc:  # noqa: BLE001
        return f"无法读取注册表：{exc}"

    groups: dict[str, list[str]] = {}
    for handler in star_handlers_registry:
        names: list[str] = []
        for flt in getattr(handler, "event_filters", []) or []:
            if isinstance(flt, CommandFilter):
                base = getattr(flt, "command_name", None) or getattr(
                    flt, "command", None
                )
                if base:
                    names.append(str(base))
                for alias in getattr(flt, "alias", None) or []:
                    names.append(str(alias))
        if not names:
            continue
        module_path = getattr(handler, "handler_module_path", "") or ""
        meta = star_map.get(module_path)
        plugin = getattr(meta, "name", None) or module_path
        for name in names:
            groups.setdefault(name, []).append(
                f"{plugin}.{getattr(handler, 'handler_name', '?')}"
            )

    conflicts = {
        name: owners
        for name, owners in groups.items()
        if len({owner.split(".", 1)[0] for owner in owners}) > 1
    }

    lines = [f"指令冲突检查（插件 {PLUGIN_VERSION}）"]
    lines.append(f"注册表里的指令名共 {len(groups)} 个，冲突 {len(conflicts)} 个")
    if not conflicts:
        lines.append("当前没有冲突。")
    for name, owners in sorted(conflicts.items()):
        lines.append(f"· /{name} ← " + "、".join(sorted(set(owners))))
    report = "\n".join(lines)
    logger.info(f"[{PLUGIN_NAME}] {report.replace(chr(10), ' | ')}")
    return report


__all__ = ["PLUGIN_NAME", "PLUGIN_VERSION", "TopicRouterPlugin"]
