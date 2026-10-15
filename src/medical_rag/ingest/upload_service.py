from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Protocol
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy.orm import Session

from medical_rag.auth.policy import AccessContext
from medical_rag.consent.service import ConsentService
from medical_rag.core.errors import MedicalRagError
from medical_rag.domain.enums import ConsentPurpose, DocumentStatus, ParseJobStatus
from medical_rag.domain.models import Document, DocumentVersion, ParseJob
from medical_rag.storage.file_policy import MAX_UPLOAD_SIZE_BYTES, validate_upload
from medical_rag.storage.object_store import ObjectStore


class VirusScanner(Protocol):
    def scan(self, content: bytes) -> None: ...


class JobPublisher(Protocol):
    def publish(self, job_id: UUID) -> None: ...


class JobPublishError(MedicalRagError):
    pass


class AllowAllVirusScanner:
    """本地适配器；生产必须用企业级扫描器替换它。"""

    def scan(self, content: bytes) -> None:
        return None


class NoopJobPublisher:
    def publish(self, job_id: UUID) -> None:
        return None


class CeleryJobPublisher:
    def __init__(self, broker_url: str) -> None:
        from celery import Celery

        self.app = Celery("medical_rag", broker=broker_url)

    def publish(self, job_id: UUID) -> None:
        self.app.send_task("medical_rag.parse_document", args=[str(job_id)])


@dataclass(frozen=True, slots=True)
class UploadResult:
    document: Document
    version: DocumentVersion
    parse_job: ParseJob


class UploadService:
    def __init__(
        self,
        session: Session,
        object_store: ObjectStore,
        publisher: JobPublisher | None = None,
        scanner: VirusScanner | None = None,
        consent_service: ConsentService | None = None,
    ) -> None:
        self.session = session
        self.object_store = object_store
        self.publisher = publisher or NoopJobPublisher()
        self.scanner = scanner or AllowAllVirusScanner()
        self.consent_service = consent_service or ConsentService(session)

    def create_document(self, context: AccessContext, file: UploadFile) -> Document:
        self.consent_service.require_active_consent(context, ConsentPurpose.REPORT_PROCESSING)

        content = file.file.read(MAX_UPLOAD_SIZE_BYTES + 1)
        validated = validate_upload(
            file.filename or "",
            file.content_type or "",
            len(content),
            content[:4096],
        )
        self.scanner.scan(content)

        document_id = uuid4()
        version_id = uuid4()
        job_id = uuid4()
        storage_key = (
            f"tenants/{context.tenant_id}/patients/{context.patient_id}/"
            f"documents/{document_id}/versions/1/original{validated.extension}"
        )
        self.object_store.put(storage_key, BytesIO(content), validated.content_type)

        document = Document(
            id=document_id,
            tenant_id=context.tenant_id,
            patient_id=context.patient_id,
            filename=validated.filename,
            content_type=validated.content_type,
            storage_key=storage_key,
            status=DocumentStatus.UPLOADED,
        )
        version = DocumentVersion(
            id=version_id,
            document_id=document_id,
            version_number=1,
            status=DocumentStatus.UPLOADED,
        )
        parse_job = ParseJob(
            id=job_id,
            document_version_id=version_id,
            status=ParseJobStatus.PENDING,
        )
        try:
            self.session.add_all([document, version, parse_job])
            self.session.flush()
            self.publisher.publish(job_id)
        except Exception as exc:
            if parse_job in self.session:
                parse_job.error_code = "worker_publish_failed"
                parse_job.status = ParseJobStatus.PENDING
                self.session.commit()
            self.object_store.delete(storage_key)
            if isinstance(exc, JobPublishError):
                raise
            raise JobPublishError("解析任务发布失败") from exc
        return document
