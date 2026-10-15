"""持久化索引作业编排与生产组合检查。

编排器刻意做得很小：PostgreSQL 持有作业状态，OutboxService 持有租约，
IndexWorker/写入器负责实际写入。这样在保证重复投递安全的同时，把可见性与
启动安全性显式地收敛到一个接缝上。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.audit.service import AuditService
from medical_rag.auth.policy import AccessContext
from medical_rag.consent.service import ConsentService
from medical_rag.domain.enums import DocumentStatus, IndexJobStatus, ParseJobStatus
from medical_rag.domain.models import Document, DocumentVersion, EvidenceUnit, IndexJob, OutboxEvent, ParseJob
from medical_rag.indexing.outbox import OutboxService
from medical_rag.knowledge.contracts import IndexRecord, PublicIndexWriter
from medical_rag.models.contracts import EmbeddingPipeline
from medical_rag.retrieval.contracts import PrivateIndexWriter
from medical_rag.retrieval.indexing import IndexReceipt, IndexWorker


@dataclass(frozen=True, slots=True)
class ProductionComposition:
    """索引所需的最小生产依赖图。

    本地/测试部署可以显式注入确定性的替身。生产依赖图不得静默回落到
    内存写入器、缺失的向量化、缺失的知情同意检查，或未配置的审计汇。
    """

    public_writer: Any | None = None
    private_writer: Any | None = None
    embedding_pipeline: EmbeddingPipeline | None = None
    consent_service: ConsentService | Any | None = None
    audit_service: AuditService | Any | None = None
    audit_context: AccessContext | None = None
    index_orchestrator: "IndexOrchestrator" | None = None

    def validate(self) -> None:
        missing: list[str] = []
        if self.public_writer is None:
            missing.append("public_writer")
        if self.private_writer is None:
            missing.append("private_writer")
        if self.embedding_pipeline is None:
            missing.append("embedding_pipeline")
        if self.consent_service is None:
            missing.append("consent_service")
        if self.audit_service is None:
            missing.append("audit_service")
        if self.audit_context is None:
            missing.append("audit_context")
        if missing:
            raise RuntimeError("unsafe production composition; missing " + ", ".join(missing))

        for name, writer, visibility in (
            ("public_writer", self.public_writer, "public"),
            ("private_writer", self.private_writer, "private"),
        ):
            if not callable(getattr(writer, "upsert", None)):
                raise RuntimeError(f"unsafe production composition; {name}.upsert is missing")
            if visibility == "private" and not callable(getattr(writer, "delete_document", None)):
                raise RuntimeError("unsafe production composition; private_writer.delete_document is missing")
            declared = getattr(writer, "visibility", getattr(writer, "scope", None))
            if declared != visibility:
                raise RuntimeError(f"unsafe production composition; {name} visibility is not {visibility}")

        if not callable(getattr(self.embedding_pipeline, "embed_records", None)):
            raise RuntimeError("unsafe production composition; embedding_pipeline.embed_records is missing")
        if not isinstance(getattr(self.embedding_pipeline, "dimensions", None), int) or self.embedding_pipeline.dimensions <= 0:
            raise RuntimeError("unsafe production composition; embedding dimensions are invalid")
        if not callable(getattr(self.consent_service, "require_active_consent", None)):
            raise RuntimeError("unsafe production composition; consent service is invalid")
        if not callable(getattr(self.audit_service, "record_index_job", None)):
            raise RuntimeError("unsafe production composition; audit service is invalid")


class IndexOrchestrator:
    """消费已租赁的 IndexJob，并把每条记录路由到一个可见性写入器。"""

    def __init__(
        self,
        session: Session,
        *,
        public_writer: PublicIndexWriter | Any | None = None,
        private_writer: PrivateIndexWriter | Any | None = None,
        public_index_writer: PublicIndexWriter | Any | None = None,
        private_index_writer: PrivateIndexWriter | Any | None = None,
        outbox_service: OutboxService | None = None,
        embedding_pipeline: EmbeddingPipeline | None = None,
        allow_unembedded: bool = False,
        audit_service: AuditService | None = None,
        audit_context: AccessContext | None = None,
        lease_seconds: int = 60,
        max_attempts: int = 5,
        metrics: object | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.session = session
        self.public_writer = public_writer if public_writer is not None else public_index_writer
        self.private_writer = private_writer if private_writer is not None else private_index_writer
        self.outbox = outbox_service or OutboxService(session)
        self.embedding_pipeline = embedding_pipeline
        self.allow_unembedded = allow_unembedded
        self.audit = audit_service
        self.audit_context = audit_context
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        # 可选的有界可观测记录器（为 None 时为空操作）。接受一个
        # 形如 ``PipelineMetricsRecorder``、暴露 ``increment`` 的对象。
        self.metrics = metrics
        if audit_service is not None and audit_context is None:
            raise ValueError("audit_context is required when audit_service is configured")

    @staticmethod
    def _metric(metrics: object | None, name: str, count: int = 1) -> None:
        """向可选记录器发出一个有界计数器（未设置时为空操作）。"""

        if metrics is None:
            return
        increment = getattr(metrics, "increment", None)
        if callable(increment):
            increment(name, count)

    @staticmethod
    def _classify_visibility(evidence: EvidenceUnit) -> str:
        """只对明确、无歧义的证据种类做分类。

        ACL 字段缺失并不构成公开可见的证据；有歧义的记录会 fail closed，
        而不是被路由到公开索引。
        """
        kind = getattr(evidence.kind, "value", evidence.kind)
        if kind == "knowledge_chunk" and evidence.patient_id is None:
            return "public"
        if kind in {"report_text", "report_table"} and evidence.patient_id is not None:
            return "private"
        raise ValueError("ambiguous evidence visibility")

    def _audit(self, job: IndexJob, worker_id: str, outcome: str | None = None) -> None:
        if self.audit is None or self.audit_context is None:
            return
        try:
            self.audit.record_index_job(
                self.audit_context,
                job_id=job.id,
                status=job.status.value,
                attempts=job.attempts,
                error_code=job.error_code,
                index_version=job.index_version,
                embedding_model=job.embedding_model,
                worker_id=worker_id,
                outcome=outcome,
            )
        except TypeError as exc:
            raise RuntimeError(
                "audit adapter must accept worker_id and outcome for index jobs"
            ) from exc
        self.session.commit()

    def _event_for(self, job: IndexJob) -> Any | None:
        events = self.session.scalars(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == str(job.document_version_id))
        ).all()
        for event in events:
            if str((event.payload or {}).get("job_id", "")) == str(job.id):
                return event
        return None

    def _publish_event(self, job: IndexJob) -> None:
        event = self._event_for(job)
        if event is not None:
            self.outbox.publish(event.id)
            self.session.commit()

    def _dead_letter(self, job: IndexJob, worker_id: str) -> None:
        """把永久失败的作业记录为持久化、可告警的死信。

        ``PERMANENT_FAILED`` 本来是一个静默的终态：没有任何东西消费它，因此一个
        耗尽重试预算的作业会消失得毫无运维痕迹。每个作业一条 OutboxEvent（以作业 id
        为键，因此重复的状态迁移不会产生重复），外加一个有界计数器，为告警提供
        持久化的挂钩。只写入标识符、版本和计数 —— 绝不写正文。
        """

        self.outbox.create_lifecycle_event(
            event_type="index.job_dead_letter",
            aggregate_id=job.document_version_id,
            idempotency_key=f"index-job-dead-letter:{job.id}",
            payload={
                "job_id": str(job.id),
                "document_version_id": str(job.document_version_id),
                "evidence_unit_id": str(job.evidence_unit_id),
                "status": job.status.value,
                "attempts": job.attempts,
                "error_code": job.error_code,
                "worker_id": worker_id,
                "parser_version": job.parser_version,
                "embedding_model": job.embedding_model,
                "index_version": job.index_version,
            },
        )
        self.session.commit()
        self._metric(self.metrics, "index_job_dead_letter")

    def _retry(self, job: IndexJob, worker_id: str, exc: Exception) -> IndexReceipt:
        # 优先使用失败阶段已经记录的错误码：worker 的错误码
        # （``parse_job_not_found``、``source_not_found``）对值班人员
        # 比异常类名更有可操作性。
        code = job.error_code or type(exc).__name__[:100]
        if job.attempts >= self.max_attempts:
            job.status = IndexJobStatus.PERMANENT_FAILED
            job.error_code = code
            job.worker_id = None
            job.lease_expires_at = None
            job.finished_at = datetime.now(UTC)
        else:
            # IndexJob.retry 只对 IndexWorker 尚未计过
            # 该次尝试之前发生的失败，才递增尝试次数。
            job.error_code = code
            job.worker_id = None
            job.lease_expires_at = None
            delay = min(3600, 5 * (2 ** max(0, job.attempts - 1)))
            job.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
            job.status = IndexJobStatus.RETRYABLE_FAILED
        job.finished_at = job.finished_at or datetime.now(UTC)
        self.session.commit()
        self._audit(job, worker_id, "retry" if job.status == IndexJobStatus.RETRYABLE_FAILED else "failure")
        if job.status == IndexJobStatus.PERMANENT_FAILED:
            # 死信：持久化、去重、可告警。
            self._dead_letter(job, worker_id)
        event = self._event_for(job)
        if event is not None and job.status == IndexJobStatus.RETRYABLE_FAILED:
            self.outbox.retry_event(event.id, datetime.now(UTC))
            self.session.commit()
        return IndexReceipt(job.id, job.status, indexed_count=0)

    def _public_record(self, job: IndexJob, evidence: EvidenceUnit, document: Document | None, version: DocumentVersion) -> IndexRecord:
        eligible = (
            version.status in {DocumentStatus.READY, DocumentStatus.NEEDS_REVIEW}
            and (document is None or document.status in {DocumentStatus.READY, DocumentStatus.NEEDS_REVIEW})
            and evidence.deleted_at is None
            and version.deleted_at is None
            and (document is None or document.deleted_at is None)
        )
        if not eligible:
            raise RuntimeError("public source is not active and retrieval-eligible")
        return IndexRecord(
            id=str(evidence.id),
            text=evidence.text,
            visibility="public",
            source_id=str(evidence.id),
            source_title=document.filename if document is not None else "knowledge",
            source_version=f"v{version.version_number}",
            page_number=evidence.page_number,
            sheet_name=evidence.sheet_name,
            cell_range=evidence.cell_range,
            source_anchor=evidence.source_anchor,
            confidence=evidence.confidence,
            content_hash=None,
            parser_version=job.parser_version,
            embedding_model=job.embedding_model,
            index_version=job.index_version,
            retrieval_eligible=True,
            source_active=True,
            deleted=False,
        )

    def _process_public(self, job: IndexJob, worker_id: str, evidence: EvidenceUnit, version: DocumentVersion, document: Document | None) -> IndexReceipt:
        writer = self.public_writer
        if writer is None:
            return self._retry(job, worker_id, RuntimeError("public index writer is not configured"))
        try:
            record = self._public_record(job, evidence, document, version)
        except Exception as exc:
            return self._retry(job, worker_id, exc)
        if self.embedding_pipeline is None and not self.allow_unembedded:
            raise RuntimeError("embedding pipeline is required")
        job.status = IndexJobStatus.RUNNING
        job.attempts += 1
        job.started_at = datetime.now(UTC)
        job.error_code = None
        self.session.commit()
        self._audit(job, worker_id, "attempt")
        try:
            if self.embedding_pipeline is not None:
                vector = self.embedding_pipeline.embed_records([record.text], job.embedding_model)[0]
                record = replace(record, embedding=vector, embedding_dimensions=self.embedding_pipeline.dimensions, embedding_version=getattr(self.embedding_pipeline, "model_version", None))
            writer.upsert([record])
        except Exception as exc:
            return self._retry(job, worker_id, exc)
        job.status = IndexJobStatus.SUCCEEDED
        job.finished_at = datetime.now(UTC)
        job.worker_id = None
        job.lease_expires_at = None
        job.next_retry_at = None
        self.session.commit()
        self._audit(job, worker_id, "success")
        self._publish_event(job)
        return IndexReceipt(job.id, job.status, indexed_count=1, retrieval_eligible=record.retrieval_eligible)

    def process(self, job_id: UUID, worker_id: str) -> IndexReceipt:
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id is required")
        job = self.session.get(IndexJob, job_id)
        if job is None:
            raise ValueError(f"索引任务不存在: {job_id}")
        if job.status == IndexJobStatus.SUCCEEDED:
            self._audit(job, worker_id, "duplicate")
            self._publish_event(job)
            return IndexReceipt(job.id, job.status, indexed_count=1)
        now = datetime.now(UTC)
        if not self.outbox.claim(job.id, worker_id, now, self.lease_seconds):
            return IndexReceipt(job.id, job.status, indexed_count=0)
        self.session.commit()
        evidence = self.session.get(EvidenceUnit, job.evidence_unit_id)
        version = self.session.get(DocumentVersion, job.document_version_id)
        document = self.session.get(Document, version.document_id) if version is not None else None
        if evidence is None or version is None:
            job.status = IndexJobStatus.PERMANENT_FAILED
            job.error_code = "source_not_found"
            job.worker_id = None
            job.lease_expires_at = None
            job.finished_at = job.finished_at or datetime.now(UTC)
            self.session.commit()
            self._audit(job, worker_id, "failure")
            self._dead_letter(job, worker_id)
            return IndexReceipt(job.id, job.status, indexed_count=0)
        try:
            visibility = self._classify_visibility(evidence)
        except ValueError as exc:
            return self._retry(job, worker_id, exc)
        if document is None and visibility != "public":
            return self._retry(job, worker_id, ValueError("document_not_found"))
        parse_job = self.session.scalar(
            select(ParseJob)
            .where(ParseJob.document_version_id == version.id)
            .order_by(ParseJob.created_at.desc())
        )
        if parse_job is not None and parse_job.status not in {
            ParseJobStatus.SUCCEEDED,
            ParseJobStatus.NEEDS_REVIEW,
        }:
            # 解析工作尚未就绪；不要调用向量化或写入器。
            return self._retry(job, worker_id, RuntimeError("parse_not_ready"))
        if visibility == "public":
            return self._process_public(job, worker_id, evidence, version, document)
        if self.private_writer is None:
            return self._retry(job, worker_id, RuntimeError("private index writer is not configured"))
        try:
            worker = IndexWorker(
                self.session,
                self.private_writer,
                embedding_pipeline=self.embedding_pipeline,
                allow_unembedded=self.allow_unembedded,
                audit_service=self.audit,
                audit_context=self.audit_context,
                worker_id=worker_id,
                lease_seconds=self.lease_seconds,
            )
            receipt = worker.run(job.id)
            if receipt.status == IndexJobStatus.SUCCEEDED:
                self._publish_event(job)
                return receipt
            if receipt.status in {
                IndexJobStatus.PERMANENT_FAILED,
                IndexJobStatus.FAILED,
            }:
                # worker 返回了终态失败，却没有抛出
                # 异常（源元数据缺失、解析作业缺失）。重试
                # 策略仍属于编排器：运行它，这样耗尽的
                # 预算会变成死信，而不是一行无人
                # 观察的静默 FAILED。
                return self._retry(
                    job, worker_id, RuntimeError(job.error_code or "index worker failed")
                )
            return receipt
        except Exception as exc:
            return self._retry(job, worker_id, exc)

    def run_once(self, worker_id: str, limit: int) -> list[IndexReceipt]:
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id is required")
        if limit <= 0:
            return []
        jobs = self.outbox.claim_pending(worker_id, datetime.now(UTC), limit=limit, lease_seconds=self.lease_seconds)
        self.session.commit()
        receipts: list[IndexReceipt] = []
        for job in jobs:
            receipts.append(self.process(job.id, worker_id))
        return receipts


__all__ = ["IndexOrchestrator", "ProductionComposition", "IndexReceipt"]
