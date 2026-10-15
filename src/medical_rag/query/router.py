from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from medical_rag.safety.service import SafetyAction, SafetyService


class QueryRoute(StrEnum):
    SAFE_BLOCK = "safe_block"
    SINGLE_QUERY = "single_query"
    MULTI_QUERY = "multi_query"
    MULTI_HOP = "multi_hop"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: QueryRoute
    confidence: float
    signals: tuple[str, ...] = ()


_NUMBERED_ITEM = re.compile(r"(?:^|\s)\d+[.)、]")
_QUESTION_MARKS = "？?"
_MULTI_CONNECTORS = ("另外", "同时", "还有", "以及", "再问")
_DEPENDENCY_PHRASES = (
    "根据这个结果",
    "根据上述结果",
    "根据报告结果",
    "需要复查吗",
    "接下来怎么办",
    "然后需要",
)
_SHARED_RANGE_PHRASES = ("正常范围", "参考范围", "正常值")


class QueryRouter:
    """Route a user question before optional multi-question decomposition."""

    def __init__(self, safety_service: SafetyService | None = None) -> None:
        self._safety = safety_service or SafetyService()

    def route(self, question: str) -> RouteDecision:
        text = question.strip()
        if not text:
            raise ValueError("问题不能为空")

        safety = self._safety.classify(text)
        if safety.action is not SafetyAction.ALLOW_RETRIEVAL:
            return RouteDecision(
                route=QueryRoute.SAFE_BLOCK,
                confidence=1.0,
                signals=(f"safety:{safety.risk_class.value}",),
            )

        normalized = text.casefold()
        question_count = sum(normalized.count(mark) for mark in _QUESTION_MARKS)
        numbered = bool(_NUMBERED_ITEM.search(normalized))
        dependency = any(phrase in normalized for phrase in _DEPENDENCY_PHRASES)
        connector = any(phrase in normalized for phrase in _MULTI_CONNECTORS)
        shared_range = any(phrase in normalized for phrase in _SHARED_RANGE_PHRASES)

        if dependency and question_count >= 2:
            return RouteDecision(
                route=QueryRoute.MULTI_HOP,
                confidence=0.92,
                signals=("dependency_phrase", "multiple_questions"),
            )

        if (numbered or question_count >= 2 or connector) and not shared_range:
            signals: list[str] = []
            if numbered:
                signals.append("numbered_items")
            if question_count >= 2:
                signals.append("multiple_questions")
            if connector:
                signals.append("parallel_connector")
            return RouteDecision(
                route=QueryRoute.MULTI_QUERY,
                confidence=0.9,
                signals=tuple(signals),
            )

        signals = ("shared_context",) if shared_range else ()
        return RouteDecision(
            route=QueryRoute.SINGLE_QUERY,
            confidence=0.85 if shared_range else 0.8,
            signals=signals,
        )
