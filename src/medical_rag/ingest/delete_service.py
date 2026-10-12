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
    # Keep failure diagnostics machine-readable without persisting exception
    # messages (which may contain object keys or patient data).  The layer
    # name is the key and the exception class is the value.
    failure_details: dict[str, str] = field(default_factory=dict)

    @property
    def retryable_layers(self) -> tuple[str, ...]:
        """Layers that should be retried by a subsequent deletion attempt."""

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
        # Preserve the original third-positional-argument shape while making
        # it convenient to pass a PrivateIndexWriter directly.
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
        # Task 11: optional bounded observability recorder (no-op when None).
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
            # A retry is an observable operation in its own right.  This keeps
            # failed external cleanup from becoming invisible after the
            # database tombstone has already been committed.
            self.audit.record(
                context,
                AuditAction.DELETION_REQUESTED,
                resource_type="document",
                resource_id=document.id,
                payload_metadata={"retry": True},
            )
            self.session.commit()
        # Even an already-deleted document needs each external layer retried;
        # a previous private-index/object-store failure must not become
        # permanently invisible behind an early return.
        receipt.completed_layers.append("database")

        # The source tombstone is committed before any external cleanup.  The
        # lifecycle event is therefore retryable independently of object/index
        # deletion and never carries patient text.
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
        # Task 11: a successful private-index cleanup leaves zero residuals; a
        # failed one leaves a potential residual (non-zero, gate-blocking).
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
            # A tombstone is durable, but external cleanup is incomplete.
            # Keep the operation in a retryable requested state instead of
            # claiming deletion completed.
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
