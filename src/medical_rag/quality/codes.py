"""单一的错误码 → 失败原因映射处（Task 7，读取时）。

所有文档级质量状态推导与错误码规范化都汇聚于此，因此扩展只有一个地方。
API 层不做临时的字符串解析。
"""

from __future__ import annotations

from enum import StrEnum


# ---------------------------------------------------------------------------
# 规范的失败原因 slug（稳定，用作 ``failure_reason_code``）
# ---------------------------------------------------------------------------
# 取值与 ``gate.py`` 中 QualityGate 的 ``REASON_*`` 常量一致，
# 使 ``quality_fail:<value>`` 能提取出完全相同的 slug，
# 无需任何转换步骤。


class QualityFailureCode(StrEnum):
    """documents API 暴露的规范稳定失败原因 slug。

    每个成员值就是 API 作为 ``failure_reason_code`` 暴露的那个确切字符串。
    """

    # 门禁失败 slug（来自 QualityGate.reasons）
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

    # 解析级错误 slug
    MIXED_PDF_OCR_DEFERRED = "mixed_pdf_ocr_deferred"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    STALE_JOB_RECOVERED = "stale_job_recovered"
    WORKER_PUBLISH_FAILED = "worker_publish_failed"
    PARSE_ERROR = "parse_error"  # 通用解析器异常
    UNKNOWN = "unknown"  # 无法识别的持久化 error_code


# ---------------------------------------------------------------------------
# Human-readable labels (Chinese, 设计 §9 运营出口)
# ---------------------------------------------------------------------------
# 每个可识别的 slug 都有一个标签，因此前端永远不必硬编码
# 标签。未定义的 slug 回落到 UNKNOWN 标签。

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

# 并非异常名的已知字面量 error_code 取值。
_KNOWN_LITERALS: frozenset[str] = frozenset({
    QualityFailureCode.MANUAL_REVIEW_REQUIRED,
    QualityFailureCode.STALE_JOB_RECOVERED,
    QualityFailureCode.WORKER_PUBLISH_FAILED,
})


# ---------------------------------------------------------------------------
# 公开映射函数
# ---------------------------------------------------------------------------


def normalize_error_code(persisted: str | None) -> str | None:
    """把持久化的 ``ParseJob.error_code`` 映射为规范的失败原因 slug。

    当 ``persisted`` 为 ``None`` 或空时返回 ``None``（READY 情形）。处理：
    * ``quality_fail:<slug>`` —— 提取并原样返回。
    * ``MixedPdfOcrDeferredError`` —— 稳定的基于异常的错误码 → ``mixed_pdf_ocr_deferred``。
    * 已知字面量（``manual_review_required``，…）—— 原样返回。
    * 通用异常名 —— 归类为 ``parse_error``。
    * 无法识别的取值 —— 回落 ``unknown``。
    """
    if not persisted:
        return None

    if persisted.startswith(_QUALITY_FAIL_PREFIX):
        suffix = persisted[len(_QUALITY_FAIL_PREFIX) :]
        # 用规范 slug 校验提取出的后缀。StrEnum 的
        # 成员判断基于 ``.value``，因此空/未知后缀会
        # 回落到 PARSE_ERROR，而不会泄漏一个非规范的原因码。
        if suffix in QualityFailureCode:
            return suffix
        return QualityFailureCode.PARSE_ERROR

    # 先检查已知字面量。
    if persisted in _KNOWN_LITERALS:
        return persisted

    # 来自解析服务的稳定、基于异常的错误码。
    if persisted == "MixedPdfOcrDeferredError":
        return QualityFailureCode.MIXED_PDF_OCR_DEFERRED

    # 其余一切（异常类型名，例如 "ValueError"、"KeyError"，
    # 或任何其他非预期错误码）→ 通用 PARSE_ERROR。
    return QualityFailureCode.PARSE_ERROR


def failure_reason_label(slug: str | None) -> str | None:
    """返回规范失败原因 slug 的人类可读标签。"""
    if slug is None:
        return None
    return _QUALITY_FAILURE_LABELS.get(slug, _QUALITY_FAILURE_LABELS[QualityFailureCode.UNKNOWN])


__all__ = [
    "QualityFailureCode",
    "failure_reason_label",
    "normalize_error_code",
]
