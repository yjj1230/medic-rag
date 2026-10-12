"""LLM 意图分类兜底 adapter。

只在该意图的规则集未命中（或置信度极低）且配置开启时被调用。保持"规则先行、
LLM 兜底"的成本结构：绝大多数查询由零成本的规则命中，LLM 只负责规则筛不出的
长尾。任何失败都优雅降级回 `ORDINARY`，不阻断检索流程。

标签与本系统 :class:`query.contracts.Intent` 枚举值一一对应：
``report_explanation`` / ``health_education`` / ``lab_indicator`` / ``ordinary``。
"""
from __future__ import annotations

from medical_rag.models.contracts import ChatRequest, ChatResponse

# 指示模型只输出一个意图标签，不要夹杂解释；标签必须命中下方白名单。
_INTENT_SYSTEM_PROMPT = (
    "你是患者问答的意图分类器。根据用户问题，只输出一个英文标签："
    "report_explanation（解释其个人报告）、health_education（一般健康科普）、"
    "lab_indicator（问化验指标的含义或正常范围）、ordinary（其他）。"
    "只输出标签本身，不含任何其他文字。"
)

# 与 Intent 枚举值同步的合法标签白名单。
KNOWN_LABELS = frozenset(
    {"report_explanation", "health_education", "lab_indicator", "ordinary"}
)


class LLMIntentClassifier:
    """通过 ModelGateway 调度的意图分类器，承接 IntentRouter 的 llm_classifier 契约。"""

    def __init__(self, gateway, *, model: str = "gpt-5.5") -> None:
        self._gateway = gateway
        self._model = model

    def classify_intent(self, text: str, *, context=None) -> str:
        """返回一个合法意图标签字符串；无法判定或无上下文时回退 ``ordinary``。

        ``context`` 由 IntentRouter.process 注入；没有它时无法走合规网关，直接回退，
        保持一般情况下判定无副作用。
        """
        if context is None:
            return "ordinary"
        request = ChatRequest(
            prompt=text,
            system_prompt=_INTENT_SYSTEM_PROMPT,
            model=self._model,
            max_output_tokens=8,
            purpose="intent_classify",
        )
        response: ChatResponse = self._gateway.chat(context, request)
        label = response.text.strip().lower()
        if label in KNOWN_LABELS:
            return label
        return "ordinary"
