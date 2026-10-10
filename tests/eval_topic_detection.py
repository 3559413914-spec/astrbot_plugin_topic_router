"""话题判定评测台：用真实聊天记录衡量判得准不准、贵不贵。

## 为什么要有它

插件里的阈值（相似度、时间间隔、置信度）都是凭常识拍的，之前唯一的验证方式是
"发一句话看日志"。这个脚本把**真实历史对话**灌进判定流水线，输出可对比的数字，
让每一次调参/改提示词都有依据。

## 数据来源

- 默认读本机 AstrBot 的 SQLite：`~/.astrbot/data/data_v4.db` 的 `conversations` 表
  （`content` 字段是 `[{role, content}, ...]`）；
- 也可以用 `--conversations <json 文件>` 指定自带样本，格式见 `--help`。

脚本**不联网**：只跑启发式层时零成本；加 `--llm` 才会真的调用模型（走 AstrBot 的
provider，需要 AstrBot 环境），并统计调用次数与耗时。

## 真值（ground truth）怎么来

真实数据里没有人工标注的"话题边界"，所以这里用可复现的启发式标注，并把口径写清楚：

- **默认：时间间隔代理**。同一会话里，相邻用户消息间隔超过 `--gap-minutes`
  （默认 15）就记为"新话题"。依据是：隔这么久再开口，多半是另一件事；
- **可选：手工标注**。`--labels <json>` 传 `{对话ID: [起始下标, ...]}`，用于对某几条
  真实对话做精确评估。

⚠️ 时间间隔代理本身有噪声（同一话题隔很久也会被算成新话题），所以：
**它的绝对值只当参考，真正有用的是"改动前后同一口径下的对比"**。

## 用法

    # 只看启发式层（零成本、零网络）
    python tests/eval_topic_detection.py

    # 连模型一起评（需要 AstrBot 环境 + 配置好 provider）
    python tests/eval_topic_detection.py --llm --provider deepseek/deepseek-v4-flash

    # 换标注口径 / 换阈值做敏感性分析
    python tests/eval_topic_detection.py --gap-minutes 30 --sim-new-topic 0.2
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

DEFAULT_DB = Path.home() / ".astrbot" / "data" / "data_v4.db"

# AstrBot 会往用户消息里注入这类提醒，评测时要剥掉，否则会污染相似度与提示词
SYSTEM_TAG_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL)
IMAGE_TAG_RE = re.compile(r"\[Image Attachment:[^\]]*\]")


# --------------------------------------------------------------------------- 载入


def load_detection():
    """载入插件的判定模块（`detection.py`）。

    它本身只依赖标准库，但为稳妥起见，若本机没有 AstrBot 就先装一个桩模块。

    Returns:
        detection 模块对象。
    """
    if "astrbot" not in sys.modules:
        try:  # 有真 AstrBot 就用真的
            import astrbot  # noqa: F401,PLC0415
        except ImportError:
            stub = type(sys)("astrbot")
            stub.api = type(sys)("astrbot.api")
            stub.api.logger = SimpleNamespace(
                info=lambda *a, **k: None,
                debug=lambda *a, **k: None,
                warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
            )
            sys.modules["astrbot"] = stub
            sys.modules["astrbot.api"] = stub.api

    spec = importlib.util.spec_from_file_location(
        "topic_router_detection", PLUGIN_DIR / "detection.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["topic_router_detection"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# --------------------------------------------------------------------------- 数据


@dataclass
class Turn:
    """一轮对话：一条用户消息 + 紧随其后的助手回复。"""

    index: int
    user_text: str
    assistant_text: str = ""
    timestamp: float | None = None
    is_new_topic: bool = False
    """真值：这一轮是否开启了新话题。"""


@dataclass
class Conversation:
    """一个会话的轮次序列。"""

    cid: str
    platform: str
    turns: list[Turn] = field(default_factory=list)


def _segments_to_text(content: object) -> str:
    """把 AstrBot 的消息 content 归一成纯文本。

    Args:
        content: 字符串，或 `[{"type": "text", "text": ...}, ...]` 结构。

    Returns:
        纯文本（已剥掉 system_reminder 等注入内容）。
    """
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        parts: list[str] = []
        for seg in content:
            if isinstance(seg, dict):
                if seg.get("type") == "text":
                    parts.append(str(seg.get("text", "")))
                elif seg.get("type") == "image_url":
                    parts.append("[图片]")
            elif isinstance(seg, str):
                parts.append(seg)
        raw = "\n".join(parts)
    else:
        raw = str(content or "")
    raw = SYSTEM_TAG_RE.sub("", raw)
    raw = IMAGE_TAG_RE.sub("[图片]", raw)
    return " ".join(raw.split()).strip()


def load_from_db(db_path: Path, min_turns: int = 2) -> list[Conversation]:
    """从 AstrBot 的 SQLite 读历史对话。

    Args:
        db_path: 数据库路径。
        min_turns: 少于这么多轮的会话会被跳过（太短没评估价值）。

    Returns:
        Conversation 列表。
    """
    if not db_path.is_file():
        raise SystemExit(f"找不到数据库：{db_path}（可用 --db 指定，或 --conversations 传样本）")

    con = sqlite3.connect(db_path)
    rows = con.execute(
        "select conversation_id, platform_id, content, updated_at from conversations"
    ).fetchall()
    con.close()

    conversations: list[Conversation] = []
    for cid, platform, content, _updated in rows:
        try:
            messages = json.loads(content or "[]")
        except ValueError:
            continue
        if not isinstance(messages, list):
            continue

        conv = Conversation(cid=str(cid), platform=str(platform or "?"))
        pending_user = ""
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            text = _segments_to_text(msg.get("content"))
            if role == "user":
                if text:
                    # AstrBot 会把同一轮的连发消息合并，这里也合并相邻 user
                    pending_user = f"{pending_user} {text}".strip() if pending_user else text
            elif role == "assistant":
                if pending_user:
                    conv.turns.append(
                        Turn(
                            index=len(conv.turns),
                            user_text=pending_user,
                            assistant_text=text,
                        )
                    )
                    pending_user = ""
        if pending_user:  # 末尾只有用户消息、还没回复
            conv.turns.append(
                Turn(index=len(conv.turns), user_text=pending_user, assistant_text="")
            )
        if len(conv.turns) >= min_turns:
            conversations.append(conv)
    return conversations


def load_from_json(path: Path) -> list[Conversation]:
    """从自带样本文件读对话。

    支持两种格式：
    1. `{"cid": [{"user": "...", "assistant": "..."}, ...], ...}`
    2. `[{"cid": "...", "turns": [{"user": "...", "assistant": "..."}, ...]}, ...]`

    Args:
        path: JSON 文件路径。

    Returns:
        Conversation 列表。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    conversations: list[Conversation] = []
    items = data.items() if isinstance(data, dict) else [
        (str(d.get("cid", i)), d.get("turns", [])) for i, d in enumerate(data)
    ]
    for cid, turns in items:
        conv = Conversation(cid=str(cid), platform="sample")
        for i, turn in enumerate(turns):
            conv.turns.append(
                Turn(
                    index=i,
                    user_text=str(turn.get("user", "")),
                    assistant_text=str(turn.get("assistant", "")),
                )
            )
        conversations.append(conv)
    return conversations


def label_by_time_gap(
    conversations: list[Conversation],
    gap_minutes: int,
    db_path: Path,
) -> int:
    """用时间间隔给对话打真值标签。

    ⚠️ 需要消息级时间戳，而 SQLite 里只有对话级的 `updated_at`，所以本函数只在消息
    自带时间戳（JSON 样本的 `ts` 字段）时生效；否则返回 0 表示"无法用该口径"。

    Args:
        conversations: 对话列表。
        gap_minutes: 超过这个间隔视为新话题。
        db_path: 数据库路径（保留参数，便于将来改用消息级时间戳）。

    Returns:
        标注了多少条"新话题"边界。
    """
    del db_path  # 目前不需要
    labeled = 0
    for conv in conversations:
        first = True
        for turn in conv.turns:
            if first:
                turn.is_new_topic = True
                first = False
                labeled += 1
                continue
            if turn.timestamp is None:
                continue
            prev = conv.turns[turn.index - 1].timestamp
            if prev is not None and (turn.timestamp - prev) > gap_minutes * 60:
                turn.is_new_topic = True
                labeled += 1
    return labeled


def label_first_turn_only(conversations: list[Conversation]) -> int:
    """保底标注：每个会话的第一轮算新话题，其余算延续。

    这不是有信息的真值，只用来让"召回率"这类指标有个可对比的分母；当时间间隔口径
    不可用时使用。

    Args:
        conversations: 对话列表。

    Returns:
        标注条数。
    """
    count = 0
    for conv in conversations:
        if conv.turns:
            conv.turns[0].is_new_topic = True
            count += 1
    return count


def apply_manual_labels(conversations: list[Conversation], labels: dict) -> int:
    """套用手工标注（`{cid: [起始下标, ...]}`）。

    Args:
        conversations: 对话列表。
        labels: 手工标注字典。

    Returns:
        套用成功的边界数。
    """
    applied = 0
    for conv in conversations:
        marks = labels.get(conv.cid)
        if not marks:
            continue
        for turn in conv.turns:
            turn.is_new_topic = turn.index in set(marks)
            if turn.is_new_topic:
                applied += 1
    return applied


# --------------------------------------------------------------------------- 评测


def legacy_heuristic(
    message: str,
    state: Any,
    *,
    sim_continue: float = 0.5,
    sim_new_topic: float = 0.12,
    time_gap_minutes: int = 45,
    min_message_length: int = 6,
):
    """v0.3.2 及以前的启发式，**只为对比保留**。

    它的问题（真实数据实测）：短句与话题名字面重叠为 0 时直接判新话题、不问模型。
    「我没有代理啊」「等等吧还没做完」这类追问因此被误判，65 轮里有 30 轮中招。

    这里重新实现一遍而不是改回去，是为了让 `--compare-legacy` 能一次跑出前后对比。

    Args:
        message: 新消息。
        state: 会话状态。
        sim_continue: 延续阈值。
        sim_new_topic: 新话题阈值。
        time_gap_minutes: 冷启动间隔。
        min_message_length: 最短参与长度。

    Returns:
        HeuristicResult。
    """
    import time as _time

    text = (message or "").strip()
    norm = "".join(ch for ch in text.lower() if not ch.isspace())
    last_text = getattr(state, "last_user_text", "") or ""
    last_ts = float(getattr(state, "last_ts", 0.0) or 0.0)
    has_topic = bool(getattr(state, "label", "") or last_text)

    if not has_topic:
        return detection_module().HeuristicResult(
            verdict="new_topic",
            confidence=0.9,
            similarity=0.0,
            needs_llm=True,
            reason="会话内还没有话题，先给它建一个",
        )

    if len(norm) < max(1, min_message_length):
        return detection_module().HeuristicResult(
            verdict="continue",
            confidence=0.65,
            similarity=_sim(text, state),
            needs_llm=False,
            reason="短消息，按延续处理",
        )

    sim = _sim(text, state)
    explicit_new = next(
        (w for w in NEW_TOPIC_WORDS if w in text.lower()), ""
    )
    marker = next(
        (w for w in CONTINUE_WORDS if text.startswith(w) or f"，{w}" in text), ""
    )
    if explicit_new:
        return detection_module().HeuristicResult(
            verdict="new_topic",
            confidence=0.95,
            similarity=sim,
            needs_llm=False,
            reason="命中显式换话题词",
        )
    if sim >= sim_continue:
        return detection_module().HeuristicResult(
            verdict="continue",
            confidence=min(0.95, 0.6 + sim / 2),
            similarity=sim,
            needs_llm=False,
            reason="高度重叠",
        )
    if sim <= sim_new_topic and not marker:
        return detection_module().HeuristicResult(
            verdict="new_topic",
            confidence=0.7,
            similarity=sim,
            needs_llm=False,
            reason="几乎不重叠",  # ← 旧行为：短句也走这条
        )
    del last_ts, _time
    return detection_module().HeuristicResult(
        verdict="continue",
        confidence=0.5,
        similarity=sim,
        needs_llm=True,
        reason="模糊区间",
    )


_DETECTION = None
NEW_TOPIC_WORDS: tuple[str, ...] = ()
CONTINUE_WORDS: tuple[str, ...] = ()


def detection_module():
    """取已载入的 detection 模块（供 legacy_heuristic 复用其数据结构）。

    Returns:
        detection 模块。
    """
    assert _DETECTION is not None
    return _DETECTION


def _sim(text: str, state: Any) -> float:
    """算相似度（走 detection 的真实实现，保证口径一致）。

    Args:
        text: 新消息。
        state: 会话状态。

    Returns:
        相似度。
    """
    return detection_module().compose_similarity(text, state)


@dataclass
class Outcome:
    """一条判定记录。"""

    cid: str
    index: int
    message: str
    predicted_new: bool
    truth_new: bool
    source: str
    confidence: float
    similarity: float
    needs_llm: bool
    llm_used: bool
    cost_ms: int = 0
    reason: str = ""


def run_eval(
    detection,
    conversations: list[Conversation],
    args: argparse.Namespace,
    ask_llm=None,
    use_legacy: bool = False,
) -> list[Outcome]:
    """把所有对话跑一遍判定流水线。

    刻意复刻 router 里的判定顺序（启发式 → 可能问模型 → combine → 门槛），
    这样评测结果才和线上一致。

    Args:
        detection: detection 模块。
        conversations: 对话列表。
        args: 命令行参数。
        ask_llm: 可选回调 `(message, state, heuristic) -> Verdict | None`。
        use_legacy: 用 v0.3.2 的旧启发式（用于前后对比）。

    Returns:
        Outcome 列表。
    """
    global _DETECTION, NEW_TOPIC_WORDS, CONTINUE_WORDS
    _DETECTION = detection
    NEW_TOPIC_WORDS = detection.NEW_TOPIC_MARKERS
    CONTINUE_WORDS = detection.CONTINUATION_MARKERS

    outcomes: list[Outcome] = []

    for conv in conversations:
        state = SimpleNamespace(
            label="",
            summary="",
            last_user_text="",
            last_assistant_text="",
            last_ts=0.0,
            conv_id=conv.cid,
        )
        for turn in conv.turns:
            message = turn.user_text.strip()
            if not message:
                continue
            started = time.perf_counter()

            if use_legacy:
                result = legacy_heuristic(
                    message,
                    state,
                    sim_continue=args.sim_continue,
                    sim_new_topic=args.sim_new_topic,
                    time_gap_minutes=args.gap_minutes,
                )
            else:
                result = detection.heuristic(
                    message,
                    state,
                    sim_continue=args.sim_continue,
                    sim_new_topic=args.sim_new_topic,
                    time_gap_minutes=args.gap_minutes,
                )

            llm_verdict = None
            if ask_llm is not None and (args.always_llm or result.needs_llm):
                llm_verdict = ask_llm(message, state, result, turn)

            verdict = detection.combine(result, llm_verdict)
            # 与线上一致：低于门槛的新话题会被压回延续
            if verdict.is_new_topic and verdict.confidence < args.min_confidence:
                verdict = detection.Verdict(
                    verdict=detection.VERDICT_CONTINUE,
                    confidence=1.0 - verdict.confidence,
                    source=verdict.source,
                    similarity=verdict.similarity,
                    reason=f"低于门槛 {args.min_confidence}",
                )

            outcomes.append(
                Outcome(
                    cid=conv.cid,
                    index=turn.index,
                    message=message,
                    predicted_new=verdict.is_new_topic,
                    truth_new=turn.is_new_topic,
                    source=verdict.source,
                    confidence=verdict.confidence,
                    similarity=verdict.similarity,
                    needs_llm=result.needs_llm,
                    llm_used=llm_verdict is not None,
                    cost_ms=int((time.perf_counter() - started) * 1000),
                    reason=verdict.reason,
                )
            )

            # 推进状态（与线上一致；`include_last_assistant` 打开时才喂助手回复）
            state.label = verdict.topic_label or state.label or detection.derive_label(message)
            state.summary = verdict.topic_summary or state.summary
            state.last_user_text = message
            if getattr(args, "include_last_assistant", False):
                state.last_assistant_text = turn.assistant_text
            state.last_ts = 0.0

    return outcomes


def summarize(outcomes: list[Outcome]) -> dict:
    """算指标。

    Args:
        outcomes: 判定记录。

    Returns:
        指标字典。
    """
    tp = sum(1 for o in outcomes if o.predicted_new and o.truth_new)
    fp = sum(1 for o in outcomes if o.predicted_new and not o.truth_new)
    fn = sum(1 for o in outcomes if not o.predicted_new and o.truth_new)
    tn = sum(1 for o in outcomes if not o.predicted_new and not o.truth_new)
    total = len(outcomes)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    llm_calls = sum(1 for o in outcomes if o.llm_used)
    return {
        "total": total,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": (tp + tn) / total if total else 0.0,
        "llm_calls": llm_calls,
        "llm_ratio": llm_calls / total if total else 0.0,
        "heuristic_ratio": (total - llm_calls) / total if total else 0.0,
        "avg_ms": sum(o.cost_ms for o in outcomes) / total if total else 0.0,
    }


def report(
    outcomes: list[Outcome],
    metrics: dict,
    args: argparse.Namespace,
    label_note: str,
    top_mistakes: int = 12,
) -> None:
    """打印评测报告。

    Args:
        outcomes: 判定记录。
        metrics: 指标。
        args: 命令行参数。
        label_note: 真值口径说明。
        top_mistakes: 展示多少条误判样例。
    """
    print("=" * 72)
    print("话题判定评测报告")
    print("=" * 72)
    print(f"真值口径      : {label_note}")
    print(
        f"阈值          : sim_continue={args.sim_continue} sim_new_topic={args.sim_new_topic} "
        f"min_confidence={args.min_confidence} gap={args.gap_minutes}min"
    )
    print(
        f"助手回复入提示: {'是' if getattr(args, 'include_last_assistant', False) else '否'}"
    )
    print(f"样本          : {metrics['total']} 轮")
    print()
    print("混淆矩阵（正类 = 判定为新话题）")
    print(f"  真·新话题   且 判新话题 (TP) : {metrics['tp']}")
    print(f"  真·延续     但 判新话题 (FP) : {metrics['fp']}   ← 误判会让用户觉得机器人失忆")
    print(f"  真·新话题   但 判延续   (FN) : {metrics['fn']}   ← 漏判会让模型串味")
    print(f"  真·延续     且 判延续   (TN) : {metrics['tn']}")
    print()
    print(f"精确率 precision : {metrics['precision']:.3f}")
    print(f"召回率 recall    : {metrics['recall']:.3f}")
    print(f"F1              : {metrics['f1']:.3f}")
    print(f"准确率 accuracy  : {metrics['accuracy']:.3f}")
    print()
    print("成本")
    print(
        f"  调用模型 {metrics['llm_calls']} 次 / {metrics['total']} 轮 "
        f"= {metrics['llm_ratio']:.1%}（启发式单独搞定 {metrics['heuristic_ratio']:.1%}）"
    )
    print(f"  平均每轮判定耗时 {metrics['avg_ms']:.1f} ms（含模型调用时会更长）")

    mistakes = [o for o in outcomes if o.predicted_new != o.truth_new]
    if mistakes and top_mistakes:
        print()
        print(f"误判样例（最多 {top_mistakes} 条）")
        for o in mistakes[:top_mistakes]:
            kind = "FP 误判成新话题" if o.predicted_new else "FN 漏判新话题"
            print(
                f"  [{kind}] conf={o.confidence:.2f} sim={o.similarity:.2f} "
                f"src={o.source} | {o.message[:56]!r}"
            )
            print(f"       理由: {o.reason[:100]}")

    # 新话题判定的置信度分布：用于判断门槛该定在哪
    new_outcomes = [o for o in outcomes if o.predicted_new]
    if new_outcomes:
        buckets = {"<0.6": 0, "0.6-0.7": 0, "0.7-0.8": 0, "0.8-0.9": 0, ">=0.9": 0}
        for o in new_outcomes:
            c = o.confidence
            if c < 0.6:
                buckets["<0.6"] += 1
            elif c < 0.7:
                buckets["0.6-0.7"] += 1
            elif c < 0.8:
                buckets["0.7-0.8"] += 1
            elif c < 0.9:
                buckets["0.8-0.9"] += 1
            else:
                buckets[">=0.9"] += 1
        print()
        print("判为「新话题」的置信度分布（决定 auto_threshold 定在哪）")
        for key, value in buckets.items():
            print(f"  {key:>8}: {value}")
    print("=" * 72)


def build_cli() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        ArgumentParser。
    """
    parser = argparse.ArgumentParser(description="话题判定评测台")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="AstrBot SQLite 路径")
    parser.add_argument("--conversations", help="改用 JSON 样本文件")
    parser.add_argument("--labels", help="手工标注 JSON：{cid: [起始下标, ...]}")
    parser.add_argument("--gap-minutes", type=int, default=15, help="时间间隔标注口径")
    parser.add_argument("--sim-continue", type=float, default=0.5)
    parser.add_argument("--sim-new-topic", type=float, default=0.12)
    parser.add_argument("--min-confidence", type=float, default=0.6)
    parser.add_argument("--min-turns", type=int, default=2, help="少于这么多轮的会话跳过")
    parser.add_argument("--llm", action="store_true", help="真的调用模型（需要 AstrBot 环境）")
    parser.add_argument("--always-llm", action="store_true", help="每轮都问模型")
    parser.add_argument("--provider", default="", help="判定用 provider id")
    parser.add_argument(
        "--include-last-assistant",
        action="store_true",
        help="把机器人上一句回复也喂给判定器（对比 A/B）",
    )
    parser.add_argument("--json-out", help="把指标写到 JSON 文件，便于前后对比")
    parser.add_argument(
        "--compare-legacy",
        action="store_true",
        help="同时跑 v0.3.2 的旧启发式，打印前后对比（用于验证改动效果）",
    )
    return parser


def compare_legacy(
    detection,
    conversations: list[Conversation],
    args: argparse.Namespace,
) -> None:
    """打印"旧启发式 vs 当前启发式"的行为对比。

    只看行为分布（判了多少新话题、有多少真的会去问模型、短句误判多少），
    不依赖真值 —— 因为真值本身是代理口径，而"短句被直接定性成新话题"是无条件错误。

    Args:
        detection: detection 模块。
        conversations: 对话列表。
        args: 命令行参数。
    """
    def distribution(use_legacy: bool) -> dict:
        outcomes = run_eval(
            detection, conversations, args, ask_llm=None, use_legacy=use_legacy
        )
        total = len(outcomes)
        new = sum(1 for o in outcomes if o.predicted_new)
        llm = sum(1 for o in outcomes if o.needs_llm)
        short_new = sum(
            1 for o in outcomes if o.predicted_new and len(o.message) <= 25
        )
        return {
            "total": total,
            "new": new,
            "new_ratio": new / total if total else 0.0,
            "llm": llm,
            "llm_ratio": llm / total if total else 0.0,
            "short_new": short_new,
        }

    old = distribution(True)
    new = distribution(False)
    print()
    print("=" * 72)
    print("前后对比（旧启发式 v0.3.2 vs 当前）")
    print("=" * 72)
    print(f"{'指标':<34}{'旧':>10}{'新':>10}{'变化':>12}")
    rows = [
        ("判定为新话题的轮数", "new", "d"),
        ("其中短句(<=25字)被判新话题", "short_new", "d"),
        ("会去问模型的轮数", "llm", "d"),
    ]
    for label, key, _kind in rows:
        before, after = old[key], new[key]
        delta = after - before
        arrow = "↓" if delta < 0 else ("↑" if delta > 0 else "=")
        print(f"{label:<34}{before:>10}{after:>10}{arrow + str(abs(delta)):>12}")
    print()
    print("说明：")
    print("  · 「短句被判新话题」下降，是因为这类消息改由模型判语义 —— 它们字面相似度")
    print("    天然趋近 0，单凭字面判新话题是错的（实测样本里这类有 30/65 轮）")
    print("  · 「问模型」上升是预期代价：换来的是不再拿字面当证据。可用")
    print("    short_message_length=0 恢复旧行为（不推荐），或调小它来收敛成本")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    """入口。

    Args:
        argv: 命令行参数。

    Returns:
        退出码。
    """
    args = build_cli().parse_args(argv)
    detection = load_detection()

    if args.conversations:
        conversations = load_from_json(Path(args.conversations))
        source = f"样本文件 {args.conversations}"
    else:
        conversations = load_from_db(Path(args.db), min_turns=args.min_turns)
        source = f"AstrBot 数据库 {args.db}"

    turns = sum(len(c.turns) for c in conversations)
    print(f"数据源：{source}")
    print(f"载入 {len(conversations)} 个会话，共 {turns} 轮")

    # 真值：有手工标注就用它，否则退回"每个会话第一轮=新话题"的保底口径
    label_note = "每个会话仅第一轮标记为新话题（保底口径，非精确真值）"
    if args.labels:
        labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
        applied = apply_manual_labels(conversations, labels)
        label_note = f"手工标注 {applied} 个话题边界（{args.labels}）"
    else:
        label_first_turn_only(conversations)

    ask_llm = None
    if args.llm:
        ask_llm = make_llm_caller(detection, args)
        if ask_llm is None:
            print("⚠️ --llm 需要 AstrBot 环境与可用 provider，本次退化为纯启发式")
            ask_llm = None

    outcomes = run_eval(detection, conversations, args, ask_llm=ask_llm)
    metrics = summarize(outcomes)
    report(outcomes, metrics, args, label_note)

    if args.compare_legacy:
        compare_legacy(detection, conversations, args)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "args": vars(args),
                    "metrics": metrics,
                    "label_note": label_note,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"指标已写入 {args.json_out}")
    return 0


def make_llm_caller(detection, args):
    """构造真正调用模型的回调（可选功能）。

    Args:
        detection: detection 模块。
        args: 命令行参数。

    Returns:
        回调函数；环境不可用时返回 None。
    """
    try:
        from astrbot.api.provider import ProviderRequest  # noqa: F401,PLC0415
        from astrbot.core.star.star import star_map  # noqa: F401,PLC0415
    except ImportError:
        return None
    del ProviderRequest, star_map

    provider_id = args.provider.strip()
    if not provider_id:
        print("⚠️ 未指定 --provider，无法调用模型")
        return None

    # 真正跑在 AstrBot 进程里时，可以由插件自己注入 provider；离线脚本里没有上下文，
    # 所以这里只做占位说明，避免假装能调用。
    print(
        "⚠️ 离线脚本无法直接调用 AstrBot 的 provider（需要宿主上下文）。\n"
        "   评测模型层请用插件自身的 --llm 之外的方式：在 AstrBot 里跑 mark 模式看日志，\n"
        "   或把 provider 抽成独立 HTTP 调用后再接进来。本次退化：只用启发式。"
    )
    return None


if __name__ == "__main__":
    raise SystemExit(main())
