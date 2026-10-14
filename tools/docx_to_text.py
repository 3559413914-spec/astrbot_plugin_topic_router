"""把 docx 抽成纯文本，便于逐条核对（含表格）。

用法:
    python tools/docx_to_text.py <input.docx> [output.txt]
"""

from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph


def iter_blocks(doc):
    """按文档顺序产出段落与表格。

    Args:
        doc: Document 对象。

    Yields:
        Paragraph 或 Table。
    """
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def main() -> int:
    """入口。

    Returns:
        退出码。
    """
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    doc = Document(src)
    out: list[str] = []
    tables = 0
    for block in iter_blocks(doc):
        if isinstance(block, Paragraph):
            text = block.text.rstrip()
            if text:
                style = block.style.name if block.style is not None else ""
                out.append(f"[{style}] {text}" if style and style != "Normal" else text)
        else:
            tables += 1
            out.append("=== TABLE ===")
            for row in block.rows:
                cells = [c.text.replace("\n", " / ").strip() for c in row.cells]
                out.append(" | ".join(cells))
            out.append("=== /TABLE ===")
    body = "\n".join(out)
    print(body)
    print(f"\n--- 段落/块 {len(out)} 行，表格 {tables} 个 ---")
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(body, encoding="utf-8")
        print(f"已写入 {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
