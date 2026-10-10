"""当前话题状态的持久化。

AstrBot 把「会话（UMO）」和「对话（conversation）」分成两层：一个 UMO 下可以有多个
对话，插件只关心「这个会话当前在聊什么」，所以状态按 UMO 存一份，并记住它绑在哪个
对话 ID 上 —— 用户用 `/new` 换对话后，旧话题状态必须失效，否则第一次判定就会被上一
个对话的残留带偏。

状态存在插件数据目录下的 JSON 文件里，内存里保留一份，写盘做去抖，避免每条消息都落盘。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATE_VERSION = 1


@dataclass
class TopicState:
    """一个会话当前的话题状态。"""

    label: str = ""
    """当前话题的一句话名称，空表示还没有话题（新会话）。"""
    summary: str = ""
    """当前话题的补充说明，由判定模型顺手刷新。"""
    conv_id: str = ""
    """这份状态绑定的对话 ID；与当前对话不一致时状态作废。"""
    last_user_text: str = ""
    """上一条用户消息（正文），用于相似度和承接词判断。"""
    last_detected: str = ""
    """上一条**已判定过**的消息正文。

    消息事件与请求钩子可能针对同一条消息先后触发（中间还隔着别的插件），靠事件
    对象上的临时标记去重并不可靠；把"已经判过哪句话"落到状态里，重复到达时直接
    跳过，`turns` 与话题状态才不会被推快一倍。
    """
    last_ts: float = 0.0
    """上一条用户消息的时间戳（秒）。"""
    turns: int = 0
    """当前话题下已经聊了多少轮。"""
    switches: int = 0
    """本会话累计判定出的新话题次数。"""
    checks: list[float] = field(default_factory=list)
    """最近模型判定调用的时间戳，用于每小时次数上限。"""
    disabled: bool = False
    """本会话是否被 /topic off 关掉了自动判断。"""

    def is_stale_for(self, conv_id: str) -> bool:
        """判断状态是否属于另一个对话。

        Args:
            conv_id: 当前对话 ID。

        Returns:
            绑定对话与当前对话不同且状态里确实有内容时为 True。
        """
        if not self.label and not self.last_user_text:
            return False
        return bool(self.conv_id) and conv_id != self.conv_id

    def checks_last_hour(self) -> int:
        """统计最近一小时内的模型判定次数。

        Returns:
            次数。
        """
        floor = time.time() - 3600
        return sum(1 for stamp in self.checks if stamp >= floor)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写盘的字典。

        Returns:
            字典。
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> TopicState:
        """从磁盘数据恢复状态，字段缺失或类型不符时按默认值处理。

        Args:
            raw: 磁盘上的原始数据。

        Returns:
            TopicState 实例。
        """
        if not isinstance(raw, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        payload = {k: v for k, v in raw.items() if k in known}
        try:
            state = cls(**payload)
        except (TypeError, ValueError):
            return cls()
        if not isinstance(state.checks, list):
            state.checks = []
        state.checks = [float(x) for x in state.checks if isinstance(x, (int, float))][
            -200:
        ]
        return state


class TopicStore:
    """按 UMO 保存话题状态。"""

    def __init__(self, path: Path) -> None:
        """初始化存储。

        Args:
            path: 状态文件路径。
        """
        self.path = path
        self._states: dict[str, TopicState] = {}
        self._saved_revision: tuple | None = None
        self._last_save = 0.0
        self.load()

    def load(self) -> None:
        """从磁盘读取状态，任何异常都退化成空状态。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        states = raw.get("states")
        if not isinstance(states, dict):
            return
        for umo, payload in states.items():
            if isinstance(umo, str):
                self._states[umo] = TopicState.from_dict(payload)

    def _revision(self) -> tuple:
        """算一份用于判断「和上次落盘相比有没有变化」的指纹。

        Returns:
            可比较的元组。
        """
        return tuple(
            sorted(
                (
                    umo,
                    st.label,
                    st.summary,
                    st.conv_id,
                    st.disabled,
                    st.turns,
                    st.switches,
                    st.last_detected,
                    round(st.last_ts, 3),
                    len(st.last_user_text),
                )
                for umo, st in self._states.items()
            )
        )

    def get(self, umo: str) -> TopicState:
        """取某个会话的状态，不存在则建一个。

        Args:
            umo: 统一消息来源。

        Returns:
            TopicState 实例（引用，可直接修改）。
        """
        state = self._states.get(umo)
        if state is None:
            state = TopicState()
            self._states[umo] = state
        return state

    def reset(self, umo: str) -> TopicState:
        """丢弃某个会话的话题状态但保留开关设置。

        Args:
            umo: 统一消息来源。

        Returns:
            清空后的状态。
        """
        old = self._states.get(umo)
        disabled = old.disabled if old is not None else False
        state = TopicState(disabled=disabled)
        self._states[umo] = state
        self.mark_dirty()
        return state

    def clear_topic(self, umo: str) -> None:
        """只清掉"当前在聊什么"，保留开关与统计。

        用户用 `/new` 手动换了对话时走这条路径：新对话里还没有话题，但本会话累计
        判定过多少次新话题、以及用户是否关掉了自动判断，都不该被抹掉。

        Args:
            umo: 统一消息来源。
        """
        state = self.get(umo)
        state.label = ""
        state.summary = ""
        state.last_user_text = ""
        state.last_detected = ""
        state.last_ts = 0.0
        state.turns = 0
        # conv_id 由调用方立刻填成新对话 ID，这里先清空避免中间态被当成"绑定旧对话"
        state.conv_id = ""
        self.mark_dirty()

    def mark_dirty(self) -> None:
        """标记内存状态已变更。"""
        self._last_save = 0.0

    def maybe_save(self, force: bool = False) -> None:
        """把状态写盘。

        写盘用「指纹没变就跳过」代替时间去抖：状态没变时不产生 IO，状态一变
        立刻落盘，不会出现"判定过了但没记住"的窗口。

        Args:
            force: 为 True 时忽略指纹检查强制写一次。
        """
        revision = self._revision()
        if not force and revision == self._saved_revision:
            return
        self.save(revision)

    def save(self, revision: tuple | None = None) -> None:
        """把内存状态写到磁盘，失败只影响持久化不影响运行。

        Args:
            revision: 已经算好的指纹，用于避免重复计算。
        """
        snapshot = self._revision() if revision is None else revision
        payload = {
            "version": STATE_VERSION,
            "saved_at": time.time(),
            "states": {umo: st.to_dict() for umo, st in self._states.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
            self._saved_revision = snapshot
            self._last_save = time.time()
        except OSError:
            self._saved_revision = None
