"""QualityGate: single-responsibility gate over parsed/cleaned content.

Thresholds live in ONE ``QualityPolicy`` object (conservative pre-golden-set
values, 设计 §9 cold start) and are never scattered as literals across the
parser/index-job code.  Metric functions are private and pure so they are
independently testable.  ``QualityGate.evaluate`` returns a complete
``QualityDecision`` (status + reasons + full metrics) for observability.

DECISION MAP (coupled to Step-3 persistence):
  PASS            -> document/version READY, SEARCHABLE IndexJob permitted.
  NEEDS_REVIEW    -> soft threshold breach (anchor/confidence/hard_split/...);
                     content retained (write-but-flag), retrieval_eligible=False,
                     no SEARCHABLE IndexJob created.
  FAIL            -> structural failure (e.g. empty document); content retained
                     (write-but-flag), retrieval_eligible=False, stable error
                     code, no SEARCHABLE IndexJob created, source preserved for
                     reprocessing.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from collections import Counter

from medical_rag.domain.enums import QualityStatus
from medical_rag.parsing.contracts import CleanedDocument, EvidenceUnitDraft, MedicalFactDraft
from medical_rag.quality.contracts import QualityDecision

# 贯穿门禁 → reasons → 持久化（quality_flags）的稳定原因键。
REASON_EMPTY_DOCUMENT = "empty_document"
REASON_MISSING_SOURCE_ANCHOR = "missing_source_anchor"
REASON_LOW_TEXT_COVERAGE = "low_text_coverage"
REASON_LOW_NON_EMPTY_BLOCK_RATIO = "low_non_empty_block_ratio"
REASON_LOW_OCR_CONFIDENCE = "low_ocr_confidence"
REASON_ABNORMAL_CHARACTERS = "abnormal_characters"
REASON_DUPLICATE_BLOCKS = "duplicate_blocks"
REASON_TABLE_COLUMN_INCONSISTENT = "table_column_inconsistent"
REASON_LOW_FACT_COVERAGE = "low_fact_coverage"
REASON_HARD_SPLIT = "hard_split"
REASON_MISSING_PAGES = "missing_pages"
REASON_CONFIDENCE_UNAVAILABLE = "confidence_unavailable"

_SUSPICIOUS_CATEGORIES = frozenset({"Cc", "Cf"})  # 控制字符 + 格式字符
_REPLACEMENT_CHAR = "\ufffd"
_STRUCTURAL_WHITESPACE = frozenset("\t\n\r ")


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """Named decision thresholds, defined once.

    Defaults are deliberately conservative for the pre-golden-set cold start
    (合成/脱敏黄金集先行，人工复核前 50 份真实文档后再收紧；设计 §9).  All values
    are single-sourced here; callers read ``policy.min_anchor_coverage`` etc.
    and never re-declare literals.
    """

    min_text_coverage: float = 0.85
    min_non_empty_block_ratio: float = 0.90
    min_anchor_coverage: float = 1.0
    max_abnormal_character_ratio: float = 0.02
    max_duplicate_block_ratio: float = 0.25
    min_table_column_consistency: float = 0.80
    min_ocr_confidence: float = 0.80
    min_fact_coverage: float = 0.0  # 事实抽取对复核门禁是可选的
    max_hard_split_ratio: float = 0.0  # 任何硬切分块都会触发复核
    # 结构性页覆盖下限：声明页数中被证据真正覆盖的比例低于此值即 FAIL（内容确实缺失）。
    min_page_coverage: float = 0.5


# ---------------------------------------------------------------------------
# 私有的纯度量函数（可独立测试）。
# ---------------------------------------------------------------------------


def _non_empty_blocks(document: CleanedDocument) -> list[str]:
    return [b.raw_text for b in document.cleaned_blocks if b.raw_text.strip()]


def _significant_chars(text: str) -> int:
    """非空白字符数：空白差异不代表内容丢失，按"有实际内容的字符"计。"""
    return len("".join(text.split()))


def _text_coverage(document: CleanedDocument, evidence: list[EvidenceUnitDraft]) -> float:
    """原始文本 → 可用证据的覆盖率（端到端，而非 raw↔clean 字符比）。

    清洗器 v1 契约保证 raw↔clean 一一对应（只做原地归一化），因此旧口径
    ``clean_chars / raw_chars`` 恒 ≈1，无论链路上丢了多少内容都报 1.0（空转指标）。
    真正会吞内容的是"解析→清洗→分块→持久化证据"整条链，所以这里比较**原始块的
    非空白字符总量**与**最终证据文本的非空白字符总量**；重叠（overlap）会让比值
    >1，截到 1.0。无证据视图（调用方只给了 document）时退回 raw↔clean 比，不凭空
    判定内容丢失。

    分母只计**内容块**：``heading`` 块是分块边界而非块内容（其文本保存在证据的
    ``section_path`` 里，从不进入 chunk 文本），把它计进分母会把"按设计不进正文的
    标题"误报成内容丢失。
    """
    content_blocks = [
        block for block in document.raw_blocks if block.block_type != "heading"
    ]
    raw_chars = sum(_significant_chars(block.raw_text) for block in content_blocks)
    if raw_chars <= 0:
        return 1.0
    if not evidence:
        cleaned_chars = sum(
            _significant_chars(block.raw_text)
            for block in document.cleaned_blocks
            if block.block_type != "heading"
        )
        return min(1.0, cleaned_chars / raw_chars)
    produced_chars = sum(
        _significant_chars(item.clean_text or item.text or "") for item in evidence
    )
    return min(1.0, produced_chars / raw_chars)


def _non_empty_block_ratio(document: CleanedDocument) -> float:
    total = len(document.cleaned_blocks)
    if total == 0:
        return 0.0
    non_empty = sum(1 for b in document.cleaned_blocks if b.raw_text.strip())
    return non_empty / total


def _abnormal_character_ratio(document: CleanedDocument) -> float:
    total = 0
    abnormal = 0
    for block in document.cleaned_blocks:
        for char in block.raw_text:
            total += 1
            if char == _REPLACEMENT_CHAR:
                abnormal += 1
            elif char in _STRUCTURAL_WHITESPACE:
                continue
            elif unicodedata.category(char) in _SUSPICIOUS_CATEGORIES:
                abnormal += 1
    if total == 0:
        return 0.0
    return abnormal / total


def _duplicate_block_ratio(document: CleanedDocument) -> float:
    texts = [b.raw_text.strip() for b in document.cleaned_blocks if b.raw_text.strip()]
    total = len(texts)
    if total <= 1:
        return 0.0
    counts = Counter(texts)
    duplicated = sum(1 for text in texts if counts[text] > 1)
    return duplicated / total


def _row_cell_counts(lines: list[str]) -> list[int]:
    counts: list[int] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        cells = [c for c in stripped.split("|") if c.strip()]
        counts.append(len(cells))
    return counts


def _table_column_consistency(document: CleanedDocument) -> float:
    """逐表列一致性：取各表一致性的最小值，因此一个
    参差不齐的表会把整份文档拉低（fail-soft 倾向复核）。"""
    tables = [b for b in document.cleaned_blocks if b.block_type == "table"]
    if not tables:
        return 1.0
    consistencies: list[float] = []
    for table in tables:
        scores = _row_cell_counts(table.raw_text.splitlines())
        if len(scores) <= 1 or not all(scores):
            consistencies.append(1.0)
            continue
        mode = Counter(scores).most_common(1)[0][0]
        consistent = sum(1 for value in scores if value == mode)
        consistencies.append(consistent / len(scores))
    return min(consistencies)


def _confidence_values(document: CleanedDocument, evidence: list[EvidenceUnitDraft]) -> list[float]:
    values: list[float] = []
    for block in document.cleaned_blocks:
        if block.confidence is not None:
            values.append(float(block.confidence))
    for item in evidence:
        if item.confidence is not None:
            values.append(float(item.confidence))
    return values


def _ocr_confidence(document: CleanedDocument, evidence: list[EvidenceUnitDraft]) -> float:
    """可用置信度的最小值。

    **没有任何可用置信度时返回 0.0 作为占位**（metrics 的契约是 float，不能放 None）：
    请始终结合 ``confidence_unavailable_ratio`` 解读——该比值 >0 时门禁按"不可判定"
    报 ``confidence_unavailable``，并**不再**用 0.0 去报 low_ocr_confidence（避免把
    "没测出来"伪装成"测出来很低"）。
    """
    values = _confidence_values(document, evidence)
    if not values:
        return 0.0
    return min(values)


def _confidence_unavailable_ratio(
    document: CleanedDocument, evidence: list[EvidenceUnitDraft]
) -> float:
    """未提供置信度的单元占比（0.0 = 全部可用，1.0 = 完全不可判定）。

    只统计有实际内容的单元；证据为空时退回块视图。
    """
    values: list[float | None]
    if evidence:
        values = [item.confidence for item in evidence if (item.text or "").strip()]
    else:
        values = [block.confidence for block in document.cleaned_blocks if block.raw_text.strip()]
    if not values:
        return 0.0
    return sum(1 for value in values if value is None) / len(values)


def _page_coverage(document: CleanedDocument, evidence: list[EvidenceUnitDraft]) -> float:
    """声明页数中被证据真正覆盖的页占比。

    ``page_total is None``（无法确认页数）=> 返回 1.0 且门禁**不判定**缺页：
    fail-closed 到"不判定"，绝不假定"已覆盖"。页码越界的证据（例如 2 页文档里的
    p9）既不计入覆盖，也不允许把 1/2 虚报成 2/2。
    """
    total = document.page_total
    if not total or total <= 0:
        return 1.0
    source: list[int | None]
    if evidence:
        source = [item.page_number for item in evidence]
    else:
        source = [block.page_number for block in document.cleaned_blocks]
    covered = {
        page for page in source if page is not None and 1 <= page <= total
    }
    return min(1.0, len(covered) / total)


def _anchor_coverage(document: CleanedDocument, evidence: list[EvidenceUnitDraft]) -> float:
    if evidence:
        total = len(evidence)
        covered = sum(1 for item in evidence if (item.source_anchor or "").strip())
    else:
        blocks = document.cleaned_blocks
        total = len(blocks)
        covered = sum(1 for block in blocks if (block.source_anchor or "").strip())
    if total == 0:
        return 1.0
    return covered / total


def _fact_coverage(evidence: list[EvidenceUnitDraft], facts: list[MedicalFactDraft]) -> float:
    if not evidence:
        return 1.0
    covered_indices = {fact.evidence_index for fact in facts}
    covered = sum(1 for idx in range(len(evidence)) if idx in covered_indices)
    return covered / len(evidence)


def _hard_split_ratio(evidence: list[EvidenceUnitDraft]) -> float:
    if not evidence:
        return 0.0
    flag_count = sum(1 for item in evidence if "hard_split" in (item.quality_flags or ()))
    return flag_count / len(evidence)


# ---------------------------------------------------------------------------
# QualityGate
# ---------------------------------------------------------------------------


class QualityGate:
    """针对 :class:`QualityPolicy` 评测一个已解析/已清洗的文档。"""

    def __init__(self, policy: QualityPolicy | None = None) -> None:
        self.policy = policy or QualityPolicy()

    def evaluate(
        self,
        document: CleanedDocument,
        evidence: list[EvidenceUnitDraft],
        facts: list[MedicalFactDraft],
    ) -> QualityDecision:
        policy = self.policy

        # --- 完整度量字典，为可观测性原样返回 ---
        metrics: dict[str, float] = {
            "text_coverage": _text_coverage(document, evidence),
            "non_empty_block_ratio": _non_empty_block_ratio(document),
            "anchor_coverage": _anchor_coverage(document, evidence),
            "ocr_confidence": _ocr_confidence(document, evidence),
            "confidence_unavailable_ratio": _confidence_unavailable_ratio(document, evidence),
            "page_coverage": _page_coverage(document, evidence),
            "abnormal_character_ratio": _abnormal_character_ratio(document),
            "duplicate_block_ratio": _duplicate_block_ratio(document),
            "table_column_consistency": _table_column_consistency(document),
            "fact_coverage": _fact_coverage(evidence, facts),
            "hard_split_ratio": _hard_split_ratio(evidence),
        }

        reasons: list[str] = []

        # --- 硬性结构失败：无可用内容（稳定原因）---
        has_content = bool(_non_empty_blocks(document)) or any(
            (item.text or "").strip() for item in evidence
        )
        if not has_content:
            reasons.append(REASON_EMPTY_DOCUMENT)
            return QualityDecision.fail(reasons, metrics)

        # --- 软性复核信号（NEEDS_REVIEW）---
        if metrics["anchor_coverage"] < policy.min_anchor_coverage:
            reasons.append(REASON_MISSING_SOURCE_ANCHOR)
        if metrics["text_coverage"] < policy.min_text_coverage:
            reasons.append(REASON_LOW_TEXT_COVERAGE)
        if metrics["non_empty_block_ratio"] < policy.min_non_empty_block_ratio:
            reasons.append(REASON_LOW_NON_EMPTY_BLOCK_RATIO)
        # 不可判定 ≠ 低置信度：来源没给置信度时报 confidence_unavailable，
        # 并用占位 0.0 去报 low_ocr_confidence 会把"没测出来"说成"测出来很低"。
        if metrics["confidence_unavailable_ratio"] > 0.0:
            reasons.append(REASON_CONFIDENCE_UNAVAILABLE)
        elif metrics["ocr_confidence"] < policy.min_ocr_confidence:
            reasons.append(REASON_LOW_OCR_CONFIDENCE)
        if metrics["abnormal_character_ratio"] > policy.max_abnormal_character_ratio:
            reasons.append(REASON_ABNORMAL_CHARACTERS)
        if metrics["duplicate_block_ratio"] > policy.max_duplicate_block_ratio:
            reasons.append(REASON_DUPLICATE_BLOCKS)
        if metrics["table_column_consistency"] < policy.min_table_column_consistency:
            reasons.append(REASON_TABLE_COLUMN_INCONSISTENT)
        if metrics["fact_coverage"] < policy.min_fact_coverage:
            reasons.append(REASON_LOW_FACT_COVERAGE)
        if metrics["hard_split_ratio"] > policy.max_hard_split_ratio:
            reasons.append(REASON_HARD_SPLIT)

        # --- 硬性结构失败：声明的页缺失（内容已丢失）---
        # 这是"内容确实缺失"而非阈值擦边，因此与 empty_document 一样归 FAIL；
        # 已算出的软信号一并返回，便于运营出口看到完整画像。
        if metrics["page_coverage"] < policy.min_page_coverage:
            reasons.append(REASON_MISSING_PAGES)
            return QualityDecision.fail(reasons, metrics)

        if reasons:
            return QualityDecision(
                status=QualityStatus.NEEDS_REVIEW, reasons=reasons, metrics=metrics, retrieval_eligible=False
            )
        return QualityDecision(status=QualityStatus.PASS, reasons=reasons, metrics=metrics)


__all__ = ["QualityGate", "QualityPolicy", "QualityStatus"]
