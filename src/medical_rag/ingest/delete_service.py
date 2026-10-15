from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.audit.service import AuditService
from medical_rag.auth.policy import AccessContext
from medical_rag.core.errors import AccessDenied
from medical_rag.domain.enums import AuditAction, DocumentStatus
from medical_rag.domain.models import Document, DocumentVersion, EvidenceUnit, MedicalFact
from medical_rag.indexing.outbox import OutboxService
from medical_rag.retrieval.contracts import PrivateIndexWriter
from medical_rag.storage.object_store import ObjectStore


class DerivedDataDeleter(Protocol):
    def delete_for_document(self, document_id: UUID) -> None: ...


class NoopDerivedDataDeleter:
    def delete_for_document(self, document_id: UUID) -> None:
        return None


@dataclass(slots=True)
class DeletionReceipt:
    completed_layers: list[str] = field(default_factory=list)
    failed_layers: list[str] = field(default_factory=list)
    # 让失败诊断保持机器可读，同时不持久化异常
    # 消息（其中可能含对象键或患者数据）。以层名
    # 为键、异常类为值。
    failure_details: dict[str, str] = field(default_factory=dict)

    @property
    def retryable_layers(self) -> tuple[str, ...]:
        """应由后续删除尝试重试的各层。"""

        return tuple(self.failed_layers)


class DeleteService:
    def __init__(
        self,
        session: Session,
        object_store: ObjectStore,
        derived_data_deleter: DerivedDataDeleter | None = None,
        audit_service: AuditService | None = None,
        private_index_writer: PrivateIndexWriter | None = None,
        outbox_service: OutboxService | None = None,
        metrics: object | None = None,
    ) -> None:
        # 保留原先第三个位置参数的形态，同时让
        # 直接传入 PrivateIndexWriter 更便利。
        if (
            private_index_writer is None
            and derived_data_deleter is not None
            and hasattr(derived_data_deleter, "delete_document")
            and not hasattr(derived_data_deleter, "delete_for_document")
        ):
            private_index_writer = derived_data_deleter  # type: ignore[assignment]
            derived_data_deleter = None
        self.session = session
        self.object_store = object_store
        self.derived_data_deleter = derived_data_deleter or NoopDerivedDataDeleter()
        self.audit = audit_service or AuditService(session)
        self.private_index_writer = private_index_writer
        self.outbox = outbox_service or OutboxService(session)
        # Task 11：可选的有界可观测记录器（为 None 时为空操作）。
        self.metrics = metrics

    @staticmethod
    def _record_residual(metrics: object | None, count: int) -> None:
        if metrics is None:
            return
        record = getattr(metrics, "record_deletion_residual", None)
        if callable(record):
            record(count)

    def delete_document(self, context: AccessContext, document_id: UUID) -> DeletionReceipt:
        document = self.session.execute(
            select(Document).where(
                Document.id == document_id,
                Document.tenant_id == context.tenant_id,
                Document.patient_id == context.patient_id,
            )
        ).scalar_one_or_none()
        if document is None:
            raise AccessDenied("当前身份无权删除该患者文档")

        receipt = DeletionReceipt()
        already_deleted = document.status == DocumentStatus.DELETED
        if not already_deleted:
            now = datetime.now(UTC)
            document.status = DocumentStatus.DELETED
            document.deleted_at = now
            versions = self.session.scalars(
                select(DocumentVersion).where(DocumentVersion.document_id == document_id)
            ).all()
            for version in versions:
                version.status = DocumentStatus.DELETED
                version.deleted_at = now
                for evidence in self.session.scalars(
                    select(EvidenceUnit).where(EvidenceUnit.document_version_id == version.id)
                ).all():
                    evidence.deleted_at = now
                for fact in self.session.scalars(
                    select(MedicalFact).where(MedicalFact.document_version_id == version.id)
                ).all():
                    fact.deleted_at = now

            self.audit.record(
                context,
                AuditAction.DELETION_REQUESTED,
                resource_type="document",
                resource_id=document.id,
            )
            self.session.commit()
        else:
            # 重试本身就是一个可观测操作。这样可以避免
            # 失败的对外清理在数据库墓碑
            # 已经提交之后变得不可见。
            self.audit.record(
                context,
                AuditAction.DELETION_REQUESTED,
                resource_type="document",
                resource_id=document.id,
                payload_metadata={"retry": True},
            )
            self.session.commit()
        # 即便文档已被删除，每个外部层仍需重试；
        # 此前的私有索引/对象存储失败不得因为
        # 一次提前返回而永久不可见。
        receipt.completed_layers.append("database")

        # 源墓碑在任何外部清理之前就已提交。因此
        # 生命周期事件可以与对象/索引删除相互独立地重试，
        # 且绝不携带患者文本。
        try:
            versions = self.session.scalars(
                select(DocumentVersion).where(DocumentVersion.document_id == document.id)
            ).all()
            self.outbox.create_lifecycle_event(
                event_type="document.deleted",
                aggregate_id=document.id,
                idempotency_key=f"document:{document.id}:deleted",
                payload={
                    "document_id": str(document.id),
                    "version_ids": [str(version.id) for version in versions],
                    "status": DocumentStatus.DELETED.value,
                },
            )
            self.session.commit()
            receipt.completed_layers.append("outbox")
        except Exception as exc:
            self.session.rollback()
            receipt.failed_layers.append("outbox")
            receipt.failure_details["outbox"] = type(exc).__name__[:100]

        try:
            self.object_store.delete(document.storage_key)
            receipt.completed_layers.append("object_store")
        except Exception as exc:
            receipt.failed_layers.append("object_store")
            receipt.failure_details["object_store"] = type(exc).__name__[:100]

        try:
            self.derived_data_deleter.delete_for_document(document.id)
            receipt.completed_layers.append("derived_data")
        except Exception as exc:
            receipt.failed_layers.append("derived_data")
            receipt.failure_details["derived_data"] = type(exc).__name__[:100]

        if self.private_index_writer is not None:
            try:
                self.private_index_writer.delete_document(document.id)
                receipt.completed_layers.append("private_index")
            except Exception as exc:
                receipt.failed_layers.append("private_index")
                receipt.failure_details["private_index"] = type(exc).__name__[:100]
        # Task 11：成功的私有索引清理留下零残留项；失败的一次
        # 留下潜在残留项（非零，会阻塞门禁）。
        self._record_residual(
            self.metrics,
            0 if "private_index" in receipt.completed_layers else int(
                "private_index" in receipt.failed_layers
            ),
        )

        payload_metadata = {
            "completed_layers": receipt.completed_layers,
            "failed_layers": receipt.failed_layers,
            "failure_details": receipt.failure_details,
            "retryable_layers": list(receipt.retryable_layers),
        }
        if receipt.failed_layers:
            # 墓碑是持久的，但外部清理尚未完成。
            # 让该操作保持在可重试的已请求状态，而不是
            # 声称删除已完成。
            self.audit.record(
                context,
                AuditAction.DELETION_REQUESTED,
                resource_type="document",
                resource_id=document.id,
                payload_metadata={**payload_metadata, "retry": True},
            )
        else:
            self.audit.record(
                context,
                AuditAction.DELETION_COMPLETED,
                resource_type="document",
                resource_id=document.id,
                payload_metadata=payload_metadata,
            )
        self.session.commit()
        return receipt
