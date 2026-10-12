from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from medical_rag.domain.models import ChatMessage

if TYPE_CHECKING:
    from medical_rag.auth.policy import AccessContext


class Intent(StrEnum):
    REPORT_EXPLANATION = "report_explanation"
    HEALTH_EDUCATION = "health_education"
    LAB_INDICATOR = "lab_indicator"
    ORDINARY = "ordinary"


@dataclass(frozen=True, slots=True)
class SearchStrategy:
    include_public: bool
    include_private: bool


@dataclass(frozen=True, slots=True)
class ProcessedQuery:
    text: str
    original: str
    intent: Intent
    search_strategy: SearchStrategy
    used_llm: bool = False
    rewrite_log: tuple[str, ...] = ()


@dataclass
class QueryContext:
    original: str
    current: str
    history: tuple[ChatMessage, ...] = ()
    intent: Intent = Intent.ORDINARY
    intent_confidence: float = 0.0
    used_llm: bool = False
    rewrite_log: list[str] = field(default_factory=list)
    # 真实调用方身份，供需要走 ModelGateway 的步骤（如 LLM 改写）做 consent/audit。
    access_context: AccessContext | None = None


class QueryStep(Protocol):
    def process(self, ctx: QueryContext) -> QueryContext: ...
