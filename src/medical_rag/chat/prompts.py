from __future__ import annotations

import hashlib
from uuid import uuid4

from medical_rag.models.contracts import ChatRequest
from medical_rag.retrieval.contracts import Evidence


SYSTEM_PROMPT = """你是患者健康教育助手。你只能根据提供的证据回答。
区分患者报告中的客观事实与公共医学知识；不作诊断，不开处方，不建议改变药物或剂量。
使用不确定性表述，不能从证据推断未提供的事实。每个医学结论必须在句末使用对应证据 ID，格式如 [evidence-id]。
每个自然段的末尾都必须附上对应的证据 ID（包括结尾的就医建议段），段落中不出现报告数据可不引用。
如果证据不足，明确说明无法可靠回答，并建议咨询医生。"""


def build_chat_request(
    question: str,
    evidence: list[Evidence],
    *,
    model: str,
    policy_version: str = "v1",
    trace_id: str | None = None,
) -> ChatRequest:
    """构建外发模型的合规请求。

    ``model`` 必须由调用方显式注入（组合根传 ``settings.model_name``）：
    出站载荷与合规白名单都以它为准，硬编码会让任何非默认模型永远无法通过
    ``ModelGateway`` 的模型白名单校验。
    """
    return ChatRequest(
        prompt=question,
        system_prompt=SYSTEM_PROMPT,
        evidence=evidence,
        model=model,
        policy_version=policy_version,
        trace_id=trace_id or str(uuid4()),
        purpose="health_education",
        evidence_hashes=[hashlib.sha256(item.text.encode("utf-8")).hexdigest() for item in evidence],
    )
