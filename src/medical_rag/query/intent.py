from __future__ import annotations

import logging

from medical_rag.query.contracts import Intent, QueryContext
from medical_rag.query.intent_rules import PRIORITY, score_intent

logger = logging.getLogger(__name__)

# 无任何规则命中时的默认置信度（与 Intent.ORDINARY 一起使用）。
_NO_SIGNAL_CONFIDENCE = 0.5
# LLM 分类结果的置信度：模型给出的标签无法再细分把握程度，统一记 0.9。
_LLM_CONFIDENCE = 0.9
# 低于该值即认为「规则证据不足」，交给 LLM 兜底。
#
# 取值必须能被真实场景触达：原先的公式 `0.5 + 0.2*priority + 0.05*count` 对任何
# 命中的意图都 >= 1.0（REPORT 30 → min(1.0, 6.55)），于是这个分支永远不进、
# LLM 兜底等于死代码，置信度也永远等于 1.0、不携带任何信息。
_MIN_CONFIDENCE_FALLBACK = 0.6
# 信号强度因子：0.5 + priority/40。REPORT(30)/LAB(20) 取满 1.0，
# EDUCATION(10) 为 0.75 —— 优先级只影响「信号有多强」，竞争程度影响「有多确定」。
_PRIORITY_FACTOR_BASE = 0.5
_PRIORITY_FACTOR_SPAN = 40.0


def _confidence(ranked: list[tuple[Intent, int]]) -> float:
    """规则分类的置信度 = 竞争份额 × 信号强度，取值 (0, 1]。

    * 竞争份额：胜出意图的命中数占全部命中的比例。多个意图同时命中说明这句话
      可以被读成多种意图，置信度必须随之下降（而不是继续报 1.0）。
    * 信号强度：胜出意图的语义优先级。命中「报告解释」比命中「科普措辞」更说明
      问题（后者常常只是顺带出现的疑问词）。
    """

    top_intent, top_count = ranked[0]
    total = sum(count for _, count in ranked) or 1
    share = top_count / total
    factor = min(
        1.0,
        _PRIORITY_FACTOR_BASE + PRIORITY.get(top_intent, 0) / _PRIORITY_FACTOR_SPAN,
    )
    return round(share * factor, 4)


class IntentRouter:
    """Rule-first intent classifier with optional LLM fallback.

    The LLM fallback is wired by the caller (orchestrator). When disabled or
    unavailable, the router returns the best rule match or ORDINARY.
    """

    def __init__(self, llm_classifier=None, *, enable_llm: bool = False) -> None:
        self._llm = llm_classifier
        self._enable_llm = enable_llm

    def classify(self, text: str) -> tuple[Intent, float]:
        """规则优先分类。无 AccessContext 时不做 LLM 兜底（保持无状态、可测试）。

        生产路径请使用 :meth:`process`，后者会带上 QueryContext.access_context 触发
        LLM 兜底。
        """
        ranked = score_intent(text)
        if not ranked:
            return Intent.ORDINARY, _NO_SIGNAL_CONFIDENCE
        return ranked[0][0], _confidence(ranked)

    def process(self, ctx: QueryContext) -> QueryContext:
        """QueryStep 协议实现：对当前文本分类并写回 intent/置信度。

        两种情况下走 LLM 兜底：规则完全没命中，或规则命中了但置信度低（多个意图
        竞争）。规则给出高置信结论时绝不调用 LLM——延迟与成本都留给真正不确定的
        句子。
        """
        ranked = score_intent(ctx.current)
        if not ranked:
            llm_result = self._classify_via_llm(ctx.current, ctx.access_context)
            if llm_result is None:
                ctx.intent, ctx.intent_confidence = (
                    Intent.ORDINARY,
                    _NO_SIGNAL_CONFIDENCE,
                )
            else:
                ctx.intent, ctx.intent_confidence = llm_result
                ctx.used_llm = True
            return ctx

        top_intent, _ = ranked[0]
        confidence = _confidence(ranked)
        if confidence < _MIN_CONFIDENCE_FALLBACK:
            llm_result = self._classify_via_llm(ctx.current, ctx.access_context)
            if llm_result is not None:
                ctx.intent, ctx.intent_confidence = llm_result
                # 用了 LLM 必须留下痕迹：回答侧要能解释意图是怎么定的。
                ctx.used_llm = True
                return ctx
            # LLM 不可用：保留规则结论，绝不因为兜底失败把已识别的医学意图
            # 降级成 ORDINARY（那会让报告解释类问题走错检索策略）。
        ctx.intent, ctx.intent_confidence = top_intent, confidence
        return ctx

    def _should_use_llm(self, text: str, context) -> bool:
        del text
        return self._enable_llm and self._llm is not None and context is not None

    def _classify_via_llm(
        self, text: str, context
    ) -> tuple[Intent, float] | None:
        """调用 LLM 分类器；不可用/返回非法标签时返回 None（由调用方决定降级）。"""

        if not self._should_use_llm(text, context):
            return None
        try:
            label = self._llm.classify_intent(text, context=context)
            return Intent(label), _LLM_CONFIDENCE
        except Exception:
            logger.warning("LLM 意图兜底失败，回退到规则结果", exc_info=True)
            return None
