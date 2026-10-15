"""质量门禁契约：QUALITY FAIL 的写入策略与判定值对象。

已确定的 FAIL 写入策略（write-but-flag）：
  一个 NEEDS_REVIEW 判定绝不丢弃证据行。解析事务仍会持久化每一条规范化的
  EvidenceUnit 与 MedicalFact（``retrieval_eligible=False`` 并带质量标志），
  索引 worker 可以把这些记录写入私有索引并标记 ``retrieval_eligible``
  为 False。所有检索/搜索路径都会过滤不合格记录，因此任何被标记的内容
  绝不会作为可用证据被展示。

  当前的 FAIL 只在文档/证据**完全为空**（根本没有任何内容可保留）时返回，
  因此该路径不持久化任何证据行或事实。若未来的门禁在保留证据的同时返回 FAIL，
  下方契约（write-but-flag，绝不删除）将与 NEEDS_REVIEW 同样适用：行以
  ``retrieval_eligible=False`` 持久化，索引时标记为不可检索，只有
  SEARCHABLE / 可检索的 IndexJob 被抑制。FAIL 绝不删除证据行。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from medical_rag.domain.enums import QualityStatus


@dataclass(frozen=True, slots=True)
class QualityDecision:
    """质量门禁判定：status ∈ PASS | NEEDS_REVIEW | FAIL。

    NEEDS_REVIEW / FAIL 内容仍会被持久化（write-but-flag），但绝不作为检索证据
    暴露。status 是运行期校验的 :class:`QualityStatus` StrEnum。
    """

    status: Literal["PASS", "NEEDS_REVIEW", "FAIL"] | QualityStatus
    reasons: tuple[str, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    retrieval_eligible: bool = True

    def __post_init__(self) -> None:
        # 运行期校验 status（fail-closed）：强制转换为该 StrEnum，使
        # 拼错的值在此抛错，而不是静默构造。
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
        """标记为需人工复核：内容保留但不可检索。"""

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
        """以稳定原因标记为硬失败；内容保留但不可检索。"""

        return cls(
            status=QualityStatus.FAIL,
            reasons=reasons,
            metrics=metrics or {},
            retrieval_eligible=False,
        )


__all__ = ["QualityDecision"]
