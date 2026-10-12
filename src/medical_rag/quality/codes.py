"""Single error-code → failure-reason mapping site (Task 7, read-time).

All document-level quality-status derivation and error-code normalisation
converges here so there is exactly one place to extend.  No ad-hoc string
parsing in the API layer.
"""

from __future__ import annotations

from enum import StrEnum


# ---------------------------------------------------------------------------
# Canonical failure-reason slugs (stable, used as ``failure_reason_code``)
# ---------------------------------------------------------------------------
# The values match the QualityGate ``REASON_*`` constants in ``gate.py`` so
# that ``quality_fail:<value>`` extracts to exactly the same slug with no
# translation step.


class QualityFailureCode(StrEnum):
    """Canonical stable failure-reason slugs exposed by the documents API.

    Every member value is the exact string the API surfaces as
    ``failure_reason_code``.
    """

    # Gate-failure slugs (from QualityGate.reasons)
    EMPTY_DOCUMENT = "empty_document"
    MISSING_SOURCE_ANCHOR = "missing_source_anchor"
    LOW_TEXT_COVERAGE = "low_text_coverage"
    LOW_NON_EMPTY_BLOCK_RATIO = "low_non_empty_block_ratio"
    LOW_OCR_CONFIDENCE = "low_ocr_confidence"
    ABNORMAL_CHARACTERS = "abnormal_characters"
    DUPLICATE_BLOCKS = "duplicate_blocks"
    TABLE_COLUMN_INCONSISTENT = "table_column_inconsistent"
    LOW_FACT_COVERAGE = "low_fact_coverage"
    HARD_SPLIT = "hard_split"
    # 结构性硬失败：声明页数与实际产出页数不符（内容确实缺失，不是阈值擦边）。
    MISSING_PAGES = "missing_pages"
    # 不可判定：来源未提供置信度（不得当作"合格"，也不得伪装成低分）。
    CONFIDENCE_UNAVAILABLE = "confidence_unavailable"

    # Parse-level error slugs
    MIXED_PDF_OCR_DEFERRED = "mixed_pdf_ocr_deferred"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    STALE_JOB_RECOVERED = "stale_job_recovered"
    WORKER_PUBLISH_FAILED = "worker_publish_failed"
    PARSE_ERROR = "parse_error"  # generic parser exception
    UNKNOWN = "unknown"  # unrecognised persisted error_code


# ---------------------------------------------------------------------------
# Human-readable labels (Chinese, 设计 §9 运营出口)
# ---------------------------------------------------------------------------
# Every recognised slug gets a label so the frontend never has to hard-code
# labels.  Undefined slugs fall back to the UNKNOWN label.

_QUALITY_FAILURE_LABELS: dict[str, str] = {
    QualityFailureCode.EMPTY_DOCUMENT: "文档内容为空",
    QualityFailureCode.MISSING_SOURCE_ANCHOR: "缺少来源锚点，需人工确认",
    QualityFailureCode.LOW_TEXT_COVERAGE: "文本覆盖率不足，需人工确认",
    QualityFailureCode.LOW_NON_EMPTY_BLOCK_RATIO: "非空块比例偏低，需人工确认",
    QualityFailureCode.LOW_OCR_CONFIDENCE: "OCR 置信度偏低，需人工确认",
    QualityFailureCode.ABNORMAL_CHARACTERS: "异常字符比例偏高，需人工确认",
    QualityFailureCode.DUPLICATE_BLOCKS: "重复块比例偏高，需人工确认",
    QualityFailureCode.TABLE_COLUMN_INCONSISTENT: "表格列数不一致，需人工确认",
    QualityFailureCode.LOW_FACT_COVERAGE: "事实提取覆盖率偏低，需人工确认",
    QualityFailureCode.HARD_SPLIT: "存在硬拆分段落，需人工确认",
    QualityFailureCode.MISSING_PAGES: "文档缺页（页数覆盖不足），需人工复核后重新解析",
    QualityFailureCode.CONFIDENCE_UNAVAILABLE: "来源未提供置信度，需人工确认",
    QualityFailureCode.MIXED_PDF_OCR_DEFERRED: "PDF 含扫描/空白页，暂无法逐页 OCR；请拆分或用页面级 OCR",
    QualityFailureCode.MANUAL_REVIEW_REQUIRED: "需人工复核",
    QualityFailureCode.STALE_JOB_RECOVERED: "任务已恢复（原任务超时）",
    QualityFailureCode.WORKER_PUBLISH_FAILED: "任务发布失败，请重试",
    QualityFailureCode.PARSE_ERROR: "解析异常，请重试",
    QualityFailureCode.UNKNOWN: "未知错误",
}

_QUALITY_FAIL_PREFIX = "quality_fail:"

# Known literal error_code values that are NOT exception names.
_KNOWN_LITERALS: frozenset[str] = frozenset({
    QualityFailureCode.MANUAL_REVIEW_REQUIRED,
    QualityFailureCode.STALE_JOB_RECOVERED,
    QualityFailureCode.WORKER_PUBLISH_FAILED,
})


# ---------------------------------------------------------------------------
# Public mapping functions
# ---------------------------------------------------------------------------


def normalize_error_code(persisted: str | None) -> str | None:
    """Map a persisted ``ParseJob.error_code`` to a canonical failure-reason slug.

    Returns ``None`` when ``persisted`` is ``None`` or empty (the READY case).
    Handles:
    * ``quality_fail:<slug>`` — extracted and returned as-is.
    * ``MixedPdfOcrDeferredError`` — stable exception-based code → ``mixed_pdf_ocr_deferred``.
    * Known literals (``manual_review_required``, …) — returned verbatim.
    * Generic exception names — classified as ``parse_error``.
    * Unrecognised values — fallback ``unknown``.
    """
    if not persisted:
        return None

    if persisted.startswith(_QUALITY_FAIL_PREFIX):
        suffix = persisted[len(_QUALITY_FAIL_PREFIX) :]
        # Validate the extracted suffix against the canonical slugs.  StrEnum
        # membership compares on `.value`, so an empty/unknown suffix falls
        # back to PARSE_ERROR rather than leaking a non-canonical reason code.
        if suffix in QualityFailureCode:
            return suffix
        return QualityFailureCode.PARSE_ERROR

    # Check known literals first.
    if persisted in _KNOWN_LITERALS:
        return persisted

    # Stable exception-based code from parsing service.
    if persisted == "MixedPdfOcrDeferredError":
        return QualityFailureCode.MIXED_PDF_OCR_DEFERRED

    # Everything else (exception type names, e.g. "ValueError", "KeyError",
    # or any other unexpected code) → generic PARSE_ERROR.
    return QualityFailureCode.PARSE_ERROR


def failure_reason_label(slug: str | None) -> str | None:
    """Return the human-readable label for a canonical failure-reason slug."""
    if slug is None:
        return None
    return _QUALITY_FAILURE_LABELS.get(slug, _QUALITY_FAILURE_LABELS[QualityFailureCode.UNKNOWN])


__all__ = [
    "QualityFailureCode",
    "failure_reason_label",
    "normalize_error_code",
]
