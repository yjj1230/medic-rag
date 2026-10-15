from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from medical_rag.api.dependencies import get_chat_orchestrator
from medical_rag.api.dependencies import current_access_context
from medical_rag.auth.policy import AccessContext
from medical_rag.chat.contracts import AnswerResponse


router = APIRouter(prefix="/v1/chat", tags=["chat"])


class ChatAnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    document_ids: list[UUID] = Field(default_factory=list, max_length=20)
    session_id: UUID | None = None
    # 重试键。编排器一直支持幂等持久化，
    # 但请求模型没有字段承载这个键，因此 HTTP 调用方
    # 用不上它，客户端重试会把这段对话存两遍（并付两次
    # 模型调用费用）。重发同一个键的客户端
    # 会拿回原来的会话，而不是多出一轮。
    client_request_id: UUID | None = None


class ChatCitationResponse(BaseModel):
    source_id: str
    title: str
    version: str
    location: str | None
    url: str | None
    chunk_id: str | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    evidence_unit_id: str | None = None
    locator: dict[str, object] | None = None


class ChatAnswerResponse(BaseModel):
    status: str
    sections: list[dict[str, str]]
    citations: list[ChatCitationResponse]
    safety_notice: str | None = None
    trace_id: str | None = None
    session_id: UUID | None = None
    # 检索诊断信息（门禁策略、有多少候选通过它、各阶段
    # 耗时、索引版本、向量化是否降级）。没有这些，HTTP
    # 接口无法区分集群故障与"没找到证据"，
    # 服务上报的门禁行为对运维也是不可见的。
    diagnostics: dict[str, object] = Field(default_factory=dict)


@router.post("/answers", response_model=ChatAnswerResponse)
def answer_chat(
    payload: ChatAnswerRequest,
    context: AccessContext = Depends(current_access_context),
    orchestrator=Depends(get_chat_orchestrator),
) -> ChatAnswerResponse:
    response: AnswerResponse = orchestrator.answer(
        context,
        payload.question,
        document_ids=payload.document_ids,
        session_id=payload.session_id,
        client_request_id=payload.client_request_id,
    )
    return ChatAnswerResponse(
        status=response.status,
        sections=[{"title": section.title, "text": section.text} for section in response.sections],
        citations=[ChatCitationResponse(**citation.to_dict()) for citation in response.citations],
        safety_notice=response.safety_notice,
        trace_id=response.trace_id,
        session_id=response.session_id,
        diagnostics=dict(response.diagnostics),
    )
