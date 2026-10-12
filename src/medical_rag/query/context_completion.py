from __future__ import annotations

import re

from medical_rag.domain.models import ChatMessage
from medical_rag.query.contracts import QueryContext


_REFERENT = re.compile(
    r"它|他|她|那个指标|那个结果|上面|上次|这个指标|这药|这些数|这个数"
)
# 中文医疗报告里常见的可作指代锚点的实体关键词
_ANCHOR_ENTITY = re.compile(
    r"血红蛋白|白细胞|红细胞|血小板|血糖|血脂|胆固醇|血压|肝功|肾功|尿酸|ALT|AST|Hb|WBC|RBC"
)


def _extract_anchor_entities(messages: tuple[ChatMessage, ...]) -> list[str]:
    entities: list[str] = []
    seen: set[str] = set()
    for msg in reversed(messages):
        for match in _ANCHOR_ENTITY.finditer(msg.content or ""):
            term = match.group(0)
            if term not in seen:
                seen.add(term)
                entities.append(term)
    return entities


class ContextCompletionStep:
    """Resolve referents ('it', 'that result') using recent chat history."""

    def __init__(self, history_size: int = 4) -> None:
        self.history_size = history_size

    def process(self, ctx: QueryContext) -> QueryContext:
        if not ctx.history:
            return ctx
        if not _REFERENT.search(ctx.current):
            return ctx

        recent = ctx.history[-self.history_size :]
        anchors = _extract_anchor_entities(tuple(recent))
        if not anchors:
            ctx.rewrite_log.append(
                "completion: referent detected but no anchor entity in history (needs_llm)"
            )
            return ctx

        anchor = anchors[0]
        if anchor not in ctx.current:
            completed = f"{ctx.current} {anchor}"
            ctx.rewrite_log.append(
                f"completion: '{ctx.current.strip()}' -> '{completed.strip()}'"
            )
            ctx.current = completed
        return ctx
