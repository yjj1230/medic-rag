"""确定性文本清洗：原地归一化 + 稳定 flag，原文永不覆盖（设计 §5 清洗原则）。

操作顺序：NFKC → 不可见字符移除 → 空白归一化 → （仅 OCR 来源）OCR 断行合并 →
（文档级）重复行检测 → 表格分隔符归一化。不确定文本只打 flag，绝不删除。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import replace

from medical_rag.parsing.contracts import (
    CleanedDocument,
    ParsedBlock,
    ParsedDocument,
    SourceOffset,
)
from medical_rag.parsing.filetypes import IMAGE_FILE_TYPES
from medical_rag.parsing.layout import LayoutNormalizer

# 稳定清洗 flag（只在此定义一次）。三类语义：
# - 应用型（文本被实际修改）：fullwidth_normalized / invisible_character_removed /
#   whitespace_normalized / ocr_line_joined / table_delimiter_normalized；
# - 检测型（仅标记、不删除任何内容），**两种不同强度**：
#   * repeated_template_text —— **结构性页眉页脚**：同一行出现在 ≥2 个块的块首/块尾
#     且跨页（无页码信息时退化为跨块），且不是表格行（含 "|"）。这是"模板"信号；
#   * repeated_lines —— **重复行**（弱信号）：任意归一化行出现 >1 次，包含检验单里
#     天然重复的「参考范围/单位」等表格内重复，不构成页眉页脚判断；
# - 溯源型（仅记录检查已执行，不改变文本）：ocr_noise_checked —— 图像/OCR 来源检查。
FLAG_FULLWIDTH_NORMALIZED = "fullwidth_normalized"
FLAG_INVISIBLE_CHARACTER_REMOVED = "invisible_character_removed"
FLAG_WHITESPACE_NORMALIZED = "whitespace_normalized"
FLAG_OCR_LINE_JOINED = "ocr_line_joined"
FLAG_REPEATED_TEMPLATE_TEXT = "repeated_template_text"
FLAG_REPEATED_LINES = "repeated_lines"
FLAG_TABLE_DELIMITER_NORMALIZED = "table_delimiter_normalized"
FLAG_OCR_NOISE_CHECKED = "ocr_noise_checked"

_REMOVABLE_CATEGORIES = frozenset({"Cc", "Cf"})  # 控制符 + 格式符（零宽/双向/BOM 等）
_STRUCTURE_WHITESPACE = "\t\n\r"  # 结构性空白必须保留
_WHITESPACE_RUN = re.compile(r"\s+")
_SENTENCE_ENDINGS = tuple("。！？；…!?;.")

# 检验单单位词表（表格行启发式保护用；与 RuleBasedMedicalFactExtractor 的单位集合独立，
# 这里只用于判定"这一行是不是一条数据行"）。
_UNIT_PATTERN = (
    r"(?:g/L|g/dL|mg/dL|mg/L|mmol/L|μmol/L|umol/L|U/L|mIU/L|IU/L|"
    r"10\^?\d+/L|x10\^?\d+/L|%|mmHg|kg|cm|次/分)"
)
# 前行以「数值(+单位)」收尾。
_TRAILING_VALUE = re.compile(
    r"(?:\d+(?:[.,]\d+)?\s*" + _UNIT_PATTERN + r"|\d+(?:[.,]\d+)?|" + _UNIT_PATTERN + r")\s*$"
)
# 下一行以数值或单位开头（另一条数据行）。
_LEADING_VALUE = re.compile(r"^\s*(?:[+-]?\d|" + _UNIT_PATTERN + r")")
# 下一行是「指标名 + 数值」模式（一条完整检验记录）。
_MEASUREMENT_ROW = re.compile(r"^\s*[\u4e00-\u9fffA-Za-z][^\n]{0,24}?[ \t]*[:：]?[ \t]*[+-]?\d")


def _is_han(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _is_ocr_file_type(file_type: str) -> bool:
    # "image" 与 "image/*" 是 OCR 解析结果的别名/前缀；扩展名与 MIME 走 canonical 集合。
    value = file_type.lower().strip()
    return value == "image" or value.startswith("image/") or value in IMAGE_FILE_TYPES


def _normalize_nfkc(text: str) -> tuple[str, str | None]:
    normalized = unicodedata.normalize("NFKC", text)
    return normalized, FLAG_FULLWIDTH_NORMALIZED if normalized != text else None


def _remove_invisible_characters(text: str) -> tuple[str, str | None]:
    cleaned = "".join(
        char
        for char in text
        if char in _STRUCTURE_WHITESPACE
        or unicodedata.category(char) not in _REMOVABLE_CATEGORIES
    )
    return cleaned, FLAG_INVISIBLE_CHARACTER_REMOVED if cleaned != text else None


def _normalize_whitespace(text: str) -> tuple[str, str | None]:
    normalized = "\n".join(_WHITESPACE_RUN.sub(" ", line).strip() for line in text.split("\n"))
    return normalized, FLAG_WHITESPACE_NORMALIZED if normalized != text else None


def _join_ocr_lines(text: str) -> tuple[str, str | None]:
    """合并被 OCR 换行截断的行（**只用于 OCR 来源**）。

    非 OCR 来源（原生 PDF/DOCX/电子表格）的多行文本是结构性换行：把它们合并会毁掉
    表格结构，使下游 ``semantic_chunker`` 依赖 ``splitlines()`` 的表格感知分块失效。

    启发式保护（即使 OCR 来源也不合并）：下一行本身是「指标名 + 数值」记录，或前行
    以数值/单位收尾而下一行又是数值行 —— 这两种情况下合并会把两条检验记录粘成一条。
    """
    joined: list[str] = []
    joined_any = False
    for line in text.split("\n"):
        if (
            joined
            and joined[-1]
            and line
            and not joined[-1].endswith(_SENTENCE_ENDINGS)
            and "|" not in joined[-1]
            and "|" not in line
            and not _looks_like_separate_table_rows(joined[-1], line)
        ):
            separator = "" if _is_han(joined[-1][-1]) or _is_han(line[0]) else " "
            joined[-1] = f"{joined[-1]}{separator}{line}"
            joined_any = True
        else:
            joined.append(line)
    if not joined_any:
        return text, None
    return "\n".join(joined), FLAG_OCR_LINE_JOINED


def _looks_like_separate_table_rows(previous: str, line: str) -> bool:
    """判断两行是否分别是独立的检验数据行（合并会串行）。"""
    if _MEASUREMENT_ROW.match(line):
        return True
    return bool(_TRAILING_VALUE.search(previous) and _LEADING_VALUE.match(line))


def _normalize_table_delimiters(text: str) -> tuple[str, str | None]:
    lines: list[str] = []
    changed = False
    for line in text.split("\n"):
        normalized = _normalize_table_row(line) if "|" in line else line
        changed = changed or normalized != line
        lines.append(normalized)
    if not changed:
        return text, None
    return "\n".join(lines), FLAG_TABLE_DELIMITER_NORMALIZED


def _normalize_table_row(line: str) -> str:
    leading = "|" if line.startswith("|") else ""
    trailing = "|" if line.endswith("|") else ""
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    return f"{leading}{' | '.join(cells)}{trailing}"


# 逐块文本操作（顺序即应用顺序；重复模板检测是文档级分析，见 clean()）。
# ``_join_ocr_lines`` 不在其中：它只对 OCR 来源生效（见 clean()）。
_TEXT_OPERATIONS = (
    _normalize_nfkc,
    _remove_invisible_characters,
    _normalize_whitespace,
    _normalize_table_delimiters,
)

_OCR_TEXT_OPERATIONS = (
    _normalize_nfkc,
    _remove_invisible_characters,
    _normalize_whitespace,
    _join_ocr_lines,
    _normalize_table_delimiters,
)


def _clean_text(text: str, *, ocr_source: bool) -> tuple[str, tuple[str, ...]]:
    flags: list[str] = []
    for operation in _OCR_TEXT_OPERATIONS if ocr_source else _TEXT_OPERATIONS:
        text, flag = operation(text)
        if flag is not None:
            flags.append(flag)
    return text, tuple(flags)


def _normalized_lines(block: ParsedBlock) -> list[str]:
    return [normalized for line in block.raw_text.split("\n") if (normalized := " ".join(line.split()))]


def _has_repeated_lines(raw_blocks: tuple[ParsedBlock, ...]) -> bool:
    """重复行（弱信号）：任一归一化行出现 >1 次，含表格内天然重复的「参考范围/单位」。"""
    counts: Counter[str] = Counter()
    for block in raw_blocks:
        counts.update(_normalized_lines(block))
    return any(count > 1 for count in counts.values())


def _has_structural_template_lines(raw_blocks: tuple[ParsedBlock, ...]) -> bool:
    """结构性页眉页脚：同一行位于 ≥2 个块的**块首/块尾**，且跨页（无页码时跨块）。

    两个收紧条件共同避免检验单误伤：
    - 只认块首/块尾位置（页中重复的「参考范围」不算）；
    - 表格行（含 "|"）不算页眉页脚（表格表头跨页重复是正常排版）。
    """
    occurrences: dict[str, set[int]] = defaultdict(set)
    pages: dict[str, set[int | None]] = defaultdict(set)
    for index, block in enumerate(raw_blocks):
        lines = _normalized_lines(block)
        if not lines:
            continue
        for line in {lines[0], lines[-1]}:
            if "|" in line:
                continue
            occurrences[line].add(index)
            pages[line].add(block.page_number)
    for line, block_indexes in occurrences.items():
        if len(block_indexes) < 2:
            continue
        page_ids = pages[line]
        if page_ids == {None} or len(page_ids) >= 2:
            return True
    return False


class DocumentCleaner:
    """v1 清洗器：raw↔clean 块一一对应（共享 block_id），不确定文本只标记不删除。"""

    def __init__(self, version: str = "cleaner-v1") -> None:
        self.version = version

    def clean(self, source: ParsedDocument | tuple[ParsedBlock, ...]) -> CleanedDocument:
        page_total: int | None = None
        if isinstance(source, ParsedDocument):
            raw_blocks: tuple[ParsedBlock, ...] = LayoutNormalizer().normalize(source)
            ocr_source = _is_ocr_file_type(source.file_type)
            page_total = source.page_total
        elif isinstance(source, tuple):
            raw_blocks = source
            ocr_source = False  # 预构建块来自结构化文本链路（无 OCR 上下文）
        else:
            raise TypeError("clean() 接受 ParsedDocument 或 ParsedBlock 元组")

        cleaning_flags: list[str] = []
        cleaned_blocks: list[ParsedBlock] = []
        source_offsets: list[SourceOffset] = []
        for block in raw_blocks:
            cleaned_text, block_flags = _clean_text(block.raw_text, ocr_source=ocr_source)
            cleaned_blocks.append(replace(block, raw_text=cleaned_text))
            source_offsets.append(
                SourceOffset(
                    block_id=block.block_id,
                    raw_start=0,
                    raw_end=len(block.raw_text),
                    clean_start=0,
                    clean_end=len(cleaned_text),
                )
            )
            cleaning_flags.extend(flag for flag in block_flags if flag not in cleaning_flags)
        # 模板行检测读取 raw 行（文档级）：行合并会抹掉重复证据，且页眉/页脚的重复
        # 本质上跨块（跨页）出现，只能在原始行清单上按归一化频率判定。
        if _has_repeated_lines(raw_blocks):
            cleaning_flags.append(FLAG_REPEATED_LINES)
        if _has_structural_template_lines(raw_blocks):
            cleaning_flags.append(FLAG_REPEATED_TEMPLATE_TEXT)
        if ocr_source:
            cleaning_flags.append(FLAG_OCR_NOISE_CHECKED)
        return CleanedDocument(
            raw_blocks=tuple(raw_blocks),
            cleaned_blocks=tuple(cleaned_blocks),
            source_offsets=tuple(source_offsets),
            cleaning_flags=tuple(cleaning_flags),
            cleaner_version=self.version,
            page_total=page_total,
        )
