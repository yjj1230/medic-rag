from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from medical_rag.knowledge.contracts import Citation


@dataclass(frozen=True, slots=True)
class DraftAnswer:
    text: str
    cited_evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ValidatedAnswer:
    text: str
    citations: list[Citation]
    cited_evidence: list[str]


@dataclass(frozen=True, slots=True)
class AnswerSection:
    title: str
    text: str


@dataclass(frozen=True, slots=True)
class AnswerResponse:
    status: str
    sections: list[AnswerSection]
    citations: list[Citation]
    safety_notice: str | None = None
    trace_id: str | None = None
    session_id: UUID | None = None
    # 检索链路的关键诊断（门禁策略/通过条数、各阶段耗时、索引版本、
    # embedding 是否降级、是否补检索）。故障必须可观测：否则集群宕机与
    # 「医学证据不足」在响应里完全同形，监控与患者都会被误导。
    # 只放白名单键，绝不包含候选文档 id 之类的内部标识。
    diagnostics: dict[str, object] = field(default_factory=dict)
