"""读取时派生的文档质量状态（Task 7）。

documents 列表端点使用的纯函数式、无副作用推导。规则有意显式化：

* ``FAILED_PARSE`` —— 最新的稳定 ParseJob 失败，或该文档没有可用
  （活动、未删除）版本。
* ``NEEDS_REVIEW`` —— 任一活动版本为 ``DocumentStatus.NEEDS_REVIEW``。
* ``READY`` —— 其他情况。

``failure_reason_code`` / ``failure_reason_label`` 经 :mod:`medical_rag.quality.codes`
中的唯一映射处，从最近一次稳定 ParseJob 持久化的 ``error_code`` 推导而来。
"""

from __future__ import annotations

from dataclasses import dataclass

from medical_rag.domain.enums import DocumentStatus, ParseJobStatus
from medical_rag.domain.models import Document, DocumentVersion, ParseJob
from medical_rag.quality.codes import failure_reason_label, normalize_error_code

# 作为 ``quality_status`` 暴露的实际取值。
FAILED_PARSE = "FAILED_PARSE"
NEEDS_REVIEW = "NEEDS_REVIEW"
READY = "READY"

# 代表已定局、稳定 ParseJob 的状态（其 error_code
# 我们信任用于失败原因的那些）。
#
# 设计说明（选项 A）：卡在 PENDING/RUNNING 且带
# ``worker_publish_failed``（或任何非终态）error_code 的 ParseJob 有意*不*
# 属于本集合，因此它不会在试点中出现在质量列里。
# 该任务把发布失败排除在质量判定之外；暴露它们
# 超出 Task 7 的范围。
_FINAL_PARSE_STATUSES = frozenset(
    {
        ParseJobStatus.SUCCEEDED,
        ParseJobStatus.FAILED,
        ParseJobStatus.NEEDS_REVIEW,
    }
)


@dataclass(frozen=True, slots=True)
class DerivedQuality:
    """派生出的读取时文档质量信号（增量式，绝不存储）。"""

    quality_status: str  # READY | NEEDS_REVIEW | FAILED_PARSE
    failure_reason_code: str | None
    failure_reason_label: str | None


def _latest_parse_job(
    jobs: list[ParseJob],
) -> ParseJob | None:
    """返回最近的稳定 ParseJob（最新的已完成/已创建优先）。"""
    stable = [j for j in jobs if j.status in _FINAL_PARSE_STATUSES]
    if not stable:
        return None
    return max(stable, key=lambda j: (j.finished_at or j.created_at, j.created_at))


def _active_versions(versions: list[DocumentVersion]) -> list[DocumentVersion]:
    return [v for v in versions if v.status != DocumentStatus.DELETED]


def derive_document_quality(
    document: Document,
    versions: list[DocumentVersion],
    parse_jobs: list[ParseJob],
) -> DerivedQuality:
    """为单个文档推导读取时的质量状态 + 原因。"""
    del document  # 保留在签名中以便调用清晰 / 供未来信号使用
    active = _active_versions(versions)
    latest_job = _latest_parse_job(parse_jobs)

    # FAILED_PARSE：没有可用版本，或最近一次稳定解析失败。
    if not active or (latest_job is not None and latest_job.status == ParseJobStatus.FAILED):
        return _with_reason(FAILED_PARSE, latest_job)

    # NEEDS_REVIEW：任一活动版本等待复核。
    if any(v.status == DocumentStatus.NEEDS_REVIEW for v in active):
        return _with_reason(NEEDS_REVIEW, latest_job)

    # READY。
    return DerivedQuality(READY, None, None)


def _with_reason(status: str, job: ParseJob | None) -> DerivedQuality:
    slug = normalize_error_code(job.error_code if job else None)
    return DerivedQuality(status, slug, failure_reason_label(slug))


__all__ = [
    "FAILED_PARSE",
    "NEEDS_REVIEW",
    "READY",
    "DerivedQuality",
    "derive_document_quality",
]
