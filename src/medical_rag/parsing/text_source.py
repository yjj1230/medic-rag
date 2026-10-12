"""结构化文本（markdown / 中文标题）→ ParsedBlock 的确定性封装。

纯文本来源：不产生 OCR 标记，也不编造页码 / bbox / source_anchor（全部保持 None）。
"""

from __future__ import annotations

import re

from medical_rag.parsing.contracts import ParsedBlock
from medical_rag.parsing.layout import make_block_id

# 公共标题模式：markdown #{1,6} + 中文 第X章/节/条（语义与 KnowledgeChunker._heading 一致）。
# 调用方（parse）先 strip 再匹配，故不保留 ^\s{0,3} 前导空格容差；
# KnowledgeChunker 同样先 strip，Task 4 统一到本链路并移除其私有正则副本。
HEADING_PATTERN = re.compile(
    r"^(?:(?P<hashes>#{1,6})\s+(?P<md>.+?)"
    r"|第[一二三四五六七八九十0-9]+(?P<unit>[章节条])\s*(?P<cn>.+?))\s*$"
)

# 中文标题层级：章 > 节 > 条。
_CN_HEADING_LEVELS = {"章": 1, "节": 2, "条": 3}

_SectionStack = tuple[tuple[int, str], ...]


def _extend_section(section: _SectionStack, match: re.Match[str]) -> _SectionStack:
    """返回压入标题后的新章节栈（纯函数）：弹出同级及更深层的旧标题。"""
    if match.group("md") is not None:
        level = len(match.group("hashes"))
        title = match.group("md").strip()
    else:
        level = _CN_HEADING_LEVELS[match.group("unit")]
        title = match.group("cn").strip()
    kept = [entry for entry in section if entry[0] < level]
    kept.append((level, title))
    return tuple(kept)


def _make_block(
    index: int,
    block_type: str,
    raw_text: str,
    section_path: tuple[str, ...],
) -> ParsedBlock:
    return ParsedBlock(
        block_id=make_block_id(index),
        block_type=block_type,
        raw_text=raw_text,
        section_path=section_path,
        reading_order=index,
    )


class TextSourceParser:
    """把结构化文本解析为 heading / paragraph / table 块（供清洗链路直接消费）。"""

    def parse(self, text: str) -> tuple[ParsedBlock, ...]:
        blocks: list[ParsedBlock] = []
        section: _SectionStack = ()
        current: list[str] = []
        in_table = False

        def flush() -> None:
            nonlocal current, in_table
            body = "\n".join(current).strip()
            if body:
                # 表格区域内的空白行不打断表格（与 KnowledgeChunker 一致）：
                # 仅隔一个空行的两个表格因此合并为一个 table 块（见钉住该行为的测试）。
                block_type = "table" if in_table else "paragraph"
                path = tuple(title for _, title in section)
                blocks.append(_make_block(len(blocks), block_type, body, path))
            current = []
            in_table = False

        for raw_line in text.splitlines():
            line = raw_line.strip()
            heading = HEADING_PATTERN.match(line)
            if heading:
                flush()
                section = _extend_section(section, heading)
                path = tuple(title for _, title in section)
                blocks.append(_make_block(len(blocks), "heading", line, path))
                continue
            if not line:
                if not in_table:
                    flush()
                continue
            table_line = line.startswith("|")
            if table_line != in_table:
                flush()
                in_table = table_line
            current.append(line)
        flush()
        return tuple(blocks)
