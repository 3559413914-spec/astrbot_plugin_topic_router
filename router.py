"""astrbot_plugin_topic_router —— 让机器人自己判断"这是新话题还是上一个话题"。

它挂在 AstrBot 的 `on_llm_request` 钩子上（内置 Agent Runner 的必经之路）：

1. 每条用户消息到达时，先用零成本启发式判断它是不是接着上一件事说；
2. 只有落进模糊区间才调用一次便宜模型（默认 deepseek/deepseek-v4-flash）；
3. 按配置的模式行动 —— 默认 **mark**：只写日志和状态，绝不改上下文；
   确认判定准确后可以开到 **auto**：置信度达标时新建一个对话并切过去，
   旧对话完整留档，等于插件版的 `/new`。

之所以默认只标记：话题判定误判的代价全在"自动执行"那一步，它会把用户正在聊的
上下文切走。先跑几天看日志，再决定要不要放开。
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


def _read_plugin_version() -> str:
    """从 metadata.yaml 读版本号。

    作用不只是好看：日常排错时"跑的是哪一版"必须能一眼确认。上一轮就是覆盖安装
    没真正生效，日志看起来和旧版一模一样，白折腾了一轮。

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
    日志级别、日志轮转、或者你复制粘贴的片段截断；文件是死的证据，直接看时间戳就行。

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


# 模块导入时就无条件打一行"身份证"日志 + 落一个标记文件：打印这份代码在磁盘上的
# 真实路径与版本号。排错时"到底加载的是哪个文件"必须能一眼确认 —— 实测出现过
# "包是新的、跑的却是旧模块"的情况，没有这两样就只能猜。
logger.info(
    f"[{PLUGIN_NAME}] 模块已导入 {PLUGIN_VERSION} | file={Path(__file__).resolve()}"
)
_write_import_marker(f"version={PLUGIN_VERSION} file={Path(__file__).resolve()}")

# 一个会话最多保留多少条历史（超过就丢掉最旧的）
MAX_UMO_ENTRIES = 500
# 送给判定模型的近期用户消息条数上限
RECENT_KEEP = 6

MODE_OFF = "off"
MODE_MARK = "mark"
MODE_HINT = "hint"
MODE_AUTO = "auto"


class TopicRouterPlugin(Star):
    """话题路由器插件本体。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """初始化插件。

        Args:
            context: AstrBot 注入的插件上下文。
            config: 插件配置（来自 WebUI）。
        """
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        # 构造时再喊一次：这条一定会出现在插件加载日志里（`Plugin ... log level` 之后），
        # 用来证明"跑的就是这份文件"。
        logger.info(
            f"[{PLUGIN_NAME}] 实例已构造 {PLUGIN_VERSION} "
            f"| file={Path(__file__).resolve()} | mode={self.mode}"
        )
        data_dir = _data_root() / "plugin_data" / PLUGIN_NAME
        self.store = TopicStore(data_dir / "topic_state.json")
        self._recent: dict[str, list[str]] = {}
        self._model_ids: list[str] = []
        # 需要改写请求（hint / auto）但当时没拿到 ProviderRequest 时，先把
        # (消息, 判定) 挂在这里，等 on_llm_request 拿到请求时再执行。
        self._pending_actions: dict[str, tuple[str, Verdict]] = {}

    # ------------------------------------------------------------------ 配置

    @property
    def mode(self) -> str:
        """当前生效的模式。

        Returns:
            `off` / `mark` / `hint` / `auto` 之一。
        """
        raw = str(self.config.get("mode", MODE_MARK) or MODE_MARK).strip().lower()
        return raw if raw in {MODE_OFF, MODE_MARK, MODE_HINT, MODE_AUTO} else MODE_MARK

    def _flag(self, key: str, default: bool) -> bool:
        """读取布尔配置。

        Args:
            key: 配置键。
            default: 缺省值。

        Returns:
            布尔值。
        """
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(default if value is None else value)

    def _num(self, key: str, default: float, cast: type = float) -> float:
        """读取数值配置，坏值退回默认。

        Args:
            key: 配置键。
            default: 缺省值。
            cast: 转换类型。

        Returns:
            数值。
        """
        try:
            return cast(self.config.get(key, default))
        except (TypeError, ValueError):
            return cast(default)

    # ------------------------------------------------------------ 会话与状态

    @staticmethod
    def _umo(event: AstrMessageEvent) -> str:
        """取统一消息来源。

        Args:
            event: 消息事件。

        Returns:
            UMO 字符串。
        """
        return str(getattr(event, "unified_msg_origin", "") or "unknown")

    async def _conversation_id(self, event: AstrMessageEvent) -> str:
        """取当前对话 ID，取不到时返回空串。

        Args:
            event: 消息事件。

        Returns:
            对话 ID。
        """
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(
                self._umo(event)
            )
        except Exception as exc:  # noqa: BLE001 - 会话服务异常不能影响主流程
            logger.debug(f"[{PLUGIN_NAME}] 读取当前对话失败: {exc}")
            return ""
        return str(cid or "")

    def _state(self, umo: str, conv_id: str) -> Any:
        """取话题状态，必要时作废过期状态。

        Args:
            umo: 统一消息来源。
            conv_id: 当前对话 ID。

        Returns:
            TopicState。
        """
        state = self.store.get(umo)
        if conv_id and state.is_stale_for(conv_id):
            logger.info(
                f"[{PLUGIN_NAME}] 会话 {umo} 的对话已变更（{state.conv_id[:8]} -> "
                f"{conv_id[:8]}），旧话题作废"
            )
            # 只清「当前在聊什么」：累计判定次数和用户设的开关都要留着
            self.store.clear_topic(umo)
            self._recent[umo] = []
            state = self.store.get(umo)
        if conv_id:
            state.conv_id = conv_id
        return state

    def _recent_for(self, umo: str) -> list[str]:
        """取某会话的近期用户消息。

        Args:
            umo: 统一消息来源。

        Returns:
            消息列表（旧到新）。
        """
        return self._recent.setdefault(umo, [])

    # ------------------------------------------------------------ 模型判定层

    async def _resolve_provider(self, event: AstrMessageEvent) -> tuple[str, str]:
        """决定判定用哪个模型。

        Args:
            event: 消息事件。

        Returns:
            `(provider_id, 说明)`；provider_id 为空表示不可用。
        """
        want = str(self.config.get("provider_id", "") or "").strip()
        if want:
            return want, "配置指定"
        try:
            pid = await self.context.get_current_chat_provider_id(
                umo=self._umo(event)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 读取当前 provider 失败: {exc}")
            return "", "当前会话模型不可用"
        if not pid:
            return "", "当前会话没有可用模型"
        return str(pid), "跟随当前会话"

    async def _ask_llm(
        self,
        event: AstrMessageEvent,
        message: str,
        state: Any,
        heuristic_result: Any,
    ) -> Verdict | None:
        """问一次判定模型。

        Args:
            event: 消息事件。
            message: 用户最新消息。
            state: 当前话题状态。
            heuristic_result: 启发式结果（用于兜底相似度）。

        Returns:
            Verdict 或 None（任何失败都返回 None，调用方按延续处理）。
        """
        provider_id, note = await self._resolve_provider(event)
        if not provider_id:
            logger.warning(f"[{PLUGIN_NAME}] 无法确定判定模型：{note}，本轮跳过模型判定")
            return None
        timeout = max(1.0, self._num("timeout_seconds", 8.0))
        prompt = build_user_prompt(
            message,
            state,
            self._recent_for(self._umo(event)),
            int(self._num("history_turns", 3, int)),
        )
        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=self._system_prompt(),
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

    @staticmethod
    def _system_prompt() -> str:
        """判定模型的 system prompt。

        Returns:
            提示词文本。
        """
        from .detection import CLASSIFIER_SYSTEM_PROMPT

        return CLASSIFIER_SYSTEM_PROMPT

    @staticmethod
    def _request_from_event(event: AstrMessageEvent) -> ProviderRequest | None:
        """在拿不到 `req` 参数时，从事件里找 `ProviderRequest`。

        实测某些派发路径不会把 request 传给钩子，这时从事件的 extra 里取
        （AstrBot 在处理流程里会把 provider_request 挂上去）。

        Args:
            event: 消息事件。

        Returns:
            ProviderRequest，找不到返回 None。
        """
        getter = getattr(event, "get_extra", None)
        if not callable(getter):
            return None
        for key in ("provider_request", "_provider_request", "req"):
            try:
                found = getter(key, None)
            except Exception:  # noqa: BLE001 - 取不到就当没有
                continue
            if isinstance(found, ProviderRequest):
                return found
        return None

    # ------------------------------------------------------------------ 主钩子

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """主判定入口：消息事件。

        为什么不用 `on_llm_request` 当主入口：那个钩子在本机的 AstrBot 4.28.1 上
        被调用时，`star_handlers_registry` 里绑定的方法**不是本类当前定义的那个**
        （报 `missing 1 required positional argument: 'req'`，而本类签名是
        `(event, *args, **kwargs)`，不可能缺参，且只要进函数就会打日志——日志里
        一次都没有）。参数绑定在进函数之前就失败了，插件侧无从修复。

        消息事件的调用约定是确定的（永远 `handler(event)`），AstrBot 里所有插件都
        在用它，所以把它作为**唯一可靠的判定入口**：判定、状态维护、日志、`/topic`
        状态全部由这里驱动。需要改写请求的 `hint` / `auto` 仍然由 `on_llm_request`
        在能拿到 `ProviderRequest` 时补上（见 `_pending_actions`）。

        Args:
            event: 消息事件。

        Yields:
            None: 本处理器只做判定与记录，不占用消息（不回复、不阻断）。
        """
        message = (getattr(event, "message_str", "") or "").strip()
        if message:
            await self._detect(event, message, None)
        return
        yield  # pragma: no cover - 让它是异步生成器，框架两种形态都接受

    @filter.on_llm_request()
    async def hook_llm_request_v015(self, *args: Any, **kwargs: Any) -> None:
        """可选增强入口：能拿到 `ProviderRequest` 时执行 `hint` / `auto` 的请求改写。

        **方法名为什么带版本号（重要）**：AstrBot 的 `get_handler_or_create`
        （4.28.1 `core/star/register/star_handler.py:38`）用

            handler_full_name = f"{模块}_{函数名}"

        作为注册表键，键里**不含插件版本**；命中已有条目时它直接 `return md`，
        复用旧元数据。也就是说：只要方法还叫 `on_llm_request`，卸载重装后注册表里
        命中的仍是**第一次安装时**那条记录，指向旧版函数对象——新代码永远不会被
        调用（实机表现：函数入口的日志一次都不出现，却报旧签名的 TypeError）。
        改成一个从未注册过的名字，就能拿到全新条目、绑定到本类当前的方法。

        Args:
            event: 消息事件。
            *args: 派发方可能塞进来的 `ProviderRequest` 等额外参数。
            **kwargs: 同上，按关键字传的情况。
        """
        event = args[0] if args else kwargs.get("event")
        if event is None:
            return
        rest = args[1:]

        req: ProviderRequest | None = None
        for candidate in rest:
            if isinstance(candidate, ProviderRequest):
                req = candidate
                break
        if req is None:
            for key in ("req", "request", "provider_request"):
                candidate = kwargs.get(key)
                if isinstance(candidate, ProviderRequest):
                    req = candidate
                    break
        if req is None:
            req = self._request_from_event(event)

        try:
            logger.info(
                f"[{PLUGIN_NAME}] hook_llm_request_v015 被调用 | {PLUGIN_VERSION} "
                f"| has_request={req is not None} "
                f"| args={[type(a).__name__ for a in rest]} | kwargs={sorted(kwargs)}"
            )
        except Exception:  # noqa: BLE001 - 诊断日志绝不能影响主流程
            pass

        if req is None:
            return
        await self._apply_request_actions(event, req)

    # ------------------------------------------------------------------ 判定核心

    @staticmethod
    def _batch_key(event: AstrMessageEvent, message: str) -> str | None:
        """算一条消息的"批次键"，用于两个入口之间的去重。

        不能只用消息正文：用户完全可能连着发两遍一样的话，那是两轮真实对话。
        这里优先用平台给的消息 id / 时间戳；拿不到时才退回正文。

        Args:
            event: 消息事件。
            message: 消息正文。

        Returns:
            批次键，无法确定时返回 None（调用方跳过去重）。
        """
        obj = getattr(event, "message_obj", None)
        if obj is not None:
            msg_id = getattr(obj, "message_id", "") or ""
            stamp = getattr(obj, "timestamp", 0) or 0
            if msg_id or stamp:
                return f"{msg_id}|{stamp}|{message[:60]}"
        return None

    async def _detect(
        self,
        event: AstrMessageEvent,
        message: str,
        req: ProviderRequest | None,
    ) -> None:
        """对一个用户消息做话题判定并推进状态。

        Args:
            event: 消息事件。
            message: 用户消息正文。
            req: 已拿到的请求对象（没有就传 None）。
        """
        if not self._flag("enabled", True) or self.mode == MODE_OFF:
            return

        umo = self._umo(event)
        conv_id = await self._conversation_id(event)
        state = self._state(umo, conv_id)
        if state.disabled:
            return
        batch_key = self._batch_key(event, message)
        if batch_key is not None and state.last_detected == batch_key:
            logger.debug(
                f"[{PLUGIN_NAME}] 这条消息已经判定过（另一个入口先到了），跳过重复判定"
            )
            return
        if batch_key is not None:
            state.last_detected = batch_key

        started = time.perf_counter()
        result = heuristic(
            message,
            state,
            sim_continue=self._num("sim_continue", 0.5),
            sim_new_topic=self._num("sim_new_topic", 0.12),
            time_gap_minutes=int(self._num("time_gap_minutes", 45, int)),
            min_message_length=int(self._num("min_message_length", 6, int)),
        )

        llm_verdict: Verdict | None = None
        spend_llm = self._flag("always_llm", False) or result.needs_llm
        if spend_llm and self._under_hourly_cap(state):
            llm_verdict = await self._ask_llm(event, message, state, result)
            state.checks.append(time.time())

        verdict = combine(result, llm_verdict)
        verdict.details = {
            "heuristic": result.verdict,
            "heuristic_confidence": round(result.confidence, 3),
            "needs_llm": result.needs_llm,
            "llm_used": llm_verdict is not None,
            "mode": self.mode,
            "has_request": req is not None,
            "cost_ms": int((time.perf_counter() - started) * 1000),
        }

        verdict = self._apply_threshold(verdict)
        self._log_verdict(umo, message, verdict, state)

        # 状态推进：无论判定结果如何，这条消息都成为「上文」
        recent = self._recent_for(umo)
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
        self.store.mark_dirty()
        self.store.maybe_save()

        # 需要改写请求的动作：拿到请求就地执行，拿不到就先挂着等 on_llm_request
        if req is not None:
            await self._act(event, req, umo, state, verdict)
        elif self.mode in {MODE_HINT, MODE_AUTO}:
            self._pending_actions[umo] = (message, verdict)
            logger.debug(
                f"[{PLUGIN_NAME}] {self.mode} 的请求改写已挂起，"
                "等 on_llm_request 拿到 ProviderRequest 再执行"
            )

    async def _apply_request_actions(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """执行挂起的请求改写（`hint` / `auto`）。

        Args:
            event: 消息事件。
            req: 即将发出的请求。
        """
        umo = self._umo(event)
        pending = self._pending_actions.pop(umo, None)
        if pending is None:
            return
        message, verdict = pending
        state = self.store.get(umo)
        if not verdict.is_new_topic:
            return
        logger.info(
            f"[{PLUGIN_NAME}] 执行挂起的动作 | mode={self.mode} "
            f"| msg={message[:40]} | confidence={verdict.confidence:.2f}"
        )
        await self._act(event, req, umo, state, verdict)

    # ------------------------------------------------------------------ 动作层

    def _apply_threshold(self, verdict: Verdict) -> Verdict:
        """把置信度门槛应用到判定结果上。

        低于 `min_new_topic_confidence` 的新话题判定会被压回「延续」：宁可不切。

        Args:
            verdict: 原始判定。

        Returns:
            处理后的判定。
        """
        floor = self._num("min_new_topic_confidence", 0.6)
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

    async def _act(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest | None,
        umo: str,
        state: Any,
        verdict: Verdict,
    ) -> None:
        """按模式和判定结果执行动作。

        `mark` 模式不需要请求对象（它本来就不改请求）；`hint` / `auto` 必须有请求
        才能动作——它们只在拿到 `ProviderRequest` 时才会进入这里，拿不到的情况由
        调用方挂起等待。

        Args:
            event: 消息事件。
            req: 即将发出的请求。
            umo: 统一消息来源。
            state: 话题状态。
            verdict: 最终判定。
        """
        if not verdict.is_new_topic:
            return
        if req is None:
            # 调用方保证只在拿到请求时才进来；这里再兜一层，语义上不会发生。
            return

        if self.mode == MODE_AUTO:
            threshold = self._num("auto_threshold", 0.8)
            if verdict.confidence < threshold:
                logger.info(
                    f"[{PLUGIN_NAME}] 判为新话题但置信度 {verdict.confidence:.2f} "
                    f"< auto_threshold {threshold:.2f}，不自动切换"
                )
                return
            await self._switch_conversation(event, req, umo, state, verdict)
            return

        if self.mode == MODE_HINT:
            hint = make_hint(verdict)
            if hint and TextPart is not None:
                req.extra_user_content_parts.append(TextPart(text=hint).mark_as_temp())
                logger.info(
                    f"[{PLUGIN_NAME}] 已给本轮注入新话题提示（{verdict.topic_label or '未命名'}）"
                )
            return

        # mark：什么都不改，日志已经写了

    async def _switch_conversation(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        umo: str,
        state: Any,
        verdict: Verdict,
    ) -> None:
        """新建一个对话并把本轮请求挂过去。

        Args:
            event: 消息事件。
            req: 即将发出的请求。
            umo: 统一消息来源。
            state: 话题状态。
            verdict: 最终判定。
        """
        persona_id = None
        try:
            if req.conversation is not None:
                persona_id = getattr(req.conversation, "persona_id", None)
        except Exception:  # noqa: BLE001
            persona_id = None
        try:
            new_cid = await self.context.conversation_manager.new_conversation(
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
            conversation = await self.context.conversation_manager.get_conversation(
                umo, new_cid
            )
            if conversation is not None:
                req.conversation = conversation
            req.contexts = []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[{PLUGIN_NAME}] 新对话 {str(new_cid)[:8]} 已创建，但绑定请求失败: {exc}"
            )

        state.conv_id = str(new_cid)
        state.label = verdict.topic_label or state.label
        state.summary = verdict.topic_summary or ""
        state.turns = 0
        self._recent[umo] = [state.last_user_text] if state.last_user_text else []
        self.store.mark_dirty()

        logger.info(
            f"[{PLUGIN_NAME}] 已自动开启新对话 {str(new_cid)[:8]}"
            f"（话题：{verdict.topic_label or '未命名'}，置信度 {verdict.confidence:.2f}）"
        )

        if self._flag("new_conversation_notice", True):
            try:
                await event.send(
                    event.plain_result(
                        f"检测到新话题：{verdict.topic_label or '未命名'}，已为你开一段新对话。"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] 发送新话题提示失败: {exc}")

    # -------------------------------------------------------------------- 日志

    def _under_hourly_cap(self, state: Any) -> bool:
        """检查本会话是否还有模型判定额度。

        Args:
            state: 话题状态。

        Returns:
            True 表示可以调用模型。
        """
        cap = int(self._num("max_checks_per_hour", 30, int))
        if cap <= 0:
            return True
        used = state.checks_last_hour()
        if used >= cap:
            logger.info(
                f"[{PLUGIN_NAME}] 本会话本小时已判定 {used} 次，达到上限 {cap}，本轮只用启发式"
            )
            return False
        return True

    def _log_verdict(
        self, umo: str, message: str, verdict: Verdict, state: Any
    ) -> None:
        """把判定明细写进日志。

        Args:
            umo: 统一消息来源。
            message: 用户消息。
            verdict: 判定结果。
            state: 话题状态。
        """
        if not self._flag("debug_log", True):
            return
        # 日志里刻意不用 emoji：Windows 上部分控制台是 GBK，写 emoji 会让日志处理器报
        # UnicodeEncodeError（在线程里静默刷屏），纯文本在任何编码下都安全。
        tag = "NEW-TOPIC" if verdict.is_new_topic else "CONTINUE"
        preview = message.replace("\n", " ")[:60]
        logger.info(
            f"[{PLUGIN_NAME}] {tag} | {umo} | confidence={verdict.confidence:.2f} "
            f"| source={verdict.source} | topic={state.label or '(none)'} "
            f"| msg={preview} | {verdict.reason} | {verdict.details}"
        )

    # -------------------------------------------------------------------- 指令

    @filter.command("topic")
    async def topic_cmd(self, event: AstrMessageEvent) -> None:
        """查看/管理当前会话的话题状态。

        Args:
            event: 消息事件。
        """
        raw = (event.message_str or "").strip()
        parts = raw.split(maxsplit=2)
        action = parts[1].strip().lower() if len(parts) > 1 else ""
        argument = parts[2].strip() if len(parts) > 2 else ""
        umo = self._umo(event)
        conv_id = await self._conversation_id(event)
        state = self._state(umo, conv_id)

        if action in {"", "status", "状态"}:
            yield event.plain_result(
                "\n".join(
                    [
                        f"当前话题状态（插件 {PLUGIN_VERSION}）",
                        f"话题：{state.label or '（还没建立）'}",
                        f"摘要：{state.summary or '（无）'}",
                        f"当前话题已聊：{state.turns} 轮；累计判定新话题：{state.switches} 次",
                        f"本小时模型判定：{state.checks_last_hour()} 次",
                        f"本会话自动判断：{'已关闭' if state.disabled else '开启'}",
                        f"模式：{self.mode}",
                    ]
                )
            )
            return

        if action in {"new", "切换", "新话题"}:
            label = argument or "手动切换的话题"
            cid = await self.context.conversation_manager.new_conversation(
                umo, event.get_platform_id()
            )
            state = self.store.reset(umo)
            state.conv_id = str(cid)
            state.label = label
            state.last_ts = time.time()
            self._recent[umo] = []
            self.store.save()
            yield event.plain_result(f"已开始新对话（{str(cid)[:8]}），话题：{label}")
            return

        if action in {"forget", "忘记", "clear"}:
            self.store.reset(umo)
            self._recent[umo] = []
            self.store.save()
            yield event.plain_result("已忘记当前话题，下一条消息会重新建档。")
            return

        if action in {"off", "关闭"}:
            state.disabled = True
            self.store.mark_dirty()
            self.store.save()
            yield event.plain_result("本会话已关闭话题自动判断。")
            return

        if action in {"on", "开启"}:
            state.disabled = False
            self.store.mark_dirty()
            self.store.save()
            yield event.plain_result("本会话已开启话题自动判断。")
            return

        yield event.plain_result(
            "用法：/topic 查看状态；/topic new [名称] 手动翻篇；"
            "/topic forget 忘记当前话题；/topic off|on 本会话开关。"
        )

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        """启动后把可用的模型 ID 打进日志，方便填 provider_id。"""
        try:
            manager = getattr(self.context, "provider_manager", None)
            insts = list(getattr(manager, "provider_insts", []) or [])
            ids: list[str] = []
            for item in insts:
                conf = getattr(item, "provider_config", {}) or {}
                pid = str(conf.get("id", "") or "")
                if pid:
                    ids.append(pid)
            self._model_ids = ids
        except Exception as exc:  # noqa: BLE001 - 只是诊断信息
            logger.debug(f"[{PLUGIN_NAME}] 枚举 provider 失败: {exc}")
            self._model_ids = []

        logger.info(
            f"[{PLUGIN_NAME}] 已加载 {PLUGIN_VERSION} | 模式 {self.mode} | 判定模型 "
            f"{self.config.get('provider_id') or '（跟随当前会话）'} | 状态文件 {self.store.path}"
        )
        if self._model_ids:
            logger.info(
                f"[{PLUGIN_NAME}] 可用的模型 ID（可直接填进 provider_id）："
                + "、".join(self._model_ids[:20])
            )

    async def terminate(self) -> None:
        """插件卸载/停用时保存状态。"""
        try:
            self.store.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 退出时保存状态失败: {exc}")
