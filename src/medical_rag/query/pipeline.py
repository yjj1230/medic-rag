from __future__ import annotations

from medical_rag.query.context_completion import ContextCompletionStep
from medical_rag.query.contracts import (
    Intent,
    ProcessedQuery,
    QueryContext,
    QueryStep,
    SearchStrategy,
)
from medical_rag.query.intent import IntentRouter
from medical_rag.query.normalize import NormalizeStep


def derive_strategy(intent: Intent, *, has_private: bool = True) -> SearchStrategy:
    """Map an intent to retrieval flags. has_private == True means the patient
    has stored private report data available to retrieve.

    ``REPORT_EXPLANATION`` 的语义是「私有报告优先」，但**不是**「只能查私有」：
    参考范围/正常值这类关键依据常常只在公共指南里。私有结果不足时由编排器
    补一次公共检索（见 :func:`needs_public_backfill`），因此这里的策略仍然是
    私有优先，而不是把公共库永久排除。
    """
    if intent == Intent.REPORT_EXPLANATION:
        if has_private:
            return SearchStrategy(include_public=False, include_private=True)
        return SearchStrategy(include_public=True, include_private=False)
    if intent == Intent.HEALTH_EDUCATION:
        return SearchStrategy(include_public=True, include_private=False)
    # LAB_INDICATOR 与 ORDINARY 走混合
    return SearchStrategy(include_public=True, include_private=True)


# 私有优先策略下，私有命中少于该条数就补一次公共检索。取 3 是因为单条私有
# 证据不足以支撑「解释报告」所需的上下文（数值 + 参考范围 + 说明）。
PUBLIC_BACKFILL_MIN_PRIVATE_HITS = 3


def needs_public_backfill(
    strategy: SearchStrategy,
    private_hits: int,
    *,
    minimum: int = PUBLIC_BACKFILL_MIN_PRIVATE_HITS,
) -> bool:
    """私有优先策略下是否需要补一次公共检索。"""

    if strategy.include_public:
        # 策略本身已包含公共库，无需重复检索。
        return False
    return private_hits < minimum


class QueryPipeline:
    def __init__(
        self,
        steps: list[QueryStep] | None = None,
        *,
        history_size: int = 4,
    ) -> None:
        self.steps = steps or [
            NormalizeStep(),
            IntentRouter(),
            ContextCompletionStep(history_size=history_size),
        ]

    def process(
        self,
        original: str,
        history: tuple = (),
        *,
        has_private: bool = True,
        access_context=None,
    ) -> ProcessedQuery:
        ctx = QueryContext(
            original=original,
            current=original,
            history=tuple(history),
            access_context=access_context,
        )
        for step in self.steps:
            ctx = step.process(ctx)
        strategy = derive_strategy(ctx.intent, has_private=has_private)
        return ProcessedQuery(
            text=ctx.current,
            original=ctx.original,
            intent=ctx.intent,
            search_strategy=strategy,
            used_llm=ctx.used_llm,
            rewrite_log=tuple(ctx.rewrite_log),
        )
