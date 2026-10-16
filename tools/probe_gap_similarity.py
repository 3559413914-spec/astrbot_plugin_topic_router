"""实测 time_gap_minutes 取不同值时 gap 分支的真实行为。

关键：state 必须有非零 last_ts，否则 gap 分支根本不进入（`if last_ts and ...`）。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import detection  # noqa: E402

MSG = "今天天气怎么样"


def mk_state(last_ts: float) -> SimpleNamespace:
    """造一个"已有话题、上一条消息在 last_ts"的状态。

    Args:
        last_ts: 上一条消息时间戳。

    Returns:
        状态对象。
    """
    return SimpleNamespace(
        label="Python 批量转表格",
        summary="",
        last_user_text="帮我写个 Python 脚本处理 csv",
        last_ts=last_ts,
        last_assistant_text="",
    )


NOW = 10000.0
print(f"{'time_gap':>9} {'真实间隔(分)':>12}  {'needs_llm':>9}  reason")
print("-" * 78)
for gap_cfg, actual_min in ((45, 60), (60, 30), (0, 60), (0, 1), (-1, 60), (0, 0)):
    state = mk_state(NOW - actual_min * 60)
    r = detection.heuristic(MSG, state, now=NOW, time_gap_minutes=gap_cfg)
    hit_gap = "距上一条消息" in r.reason
    print(
        f"{gap_cfg:>9} {actual_min:>12}  {str(r.needs_llm):>9}  "
        f"{'[gap分支命中] ' if hit_gap else '[未命中gap]  '}{r.reason[:52]}"
    )
