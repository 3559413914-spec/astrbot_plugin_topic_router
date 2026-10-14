"""自检：确认 docx 里反引号是否被正确消化，并复核 inline 正则。

用法:
    python tools/verify_docx_text.py <docx>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document

INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def main() -> int:
    """入口。

    Returns:
        退出码。
    """
    doc = Document(Path(sys.argv[1]))
    texts = [p.text for p in doc.paragraphs]
    joined = "\n".join(texts)

    print(f"段落数: {len(texts)}")
    print(f"含反引号的行数: {sum(1 for t in texts if '`' in t)}")
    for t in texts:
        if "`" in t:
            print(f"  [反引号残留] {t[:110]}")

    print("\n关键句是否存在（不含反引号形态）:")
    for probe in ("默认模式必须是 mark", "默认模式必须是 `mark`",
                  "_ask_llm() 的所有异常分支", "3.4", "合并规则"):
        print(f"  {probe!r:34} -> {probe in joined}")

    print("\n正则自检:")
    s = "1. **默认模式必须是 `mark`**（只记录、不改上下文）；`auto` 要让用户观察几天后再自己开；"
    print("  输入:", s)
    print("  切分:", INLINE.split(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
