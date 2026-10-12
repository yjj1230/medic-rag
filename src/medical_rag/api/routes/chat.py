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
    # Retry key.  The orchestrator has always supported idempotent persistence,
    # but the request model had no field to carry the key, so HTTP callers could
    # not use it and a client retry stored the exchange twice (and paid for the
    # model call twice).  Clients that resend the same key get the original
    # session back instead of a duplicate turn.
    client_request_id: UUID | None = None


class ChatCitationResponse(BaseModel):
    source_id: str
    title: str
    version: str
    location: str | None
    url: str | None


class ChatAnswerResponse(BaseModel):
    status: str
    sections: list[dict[str, str]]
    citations: list[ChatCitationResponse]
    safety_notice: str | None = None
    trace_id: str | None = None
    session_id: UUID | None = None
    # Retrieval diagnostics (gate policy and how many candidates passed it, stage
    # timings, index version, whether embedding degraded).  Without this the HTTP
    # surface could not distinguish a cluster outage from "no evidence found",
    # and the gate behaviour the service reports was invisible to operators.
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
