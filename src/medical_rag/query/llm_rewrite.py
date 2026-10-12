from __future__ import annotations

import logging

from medical_rag.query.contracts import QueryContext

logger = logging.getLogger(__name__)

_TRIGGER = ("needs_llm", "referen")
_REWRITE_SYSTEM = (
    "你是一个检索改写助手。只把用户的问题改写得更适合检索，不回答、不补充医学事实、"
    "不增删内容。若已正常则原样输出。只输出改写后的一句话。"
)


class LLMRewriteStep:
    """Optional LLM fallback that rewrites the query for retrieval.

    Enabled by config; routes through ModelGateway so consent/audit still apply.
    Any failure degrades gracefully (no rewrite, no error propagated).
    """

    def __init__(self, gateway, *, enabled: bool = False, model: str = "gpt-5.5") -> None:
        self._gateway = gateway
        self._enabled = enabled
        self._model = model

    def _should_rewrite(self, ctx: QueryContext) -> bool:
        if not self._enabled or self._gateway is None or ctx.access_context is None:
            return False
        # 仅当 context completion 标记了 needs_llm 或存在未解指代时改写
        joined = " ".join(ctx.rewrite_log)
        return any(token in joined for token in _TRIGGER)

    def process(self, ctx: QueryContext) -> QueryContext:
        if not self._should_rewrite(ctx):
            return ctx
        try:
            from medical_rag.models.contracts import ChatRequest

            request = ChatRequest(
                prompt=ctx.current,
                system_prompt=_REWRITE_SYSTEM,
                model=self._model,
                purpose="query_rewrite",
            )
            response = self._gateway.chat(ctx.access_context, request)
            text = response.text.strip()
            if text:
                ctx.rewrite_log.append(f"llm_rewrite: -> {text}")
                ctx.current = text
                ctx.used_llm = True
        except Exception:
            # Graceful degradation: a failed rewrite must not block the query.
            # Logged rather than silently swallowed so a broken gateway is
            # visible instead of looking like "the rewrite was not needed".
            logger.warning("LLM 查询改写失败，保留规则改写结果", exc_info=True)
        return ctx
