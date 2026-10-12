"""Deterministic, semantic, table-aware chunking of cleaned documents.

SemanticChunker has one responsibility: turn a CleanedDocument into a list of
EvidenceUnitDraft values by splitting at semantic boundaries.  Heading blocks
are boundaries (they never become chunk text themselves); table blocks are
split per-row-group with the normalized header repeated in every child chunk.

This module deliberately reuses the section ownership established by
TextSourceParser / LayoutNormalizer: each body block already carries its
section_path, so the chunker only reads ``block.section_path`` and never
re-derives headings.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from medical_rag.domain.enums import EvidenceKind
from medical_rag.parsing.contracts import CleanedDocument, EvidenceUnitDraft, ParsedBlock

# 中文字符/token 折算基准（历史常量，保留供外部引用；实际计数见 HeuristicTokenCounter）。
CHARS_PER_TOKEN = 1.5

# 硬切 flag；QualityGate（Task 6）据此决定是否需要复核。
FLAG_HARD_SPLIT = "hard_split"

# 指标名可配置来源：环境变量（逗号分隔）优先，未配置时回落到内置冷启动清单。
# 指标名是**部署/数据相关**配置（不同科室检验项目不同），因此绝不写进函数默认参数。
_MEASUREMENT_NAMES_ENV = "MEDICAL_RAG_MEASUREMENT_NAMES"
_BUILTIN_MEASUREMENT_NAMES: tuple[str, ...] = ("血红蛋白", "白细胞")


class TokenCounter(Protocol):
    """文本 → token 数的可注入估算契约（语言/模型相关，故不写死为单一字符近似）。"""

    def count(self, text: str) -> int: ...  # noqa: E704

    def chars_per_token(self, text: str) -> float: ...  # noqa: E704


@dataclass(frozen=True, slots=True)
class HeuristicTokenCounter:
    """语言自适应的字符近似计数器。

    单一 ``CHARS_PER_TOKEN = 1.5`` 对拉丁语系高估约 2.7 倍（英文真实约 4 字符/token），
    导致英文块只有目标大小的 ~1/3。这里按字符脚本分别折算：汉字 ≈ 1.5 字符/token，
    其余（拉丁/数字/标点/空白）≈ 4 字符/token。
    """

    zh_chars_per_token: float = 1.5
    latin_chars_per_token: float = 4.0

    def count(self, text: str) -> int:
        if not text:
            return 0
        han = sum(1 for char in text if _is_han(char))
        latin = len(text) - han
        tokens = han / self.zh_chars_per_token + latin / self.latin_chars_per_token
        return max(1, math.ceil(tokens))

    def chars_per_token(self, text: str) -> float:
        tokens = self.count(text)
        if tokens <= 0:
            return self.latin_chars_per_token
        return len(text) / tokens


def default_measurement_names() -> tuple[str, ...]:
    """指标名清单：环境变量注入优先，未配置时回落到内置冷启动清单。"""
    raw = os.environ.get(_MEASUREMENT_NAMES_ENV, "")
    injected = tuple(part.strip() for part in raw.split(",") if part.strip())
    return injected or _BUILTIN_MEASUREMENT_NAMES


def _is_han(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def estimate_tokens(text: str) -> int:
    """默认（语言自适应）计数器的便捷入口，保持既有模块级 API。"""
    return HeuristicTokenCounter().count(text)


def _split_sentences(text: str) -> list[str]:
    endings = frozenset("。！？；!?;")
    units: list[str] = []
    buffer: list[str] = []
    for char in text:
        buffer.append(char)
        if char in endings:
            units.append("".join(buffer).strip())
            buffer = []
    if buffer:
        tail = "".join(buffer).strip()
        if tail:
            units.append(tail)
    return [unit for unit in units if unit]


def _parse_cell_range(cell_range: str | None) -> tuple[str, str, int, int] | None:
    """把 ``A1:B26`` 解析为 (起点列, 终点列, 起点行, 终点行)；无法解析返回 None。"""
    if not cell_range or ":" not in cell_range:
        return None
    try:
        start, end = cell_range.split(":", 1)
        if not start or not end:
            return None
        start_col = "".join(ch for ch in start if ch.isalpha()).upper()
        end_col = "".join(ch for ch in end if ch.isalpha()).upper()
        start_row = int("".join(ch for ch in start if ch.isdigit()))
        end_row = int("".join(ch for ch in end if ch.isdigit()))
        if not start_col or not end_col or not start_row or not end_row:
            return None
        return (start_col, end_col, start_row, end_row)
    except ValueError:
        return None


def _cell_range_for_rows(cell_range: str | None, start_row: int, end_row: int) -> str | None:
    parsed = _parse_cell_range(cell_range)
    if parsed is None:
        return cell_range
    start_col, end_col, _, _ = parsed
    return f"{start_col}{start_row}:{end_col}{end_row}"


class SemanticChunker:
    """把一个 CleanedDocument 切成语义感知的 EvidenceUnitDraft 列表。

    配置在构造时固定；chunk() 为纯调用——同一输入恒产同一次序/ID 的值。
    """

    def __init__(
        self,
        target_tokens: int = 400,
        min_tokens: int = 80,
        max_tokens: int = 600,
        overlap_ratio: float = 0.12,
        table_rows_per_chunk: int = 10,
        measurement_names: Sequence[str] | None = None,
        token_counter: TokenCounter | None = None,
        version: str = "semantic-chunker-v1",
    ) -> None:
        """配置语义分块。

        分块聚合目标为 target_tokens，max_tokens 是硬上限。min_tokens 是
        收口下限：只有当已累积 token ≥ min_tokens 时才允许按 target 收口；
        当 min_tokens ≤ target_tokens（默认配置即如此）时该条件天然满足，
        因此 **min_tokens 只有在 min_tokens > target_tokens 时才真正生效**，
        用于强制产出更大的块。硬上限 max_tokens 无论 min 取值都优先触发。
        """
        if max_tokens < min_tokens or target_tokens > max_tokens:
            raise ValueError("max_tokens 必须不小于 min_tokens 与 target_tokens")
        if not 0 <= overlap_ratio < 1:
            raise ValueError("overlap_ratio 必须在 [0, 1) 内")
        if table_rows_per_chunk < 1:
            raise ValueError("table_rows_per_chunk 必须为正整数")
        self.target_tokens = target_tokens
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        self.overlap_ratio = overlap_ratio
        self.table_rows_per_chunk = table_rows_per_chunk
        self.measurement_names = (
            tuple(measurement_names)
            if measurement_names is not None
            else default_measurement_names()
        )
        self.token_counter: TokenCounter = token_counter or HeuristicTokenCounter()
        self.version = version

    def chunk(self, document: CleanedDocument) -> list[EvidenceUnitDraft]:
        # raw 层按 block_id 索引（CleanedDocument 保证 raw↔clean 一一配对）：
        # 发射时用它还原 raw_text，使 raw_text/clean_text 真正分层。
        state = _ChunkState(
            raw_text_by_id={block.block_id: block.raw_text for block in document.raw_blocks}
        )
        blocks = sorted(document.cleaned_blocks, key=lambda block: block.reading_order)
        for block in blocks:
            if block.block_type == "heading":
                self._flush_acc(state, document)
                # 标题是边界：切断上一节的 overlap，避免下一节首块混入前节尾段。
                state.prev_text = None
                continue
            if block.block_type == "table":
                self._flush_acc(state, document)
                # 表格是不可横跨的材料边界：切断表前文本的 overlap，表后文本自成一链。
                state.prev_text = None
                self._chunk_table(block, document, state)
                continue
            # 普通文本块：合并到当前累积，直到越过 target（且达 min 下限）或顶到 max 硬上限。
            block_tokens = self._count_tokens(block.raw_text)
            if block_tokens > self.max_tokens:
                self._flush_acc(state, document)
                self._chunk_oversized_block(block, document, state)
                continue
            if state.acc and self._should_flush(
                state.acc_text(), block_tokens
            ):
                self._flush_acc(state, document)
            state.acc.append(block)
        self._flush_acc(state, document)
        return state.drafts

    def _count_tokens(self, text: str) -> int:
        """当前计数器下的 token 数（唯一入口，便于注入真实 tokenizer）。"""
        return self.token_counter.count(text)

    def _should_flush(self, acc_text: str, next_tokens: int) -> bool:
        """累积是否应在此处收口为一块：顶到 max 必 flush；越过 target 且过 min 才 flush。"""
        current = self._count_tokens(acc_text)
        if current + next_tokens > self.max_tokens:
            return True
        return (
            current >= self.min_tokens
            and current >= self.target_tokens
            and current + next_tokens > self.target_tokens
        )

    # ---- 文本累积与发射 ----

    def _flush_acc(self, state: _ChunkState, document: CleanedDocument) -> None:
        if not state.acc:
            return
        text = state.acc_text()
        first = state.acc[0]
        self._emit(
            state,
            text=text,
            raw_text=state.acc_raw_text(),
            kind=EvidenceKind.REPORT_TEXT,
            block=first,
            document=document,
            parent_block_id=None,
            quality_flags=(),
            apply_overlap=True,
        )
        state.acc = []

    def _chunk_oversized_block(
        self, block: ParsedBlock, document: CleanedDocument, state: _ChunkState
    ) -> None:
        for piece, was_hard_split in self._split_long_block(block.raw_text):
            self._emit(
                state,
                text=piece,
                raw_text=state.raw_text_by_id.get(block.block_id, block.raw_text),
                kind=EvidenceKind.REPORT_TEXT,
                block=block,
                document=document,
                parent_block_id=block.block_id,
                quality_flags=(FLAG_HARD_SPLIT,) if was_hard_split else (),
                apply_overlap=True,
            )

    # ---- 表格分块 ----

    def _group_table_rows(
        self, header: str, rows: list[tuple[str, int]]
    ) -> list[list[tuple[str, int]]]:
        """按 ``table_rows_per_chunk`` 与 ``max_tokens`` 预算把数据行分组（每组非空）。

        ``max_tokens`` 是**所有**块的硬上限，表格子块也不例外：行长 × 行数超预算时必须
        提前收口，否则会产出超过模型窗口的块。
        """
        groups: list[list[tuple[str, int]]] = []
        current: list[tuple[str, int]] = []
        for row in rows:
            if current and (
                len(current) >= self.table_rows_per_chunk
                or self._count_tokens(
                    "\n".join([header, *(line for line, _ in current), row[0]])
                )
                > self.max_tokens
            ):
                groups.append(current)
                current = [row]
            else:
                current.append(row)
        if current:
            groups.append(current)
        return groups

    def _chunk_table(self, block: ParsedBlock, document: CleanedDocument, state: _ChunkState) -> None:
        lines = [line for line in block.raw_text.splitlines() if line.strip()]
        if not lines:
            return
        header, data_rows = lines[0], lines[1:]
        parsed = _parse_cell_range(block.cell_range)
        # 匹配行与常规行**互斥**：匹配行只出现在指标聚焦块里，常规子块不再重复同一批行。
        matched: list[tuple[str, int]] = []
        regular: list[tuple[str, int]] = []
        for index, line in enumerate(data_rows):
            absolute_row = index + 2  # 表头占绝对行 1，数据行 index i 位于绝对行 i+2
            target = (
                matched
                if any(name in line for name in self.measurement_names)
                else regular
            )
            target.append((line, absolute_row))
        raw_text = state.raw_text_by_id.get(block.block_id, block.raw_text)
        emitted_group = False
        for group in self._group_table_rows(header, regular):
            # 表头在第 1 行；首组 range 含表头行，后续组只描述其数据行区间
            # （表头在每个子块的文本中重复，cell_range 描述数据主区间）。
            start_abs = 1 if not emitted_group else group[0][1]
            end_abs = group[-1][1]
            cell_range = (
                _cell_range_for_rows(block.cell_range, start_abs, end_abs)
                if parsed
                else block.cell_range
            )
            self._emit(
                state,
                text="\n".join([header, *(line for line, _ in group)]),
                raw_text=raw_text,
                kind=EvidenceKind.REPORT_TABLE,
                block=block,
                document=document,
                parent_block_id=block.block_id,
                quality_flags=(),
                apply_overlap=False,
                cell_range=cell_range,
            )
            emitted_group = True
        for group in self._group_table_rows(header, matched):
            # 指标聚焦块：只含匹配行（区间缩到匹配行跨度），同样受 max_tokens 预算约束。
            fact_range = (
                _cell_range_for_rows(block.cell_range, 1, group[-1][1])
                if parsed
                else block.cell_range
            )
            self._emit(
                state,
                text="\n".join([header, *(line for line, _ in group)]),
                raw_text=raw_text,
                kind=EvidenceKind.REPORT_TABLE,
                block=block,
                document=document,
                parent_block_id=block.block_id,
                quality_flags=(),
                apply_overlap=False,
                cell_range=fact_range,
            )

    # ---- 长块内部切分 ----

    def _split_long_block(self, text: str) -> list[tuple[str, bool]]:
        """句子级切一个超长块；每个片段附带是否由硬切产生（last resort）。

        优先句子边界；句子越 target 且过 min 才收口，顶到 max 强制收口；
        无点标点长串交叉兜底硬切并记录 hard_split flag。
        """
        pieces: list[str] = []
        current = ""
        for unit in _split_sentences(text):
            merged = (
                self._count_tokens(f"{current}\n{unit}".strip())
                if current
                else self._count_tokens(unit)
            )
            if current and (
                merged > self.max_tokens
                or (
                    self._count_tokens(current) >= self.min_tokens
                    and merged > self.target_tokens
                )
            ):
                pieces.append(current)
                current = unit
            else:
                current = unit if not current else f"{current}\n{unit}".strip()
        if current:
            pieces.append(current)
        result: list[tuple[str, bool]] = []
        for piece in pieces:
            if self._count_tokens(piece) > self.max_tokens:
                result.extend(
                    (fragment, True) for fragment in self._hard_split_piece(piece)
                )
            else:
                result.append((piece, False))
        return result

    def _hard_split_piece(self, piece: str) -> list[str]:
        """按 token 预算硬切（最后手段）：每片段都满足 ``count <= max_tokens``。

        字符预算只用于**估算切点**，最终以计数器校验并收缩——纯字符近似会因片段内
        脚本比例不同而越界（旧实现正是如此）。
        """
        fragments: list[str] = []
        remaining = piece
        while remaining:
            if self._count_tokens(remaining) <= self.max_tokens:
                fragments.append(remaining.strip())
                break
            estimated = int(
                self.max_tokens * self.token_counter.chars_per_token(remaining)
            )
            cut = max(1, min(len(remaining), estimated))
            while cut > 1 and self._count_tokens(remaining[:cut]) > self.max_tokens:
                cut -= 1
            fragments.append(remaining[:cut].strip())
            remaining = remaining[cut:]
        return [fragment for fragment in fragments if fragment]

    # ---- 通用发射（处理 overlap、索引与草稿构造） ----

    def _emit(
        self,
        state: _ChunkState,
        *,
        text: str,
        raw_text: str,
        kind: EvidenceKind,
        block: ParsedBlock,
        document: CleanedDocument,
        parent_block_id: str | None,
        quality_flags: tuple[str, ...],
        apply_overlap: bool,
        cell_range: str | None = None,
    ) -> None:
        if not text:
            return
        clean_text = text
        if apply_overlap and state.prev_text and self.overlap_ratio > 0:
            prefix = self._bounded_overlap_prefix(state.prev_text, text)
            if prefix:
                clean_text = f"{prefix}\n{text}"
        state.drafts.append(
            EvidenceUnitDraft(
                text=clean_text,
                kind=kind,
                page_number=block.page_number,
                sheet_name=block.sheet_name,
                cell_range=cell_range if cell_range is not None else block.cell_range,
                source_anchor=block.source_anchor,
                language=block.language,
                confidence=block.confidence,
                raw_text=raw_text,
                clean_text=clean_text,
                section_path=block.section_path,
                parent_block_id=parent_block_id,
                chunk_index=state.chunk_index,
                parser_version=None,
                cleaner_version=document.cleaner_version,
                chunker_version=self.version,
                quality_flags=quality_flags,
                retrieval_eligible=True,
            )
        )
        state.chunk_index += 1
        if apply_overlap:
            state.prev_text = text

    def _bounded_overlap_prefix(self, previous: str, text: str) -> str:
        """上一块尾部的 overlap 前缀，且**计入 max_tokens 预算**。

        旧实现先按预算切好 text，再额外前置 12% 的上一块尾部，导致输出块可以超过
        max_tokens（硬上限名不副实）。这里把前缀收缩到
        ``count(prefix + "\\n" + text) <= max_tokens``，前缀放不下就不加。
        """
        desired = int(len(previous) * self.overlap_ratio)
        if desired <= 0:
            return ""
        budget = self.max_tokens - self._count_tokens(f"\n{text}")
        if budget <= 0:
            return ""
        candidate = previous[-desired:]
        allowed = int(budget * self.token_counter.chars_per_token(candidate))
        if allowed < len(candidate):
            candidate = candidate[-allowed:] if allowed > 0 else ""
        while candidate and self._count_tokens(candidate) > budget:
            candidate = candidate[1:]
        return candidate.strip()


@dataclass
class _ChunkState:
    """chunk() 单次调用的可变累积；配置与状态分离，保证 chunker 可复用且可重入。"""

    drafts: list[EvidenceUnitDraft] = field(default_factory=list)
    chunk_index: int = 0
    prev_text: str | None = None
    acc: list[ParsedBlock] = field(default_factory=list)
    # block_id → raw 文本（来自 CleanedDocument.raw_blocks）：用于 raw/clean 分层。
    raw_text_by_id: dict[str, str] = field(default_factory=dict)

    def acc_text(self) -> str:
        return "\n\n".join(
            block.raw_text for block in self.acc if block.raw_text.strip()
        )

    def acc_raw_text(self) -> str:
        return "\n\n".join(
            self.raw_text_by_id.get(block.block_id, block.raw_text)
            for block in self.acc
            if block.raw_text.strip()
        )
