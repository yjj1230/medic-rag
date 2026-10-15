from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.api.dependencies import (
    current_access_context,
    get_job_publisher,
    get_object_store,
    get_private_index_writer,
    get_session,
)
from medical_rag.auth.policy import AccessContext
from medical_rag.audit.service import AuditService
from medical_rag.core.errors import AccessDenied
from medical_rag.domain.enums import AuditAction, DocumentStatus
from medical_rag.domain.models import Document, DocumentVersion, ParseJob
from medical_rag.ingest.delete_service import DeleteService
from medical_rag.ingest.upload_service import JobPublisher, UploadService
from medical_rag.quality.documents import derive_document_quality
from medical_rag.storage.object_store import ObjectStore
from medical_rag.retrieval.contracts import PrivateIndexWriter


router = APIRouter(prefix="/v1/documents", tags=["documents"])


class DocumentResponse(BaseModel):
    id: UUID
    filename: str
    content_type: str
    status: str
    # Task 7：读取时派生的质量信号（增量式）。
    quality_status: str
    failure_reason_code: str | None
    failure_reason_label: str | None


class DocumentsListResponse(BaseModel):
    items: list[DocumentResponse]


class DeletionResponse(BaseModel):
    document_id: UUID
    completed_layers: list[str]
    failed_layers: list[str]
    failure_details: dict[str, str] = Field(default_factory=dict)
    retryable_layers: list[str] = Field(default_factory=list)


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
def upload_document(
    file: UploadFile = File(...),
    context: AccessContext = Depends(current_access_context),
    session: Session = Depends(get_session),
    object_store: ObjectStore = Depends(get_object_store),
    publisher: JobPublisher = Depends(get_job_publisher),
) -> DocumentResponse:
    document = UploadService(session, object_store, publisher).create_document(context, file)
    session.commit()
    versions = session.scalars(
        select(DocumentVersion).where(DocumentVersion.document_id == document.id)
    ).all()
    jobs = session.scalars(
        select(ParseJob)
        .join(DocumentVersion, ParseJob.document_version_id == DocumentVersion.id)
        .where(DocumentVersion.document_id == document.id)
    ).all()
    derived = derive_document_quality(document, versions, jobs)
    return DocumentResponse(
        id=document.id,
        filename=document.filename,
        content_type=document.content_type,
        status=document.status.value,
        quality_status=derived.quality_status,
        failure_reason_code=derived.failure_reason_code,
        failure_reason_label=derived.failure_reason_label,
    )


@router.delete("/{document_id}", response_model=DeletionResponse)
def delete_document(
    document_id: UUID,
    context: AccessContext = Depends(current_access_context),
    session: Session = Depends(get_session),
    object_store: ObjectStore = Depends(get_object_store),
    private_index_writer: PrivateIndexWriter = Depends(get_private_index_writer),
) -> DeletionResponse:
    receipt = DeleteService(
        session, object_store, private_index_writer=private_index_writer
    ).delete_document(context, document_id)
    return DeletionResponse(
        document_id=document_id,
        completed_layers=receipt.completed_layers,
        failed_layers=receipt.failed_layers,
        failure_details=receipt.failure_details,
        retryable_layers=list(receipt.retryable_layers),
    )


@router.get("", response_model=DocumentsListResponse)
def list_documents(
    context: AccessContext = Depends(current_access_context),
    session: Session = Depends(get_session),
) -> DocumentsListResponse:
    documents = session.scalars(
        select(Document).where(
            Document.tenant_id == context.tenant_id,
            Document.patient_id == context.patient_id,
            Document.status != DocumentStatus.DELETED,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc())
    ).all()

    items: list[DocumentResponse] = []
    if documents:
        document_ids = [doc.id for doc in documents]
        versions = session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids))
        ).all()
        jobs = session.scalars(
            select(ParseJob)
            .join(DocumentVersion, ParseJob.document_version_id == DocumentVersion.id)
            .where(DocumentVersion.document_id.in_(document_ids))
        ).all()

        versions_by_document: dict[object, list[DocumentVersion]] = {}
        for version in versions:
            versions_by_document.setdefault(version.document_id, []).append(version)

        version_document = {version.id: version.document_id for version in versions}
        jobs_by_document: dict[object, list[ParseJob]] = {}
        for job in jobs:
            jobs_by_document.setdefault(version_document[job.document_version_id], []).append(job)

        for doc in documents:
            derived = derive_document_quality(
                doc,
                versions_by_document.get(doc.id, []),
                jobs_by_document.get(doc.id, []),
            )
            items.append(
                DocumentResponse(
                    id=doc.id,
                    filename=doc.filename,
                    content_type=doc.content_type,
                    status=doc.status.value,
                    quality_status=derived.quality_status,
                    failure_reason_code=derived.failure_reason_code,
                    failure_reason_label=derived.failure_reason_label,
                )
            )

    return DocumentsListResponse(items=items)


@router.get("/{document_id}")
def download_document(
    document_id: UUID,
    context: AccessContext = Depends(current_access_context),
    session: Session = Depends(get_session),
    object_store: ObjectStore = Depends(get_object_store),
) -> StreamingResponse:
    document = session.execute(
        select(Document).where(
            Document.id == document_id,
            Document.tenant_id == context.tenant_id,
            Document.patient_id == context.patient_id,
            Document.status != DocumentStatus.DELETED,
            Document.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if document is None:
        raise AccessDenied("当前身份无权访问该患者文档")

    AuditService(session).record(
        context,
        AuditAction.DOCUMENT_ACCESSED,
        resource_type="document",
        resource_id=document.id,
    )
    session.commit()
    return StreamingResponse(
        object_store.get(document.storage_key),
        media_type=document.content_type,
        headers={"Content-Disposition": f'attachment; filename="{document.filename}"'},
    )
