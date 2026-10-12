from dataclasses import dataclass
from uuid import UUID

from medical_rag.core.errors import AccessDenied
from medical_rag.domain.enums import DocumentStatus
from medical_rag.domain.models import Document


@dataclass(frozen=True, slots=True)
class AccessContext:
    tenant_id: UUID
    patient_id: UUID
    user_id: UUID


def can_read_document(context: AccessContext, document: Document) -> bool:
    return (
        context.tenant_id == document.tenant_id
        and context.patient_id == document.patient_id
        and document.status != DocumentStatus.DELETED
        and document.deleted_at is None
    )


def require_document_access(context: AccessContext, document: Document) -> Document:
    if not can_read_document(context, document):
        raise AccessDenied("当前身份无权访问该患者文档")
    return document

