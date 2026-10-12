from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.audit.service import AuditService
from medical_rag.auth.policy import AccessContext
from medical_rag.consent.service import ConsentService
from medical_rag.core.errors import AccessDenied
from medical_rag.domain.enums import AuditAction, ConsentPurpose, DocumentStatus
from medical_rag.domain.models import Document
from medical_rag.models.contracts import ChatModel, ChatRequest, ChatResponse
from medical_rag.storage.file_policy import MAX_UPLOAD_SIZE_BYTES, SUPPORTED_TYPES


class ModelGateway:
    """外发模型的合规网关。

    会话通过 ``session_factory`` 按调用获取：网关本身是长生命周期单例，
    不持有跨请求的 SQLAlchemy Session，避免并发请求共享事务状态。
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        chat_model: ChatModel,
        *,
        provider_id: str = "lan-chat",
        allowed_providers: set[str] | None = None,
        allowed_models: set[str] | None = None,
        policy_version: str = "v1",
        max_request_chars: int = 120_000,
        data_region: str | None = None,
        zero_retention: bool = True,
        consent_service: ConsentService | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.chat_model = chat_model
        self.provider_id = provider_id
        self.allowed_providers = allowed_providers or {provider_id}
        self.allowed_models = allowed_models or {"gpt-5.5"}
        self.policy_version = policy_version
        self.max_request_chars = max_request_chars
        self.data_region = data_region
        if not zero_retention:
            raise ValueError("患者数据外发必须启用零留存策略")
        self.zero_retention = zero_retention
        self.consent = consent_service
        self.audit = audit_service

    @contextmanager
    def _session_scope(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
        finally:
            session.close()

    def _validate_files(
        self, session: Session, context: AccessContext, request: ChatRequest
    ) -> list[UUID]:
        document_ids: list[UUID] = []
        for file_ref in request.file_refs:
            if file_ref.tenant_id != context.tenant_id or file_ref.patient_id != context.patient_id:
                raise AccessDenied("外部模型文件不属于当前租户或患者")
            if file_ref.size_bytes <= 0 or file_ref.size_bytes > MAX_UPLOAD_SIZE_BYTES:
                raise ValueError("外部模型文件大小不合法")
            if file_ref.content_type not in {mime for mimes in SUPPORTED_TYPES.values() for mime in mimes}:
                raise ValueError("外部模型文件类型不受支持")
            document = session.scalar(
                select(Document).where(
                    Document.id == file_ref.document_id,
                    Document.tenant_id == context.tenant_id,
                    Document.patient_id == context.patient_id,
                    Document.status != DocumentStatus.DELETED,
                    Document.deleted_at.is_(None),
                )
            )
            if document is None:
                raise AccessDenied("外部模型文件不存在或不属于当前患者")
            if document.storage_key != file_ref.object_key:
                raise AccessDenied("外部模型文件对象与文档记录不匹配")
            document_ids.append(file_ref.document_id)
        return document_ids

    def _request_hashes(self, request: ChatRequest) -> list[str]:
        if request.evidence_hashes:
            return list(request.evidence_hashes)
        return [hashlib.sha256(item.text.encode("utf-8")).hexdigest() for item in request.evidence]

    def chat(self, context: AccessContext, request: ChatRequest) -> ChatResponse:
        if self.provider_id not in self.allowed_providers:
            raise ValueError("模型供应商不在白名单中")
        if request.model not in self.allowed_models:
            raise ValueError("模型不在白名单中")
        if request.policy_version != self.policy_version:
            raise ValueError("模型策略版本不匹配")
        with self._session_scope() as session:
            consent = self.consent or ConsentService(session, policy_version=self.policy_version)
            audit = self.audit or AuditService(session)
            consent.require_active_consent(context, ConsentPurpose.EXTERNAL_MODEL)
            document_ids = self._validate_files(session, context, request)
            evidence_chars = sum(len(item.text) for item in request.evidence)
            if len(request.prompt) + len(request.system_prompt) + evidence_chars > self.max_request_chars:
                raise ValueError("外部模型请求超过大小限制")
            audit_metadata = {
                "provider": self.provider_id,
                "model": request.model,
                "policy_version": request.policy_version,
                "purpose": request.purpose,
                "document_ids": [str(document_id) for document_id in document_ids],
                "evidence_hashes": self._request_hashes(request),
                "trace_id": request.trace_id,
            }
            try:
                response = self.chat_model.generate(request)
            except Exception:
                audit.record(
                    context,
                    AuditAction.EXTERNAL_MODEL_CALL,
                    resource_type="external_model",
                    payload_metadata={**audit_metadata, "status": "failed"},
                )
                session.commit()
                raise
            audit.record(
                context,
                AuditAction.EXTERNAL_MODEL_CALL,
                resource_type="external_model",
                payload_metadata={**audit_metadata, "status": "succeeded"},
            )
            session.commit()
            return response
