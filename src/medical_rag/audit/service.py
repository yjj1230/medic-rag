from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy.orm import Session

from medical_rag.auth.policy import AccessContext
from medical_rag.domain.enums import AuditAction
from medical_rag.domain.models import AuditEvent


class AuditService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(self, event: AuditEvent) -> None:
        self.session.add(event)
        self.session.flush()

    def record(
        self,
        context: AccessContext,
        action: AuditAction,
        *,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        payload_hash: str | None = None,
        payload_metadata: Mapping[str, object] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            tenant_id=context.tenant_id,
            patient_id=context.patient_id,
            actor_user_id=context.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            payload_hash=payload_hash,
            payload_metadata=dict(payload_metadata or {}),
        )
        self.append(event)
        return event

    def record_external_transfer(
        self,
        context: AccessContext,
        document_ids: list[UUID],
        model_name: str,
        purpose: str,
    ) -> None:
        self.record(
            context,
            AuditAction.EXTERNAL_MODEL_CALL,
            resource_type="external_model",
            payload_metadata={
                "document_ids": [str(document_id) for document_id in document_ids],
                "model_name": model_name,
                "purpose": purpose,
            },
        )

    def record_index_job(
        self,
        context: AccessContext,
        *,
        job_id: UUID,
        status: str,
        attempts: int,
        error_code: str | None = None,
        index_version: str | None = None,
        embedding_model: str | None = None,
        worker_id: str | None = None,
        outcome: str | None = None,
    ) -> AuditEvent:
        """记录一个持久化索引作业的重试/失败状态。

        索引 worker 可以在每次尝试后调用这个接缝。只持久化有界的标识符和计数器；
        提供方的异常消息有意不接受。
        """

        return self.record(
            context,
            AuditAction.RETRIEVAL,
            resource_type="index_job",
            resource_id=job_id,
            payload_metadata={
                "status": status,
                "attempts": attempts,
                "error_code": error_code,
                "index_version": index_version,
                "embedding_model": embedding_model,
                "worker_id": worker_id,
                "outcome": outcome,
            },
        )

    def record_lifecycle(
        self,
        context: AccessContext,
        *,
        event_type: str,
        aggregate_id: UUID | str,
        outcome: str = "requested",
        metadata: Mapping[str, object] | None = None,
    ) -> AuditEvent:
        """记录一次有界的状态迁移，且不含源文本。"""

        payload = {
            "event_type": str(event_type)[:120],
            "aggregate_id": str(aggregate_id)[:200],
            "outcome": str(outcome)[:40],
        }
        payload.update(dict(metadata or {}))
        return self.record(
            context,
            AuditAction.LIFECYCLE,
            resource_type="lifecycle",
            payload_metadata=payload,
        )

    def record_reconciliation(
        self,
        context: AccessContext,
        *,
        scope: str,
        outcome: str,
        item_id: UUID | str,
        reason: str,
        metadata: Mapping[str, object] | None = None,
    ) -> AuditEvent:
        """记录一次隐私安全的陈旧索引扫描/修复结果。"""

        payload = {
            "scope": str(scope)[:40],
            "outcome": str(outcome)[:40],
            "item_id": str(item_id)[:200],
            "reason": str(reason)[:80],
        }
        payload.update(dict(metadata or {}))
        return self.record(
            context,
            AuditAction.RECONCILIATION,
            resource_type="index_reconciliation",
            payload_metadata=payload,
        )
