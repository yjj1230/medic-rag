"""Quality gate contracts: QUALITY FAIL write policy and decision value object.

DECIDED FAIL WRITE POLICY (write-but-flag):
  A NEEDS_REVIEW decision NEVER discards evidence rows.  The parse
  transaction still persists every normalized EvidenceUnit and MedicalFact
  (with ``retrieval_eligible=False`` and quality flags), and the index worker
  may write those records to the private index marked ``retrieval_eligible``
  False.  Every retrieval/search path filters ineligible records, so nothing
  flagged is ever surfaced as usable evidence.

  FAIL today returns only when the document/evidence is **truly empty** (no
  content to retain at all), so no evidence rows or facts are persisted in
  that path.  If a future gate returns FAIL while retaining evidence, the
  contract below (write-but-flag, never delete) applies the same as
  NEEDS_REVIEW: rows are persisted ``retrieval_eligible=False``, indexed
  flagged non-retrievable, and only a SEARCHABLE / retrievable IndexJob is
  suppressed.  FAIL never deletes evidence rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from medical_rag.domain.enums import QualityStatus


@dataclass(frozen=True, slots=True)
class QualityDecision:
    """Quality gate judgement: status ∈ PASS | NEEDS_REVIEW | FAIL.

    NEEDS_REVIEW / FAIL content is still persisted (write-but-flag), but is
    never exposed as retrieval evidence.  Status is a runtime-validated
    :class:`QualityStatus` StrEnum.
    """

    status: Literal["PASS", "NEEDS_REVIEW", "FAIL"] | QualityStatus
    reasons: tuple[str, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    retrieval_eligible: bool = True

    def __post_init__(self) -> None:
        # Runtime-validate the status (fail-closed): coerce to the StrEnum so
        # a mistyped value raises here rather than constructing silently.
        object.__setattr__(self, "status", QualityStatus(self.status))
        # 设计 §9：PASS→可检索，NEEDS_REVIEW/FAIL→不可检索；构造时强制一致（fail-closed）。
        if self.retrieval_eligible != (self.status == QualityStatus.PASS):
            raise ValueError(
                "retrieval_eligible 必须与 status 一致：PASS→True，NEEDS_REVIEW/FAIL→False"
                f"（got status={self.status!r}, retrieval_eligible={self.retrieval_eligible!r}）"
            )
        # 唯一归一化点：reasons 固定为 tuple；metrics 拷贝为独立 dict，避免调用方别名污染。
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "metrics", dict(self.metrics))

    @classmethod
    def review(
        cls,
        reasons: Iterable[str],
        metrics: Mapping[str, float] | None = None,
    ) -> QualityDecision:
        """Mark for manual review: content is retained but not retrievable."""

        return cls(
            status=QualityStatus.NEEDS_REVIEW,
            reasons=reasons,
            metrics=metrics or {},
            retrieval_eligible=False,
        )

    @classmethod
    def fail(
        cls,
        reasons: Iterable[str],
        metrics: Mapping[str, float] | None = None,
    ) -> QualityDecision:
        """Mark as hard failure with a stable reason; content retained but not retrievable."""

        return cls(
            status=QualityStatus.FAIL,
            reasons=reasons,
            metrics=metrics or {},
            retrieval_eligible=False,
        )


__all__ = ["QualityDecision"]
