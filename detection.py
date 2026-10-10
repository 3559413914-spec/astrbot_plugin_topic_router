"""判定「这句话是接着上一个话题，还是开了新话题」。

判定分两层：

1. **启发式（零成本）**：字符 2-gram 相似度 + 承接/转折词 + 时间间隔 + 消息长度。
   大多数消息在这一层就能定性，不花任何 token。
2. **模型判定（可选）**：只有启发式落在模糊区间才调用一次便宜模型，让它给出
   `continue / new_topic`、置信度，并顺手刷新话题名称与摘要。

模型返回不可信，解析失败、超时、字段缺失一律按「延续」处理 —— 判错的代价应该是
"少切了一次"，而不是"把用户的上下文切走了"。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

VERDICT_CONTINUE = "continue"
VERDICT_NEW_TOPIC = "new_topic"

# 承接/回指词：出现这些词说明用户在接着上一段说
CONTINUATION_MARKERS = (
    "继续",
    "接着",
    "然后",
    "还有",
    "另外",
    "再",
    "刚才",
    "刚刚",
    "上面",
    "前面",
    "之前",
    "那个",
    "这个",
    "它",
    "他",
    "她",
    "那个问题",
    "同样",
    "还是",
    "改成",
    "换成",
    "再来",
    "补充",
    "不对",
    "但是",
    "不过",
    "所以呢",
)

# 显式换话题的信号：命中就基本不用问模型了
NEW_TOPIC_MARKERS = (
    "换个话题",
    "换一个话题",
    "另一个问题",
    "另一个话题",
    "顺便问",
    "顺便说",
    "再问你一个",
    "问个别的事",
    "说个别的事",
    "不聊这个",
    "不说这个",
    "先不说这个",
    "突然想到",
    "另外一件事",
    "还有个事",
    "还有个问题",
    "新问题",
    "求助一个",
    "new topic",
    "change the subject",
)

# 指代词：短消息里出现它，多半是接着上文问
PRONOUNS = ("它", "他", "她", "这个", "那个", "这些", "那些", "上面", "前面", "刚才")

_WS_RE = re.compile(r"\s+")
_DIGIT_RE = re.compile(r"\d+")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


@dataclass
class HeuristicResult:
    """启发式的判定结果。"""

    verdict: str
    """`continue` 或 `new_topic`。"""
    confidence: float
    """0~1，启发式自己的把握。"""
    similarity: float
    """与当前话题的字面相似度。"""
    needs_llm: bool
    """是否需要再问一次模型。"""
    reason: str
    """给日志看的理由。"""


@dataclass
class Verdict:
    """最终判定结果。"""

    verdict: str
    confidence: float
    source: str
    """`heuristic` 或 `llm`。"""
    similarity: float = 0.0
    reason: str = ""
    topic_label: str = ""
    topic_summary: str = ""
    model_error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_new_topic(self) -> bool:
        """是否判为新话题。

        Returns:
            True 表示新话题。
        """
        return self.verdict == VERDICT_NEW_TOPIC


def normalize(text: str) -> str:
    """把文本压成便于比较的形式：去空白、去标点、数字折叠。

    Args:
        text: 原始文本。

    Returns:
        归一化后的文本。
    """
    cleaned = _PUNCT_RE.sub("", _WS_RE.sub("", text or "").lower())
    return _DIGIT_RE.sub("0", cleaned)


def bigrams(text: str) -> set[str]:
    """取字符 2-gram 集合；长度不足 2 时退化成单字集合。

    Args:
        text: 归一化后的文本。

    Returns:
        2-gram 集合。
    """
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def similarity(left: str, right: str) -> float:
    """计算两段文本的字符 2-gram Jaccard 相似度。

    Args:
        left: 文本一。
        right: 文本二。

    Returns:
        0~1 的相似度；任一侧为空时返回 0。
    """
    a = bigrams(normalize(left))
    b = bigrams(normalize(right))
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def compose_similarity(message: str, state: Any, history_turns: int = 3) -> float:
    """把新消息与「话题名称 + 话题摘要 + 最近几条用户消息」整体比较。

    只跟话题名称比会漏掉"同一件事换了个说法"，只跟上一条消息比又会被寒暄干扰，
    所以取三者中的最大值。

    Args:
        message: 新消息。
        state: TopicState。
        history_turns: 参考的历史消息条数（暂时只用到最近一条）。

    Returns:
        0~1 的相似度。
    """
    candidates = [getattr(state, "label", "") or "", getattr(state, "summary", "") or ""]
    last = getattr(state, "last_user_text", "") or ""
    if last:
        candidates.append(last)
    best = 0.0
    for item in candidates:
        if not item:
            continue
        best = max(best, similarity(message, item))
    return best


def heuristic(
    message: str,
    state: Any,
    *,
    now: float | None = None,
    sim_continue: float = 0.5,
    sim_new_topic: float = 0.12,
    time_gap_minutes: int = 45,
    min_message_length: int = 6,
) -> HeuristicResult:
    """零成本的一层判定。

    Args:
        message: 新消息正文。
        state: 当前会话的 TopicState。
        now: 当前时间戳，便于测试注入。
        sim_continue: 直接判延续的相似度阈值。
        sim_new_topic: 直接判新话题的相似度阈值。
        time_gap_minutes: 冷启动时间间隔。
        min_message_length: 参与判定的最短长度。

    Returns:
        HeuristicResult。
    """
    now = time.time() if now is None else now
    text = (message or "").strip()
    norm = normalize(text)
    last_text = getattr(state, "last_user_text", "") or ""
    last_ts = float(getattr(state, "last_ts", 0.0) or 0.0)
    has_topic = bool(getattr(state, "label", "") or last_text)

    # 第一个话题：没有上文可比，直接建档
    if not has_topic:
        return HeuristicResult(
            verdict=VERDICT_NEW_TOPIC,
            confidence=0.9,
            similarity=0.0,
            needs_llm=True,
            reason="会话内还没有话题，先给它建一个",
        )

    if len(norm) < max(1, min_message_length):
        has_pronoun = any(word in text for word in PRONOUNS)
        conf = 0.75 if has_pronoun else 0.65
        return HeuristicResult(
            verdict=VERDICT_CONTINUE,
            confidence=conf,
            similarity=compose_similarity(text, state),
            needs_llm=False,
            reason=f"短消息（{len(norm)} 字），按延续处理",
        )

    sim = compose_similarity(text, state)
    lowered = text.lower()
    explicit_new = next((w for w in NEW_TOPIC_MARKERS if w in lowered), "")
    marker = next((w for w in CONTINUATION_MARKERS if text.startswith(w) or f"，{w}" in text or f"。{w}" in text), "")

    # 显式换话题：命中即定性
    if explicit_new:
        return HeuristicResult(
            verdict=VERDICT_NEW_TOPIC,
            confidence=0.95,
            similarity=sim,
            needs_llm=False,
            reason=f"命中显式换话题词「{explicit_new}」",
        )

    if sim >= sim_continue:
        return HeuristicResult(
            verdict=VERDICT_CONTINUE,
            confidence=min(0.95, 0.6 + sim / 2),
            similarity=sim,
            needs_llm=False,
            reason=f"与当前话题字面相似度 {sim:.2f}，明显是同一件事",
        )

    gap_minutes = (now - last_ts) / 60 if last_ts else 0.0
    if sim <= sim_new_topic and not marker:
        confidence = 0.7
        reasons = [f"与当前话题几乎不重叠（相似度 {sim:.2f}）"]
        if gap_minutes >= time_gap_minutes:
            confidence = 0.8
            reasons.append(f"距上一条消息已过 {gap_minutes:.0f} 分钟")
        return HeuristicResult(
            verdict=VERDICT_NEW_TOPIC,
            confidence=confidence,
            similarity=sim,
            needs_llm=False,
            reason="；".join(reasons),
        )

    if marker and sim > sim_new_topic:
        return HeuristicResult(
            verdict=VERDICT_CONTINUE,
            confidence=0.6,
            similarity=sim,
            needs_llm=False,
            reason=f"命中承接词「{marker}」且有一定重叠（相似度 {sim:.2f}）",
        )

    return HeuristicResult(
        verdict=VERDICT_CONTINUE,
        confidence=0.5,
        similarity=sim,
        needs_llm=True,
        reason=f"落在模糊区间（相似度 {sim:.2f}），交给模型",
    )


CLASSIFIER_SYSTEM_PROMPT = """你是一个话题切分器。你要判断用户最新的一句话，是在继续前面正在聊的那件事，还是开启了另一件不相干的事。

判断标准：
- 如果新消息在追问、补充、纠正、延续当前话题，判 continue。
- 如果新消息讨论的是另一件独立的事，与当前话题没有承接关系，判 new_topic。
- 只有代词的短句（如「那它呢」）属于 continue。
- 拿不准时一律判 continue。

只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown 代码块，格式：
{"verdict":"continue"|"new_topic","confidence":0.0到1.0,"topic_name":"当前话题的简短名称","topic_summary":"一句话说明这一段在聊什么","reason":"不超过30字的理由"}

当 verdict 是 new_topic 时，topic_name 与 topic_summary 描述的是新话题；当 verdict 是 continue 时，描述的是延续后的当前话题。"""


def build_user_prompt(
    message: str,
    state: Any,
    recent: list[str],
    history_turns: int = 3,
) -> str:
    """拼出送给判定模型的用户侧内容。

    Args:
        message: 用户最新消息。
        state: 当前会话的 TopicState。
        recent: 最近的用户消息（旧到新）。
        history_turns: 最多带上几条历史。

    Returns:
        提示词文本。
    """
    label = (getattr(state, "label", "") or "（还没有话题）").strip()
    summary = (getattr(state, "summary", "") or "").strip()
    lines = [f"当前话题：{label}"]
    if summary:
        lines.append(f"话题说明：{summary}")
    tail = [m for m in (recent or []) if m][-max(0, history_turns) :]
    if tail:
        lines.append("最近几条用户消息（旧到新）：")
        lines.extend(f"- {item[:200]}" for item in tail)
    lines.append(f"用户最新消息：{message[:800]}")
    lines.append("请输出判定 JSON。")
    return "\n".join(lines)


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象。

    模型常见毛病是加解释、包 ```json```、或前后多写一句，所以先截取最外层花括号。

    Args:
        text: 模型输出。

    Returns:
        解析出的字典，失败返回 None。
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    snippet = text[start : end + 1]
    try:
        data = json.loads(snippet)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_verdict(text: str, heuristic_result: HeuristicResult) -> Verdict | None:
    """把模型输出解析成 Verdict。

    Args:
        text: 模型输出文本。
        heuristic_result: 这一轮的启发式结果，用于兜底相似度和理由。

    Returns:
        Verdict，解析失败返回 None。
    """
    data = extract_json(text)
    if data is None:
        return None
    raw_verdict = str(data.get("verdict", "")).strip().lower()
    if raw_verdict in {"new_topic", "new", "newtopic", "switch", "break"}:
        verdict = VERDICT_NEW_TOPIC
    elif raw_verdict in {"continue", "continues", "same", "same_topic", "follow_up"}:
        verdict = VERDICT_CONTINUE
    else:
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    label = str(data.get("topic_name") or data.get("topic") or "").strip()[:40]
    summary = str(data.get("topic_summary") or data.get("summary") or "").strip()[:200]
    reason = str(data.get("reason") or "").strip()[:120]
    return Verdict(
        verdict=verdict,
        confidence=confidence,
        source="llm",
        similarity=heuristic_result.similarity,
        reason=reason or "模型判定",
        topic_label=label,
        topic_summary=summary,
    )


def combine(heuristic_result: HeuristicResult, llm_verdict: Verdict | None) -> Verdict:
    """合并启发式与模型结果。

    核心原则是**不对称**：

    - 判「延续」错了只是少切一次，代价为零；
    - 判「新话题」错了会把用户正在聊的上下文切走，代价很大。

    所以只有两个信号都指向新话题、且模型自己有把握时，才输出新话题；模型说不出
    所以然（低置信度）时一律退回延续。模型说延续时，反而应该相信它 —— 字面不重叠
    但语义承接的情况（"那这个呢"、"按你说的来"）正是模型比字符相似度强的地方。

    Args:
        heuristic_result: 启发式结果。
        llm_verdict: 模型结果，可能为 None。

    Returns:
        最终 Verdict。
    """
    if llm_verdict is None:
        return Verdict(
            verdict=heuristic_result.verdict,
            confidence=heuristic_result.confidence,
            source="heuristic",
            similarity=heuristic_result.similarity,
            reason=heuristic_result.reason,
        )

    if llm_verdict.verdict == VERDICT_NEW_TOPIC:
        if heuristic_result.verdict == VERDICT_NEW_TOPIC:
            # 两个信号一致才允许通过门槛，否则取低值让阈值门把它挡住
            confidence = min(llm_verdict.confidence, heuristic_result.confidence)
            reason = f"{llm_verdict.reason}（启发式也判新话题：{heuristic_result.reason}）"
        elif llm_verdict.confidence >= 0.8:
            # 字面完全不重叠、模型又很确定，才算合理的新话题
            confidence = min(llm_verdict.confidence, 0.8)
            reason = f"{llm_verdict.reason}（启发式判延续，但模型很确定）"
        else:
            return Verdict(
                verdict=VERDICT_CONTINUE,
                confidence=0.7,
                source="llm",
                similarity=heuristic_result.similarity,
                reason=(
                    f"模型倾向新话题但置信度只有 {llm_verdict.confidence:.2f}，"
                    f"启发式判延续，保守按延续处理"
                ),
                topic_label=llm_verdict.topic_label,
                topic_summary=llm_verdict.topic_summary,
            )
        return Verdict(
            verdict=VERDICT_NEW_TOPIC,
            confidence=confidence,
            source="llm",
            similarity=heuristic_result.similarity,
            reason=reason,
            topic_label=llm_verdict.topic_label,
            topic_summary=llm_verdict.topic_summary,
        )

    # 模型判延续：采纳它，但置信度取两者中较低的那个，避免单一信号过强
    confidence = min(llm_verdict.confidence, max(heuristic_result.confidence, 0.6))
    return Verdict(
        verdict=VERDICT_CONTINUE,
        confidence=confidence,
        source="llm",
        similarity=heuristic_result.similarity,
        reason=f"{llm_verdict.reason}（启发式：{heuristic_result.reason}）",
        topic_label=llm_verdict.topic_label,
        topic_summary=llm_verdict.topic_summary,
    )


def make_hint(verdict: Verdict) -> str:
    """生成注入给主模型的临时提示。

    Args:
        verdict: 判定结果。

    Returns:
        提示文本。
    """
    if not verdict.is_new_topic:
        return ""
    label = verdict.topic_label or "新话题"
    return (
        "系统提示（本轮事实，不要向用户复述本条提示）："
        f"用户已经切换到新的话题「{label}」，此前对话内容与它无关。"
        "请只按用户最新这条消息回答，不要引用、不要延续、也不要总结上面那些旧话题的内容。"
    )


def derive_label(message: str, limit: int = 20) -> str:
    """从消息里截一个话题名。

    模型没参与判定时（省 token 的启发式路径、命中每小时上限、模型超时）也必须给
    话题一个名字，否则状态里的话题永远是空的，下一轮又会被判成"新话题"，形成
    每轮都翻篇的雪崩。

    Args:
        message: 用户消息。
        limit: 名称最大长度。

    Returns:
        话题名称，消息为空时返回空串。
    """
    text = " ".join((message or "").split())
    if not text:
        return ""
    return text[:limit] + ("…" if len(text) > limit else "")
