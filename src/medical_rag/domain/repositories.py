from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from medical_rag.domain.enums import ConsentPurpose, DocumentStatus
from medical_rag.domain.models import AuditEvent, Consent, Document, Patient, Tenant


class TenantRepository(Protocol):
    def get(self, tenant_id: UUID) -> Tenant | None: ...


class PatientRepository(Protocol):
    def get(self, patient_id: UUID, tenant_id: UUID) -> Patient | None: ...


class DocumentRepository(Protocol):
    def get_owned(self, document_id: UUID, patient_id: UUID) -> Document | None: ...


class ConsentRepository(Protocol):
    def active_for(self, patient_id: UUID, purpose: ConsentPurpose) -> Consent | None: ...


class AuditRepository(Protocol):
    def append(self, event: AuditEvent) -> None: ...


class SqlDocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_owned(self, document_id: UUID, patient_id: UUID) -> Document | None:
        statement: Select[tuple[Document]] = select(Document).where(
            Document.id == document_id,
            Document.patient_id == patient_id,
            Document.status != DocumentStatus.DELETED,
            Document.deleted_at.is_(None),
        )
        return self.session.execute(statement).scalar_one_or_none()

