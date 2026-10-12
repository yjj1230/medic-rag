"""Private-index writer seams and deterministic test adapter."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.audit.service import AuditService
from medical_rag.auth.policy import AccessContext
from medical_rag.domain.enums import DocumentStatus, IndexJobStatus
from medical_rag.domain.models import Document, DocumentVersion, EvidenceUnit, IndexJob, ParseJob
from medical_rag.models.contracts import EmbeddingPipeline
from medical_rag.knowledge.contracts import IndexRecord
from medical_rag.retrieval.contracts import PrivateIndexRecord, PrivateIndexWriter


class InMemoryPrivateIndexWriter:
    """Deterministic in-memory implementation of :class:`PrivateIndexWriter`.

    Records are keyed by their public record ID for predictable lookup.  A
    second map tracks the P0 idempotency key, so retrying the same evidence
    write is a no-op while a changed parser/model identity is a new write.
    """

    def __init__(self, records: Iterable[PrivateIndexRecord] | None = None) -> None:
        self.records: dict[str, PrivateIndexRecord] = {}
        self._idempotency: dict[tuple[object, object, str, str | None], str] = {}
        self.deleted_documents: set[UUID] = set()
        if records:
            self.upsert(list(records))

    @staticmethod
    def _coerce_uuid(value: UUID | str | None, field_name: str) -> UUID:
        if value is None:
            raise ValueError(f"private index record requires {field_name}")
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"private index record {field_name} must be a UUID") from exc

    @classmethod
    def _normalize(cls, record: PrivateIndexRecord | IndexRecord) -> PrivateIndexRecord:
        if isinstance(record, PrivateIndexRecord):
            return record
        if not isinstance(record, IndexRecord):
            raise TypeError("private index writer accepts PrivateIndexRecord or IndexRecord")
        if record.visibility != "private":
            raise ValueError("private index writer accepts only private records")
        return PrivateIndexRecord(
            id=record.id,
            text=record.text,
            tenant_id=cls._coerce_uuid(record.tenant_id, "tenant_id"),
            patient_id=cls._coerce_uuid(record.patient_id, "patient_id"),
            document_id=cls._coerce_uuid(record.document_id, "document_id"),
            document_version_id=cls._coerce_uuid(
                record.document_version_id, "document_version_id"
            ),
            evidence_unit_id=cls._coerce_uuid(record.evidence_unit_id, "evidence_unit_id"),
            source_title=record.source_title or "",
            source_version=record.source_version or "",
            page_number=record.page_number,
            sheet_name=record.sheet_name,
            cell_range=record.cell_range,
            source_anchor=record.source_anchor,
            confidence=record.confidence,
            retrieval_eligible=record.retrieval_eligible,
            deleted=record.deleted,
            embedding=record.embedding,
            embedding_model=record.embedding_model,
            embedding_dimensions=record.embedding_dimensions,
            index_version=record.index_version or "private-v1",
            parser_version=record.parser_version or "parser-v1",
            content_hash=record.content_hash,
            embedding_version=record.embedding_version,
            source_active=record.source_active,
            created_at=record.created_at,
            updated_at=record.updated_at,
            # Task 5 provenance mirrors forwarded through normalization.
            raw_text=record.raw_text,
            clean_text=record.clean_text,
            section_path=record.section_path,
            parent_block_id=record.parent_block_id,
            chunk_index=record.chunk_index,
            quality_flags=record.quality_flags,
            cleaner_version=record.cleaner_version,
            chunker_version=record.chunker_version,
            extractor_version=record.extractor_version,
        )

    def upsert(self, records: list[PrivateIndexRecord | IndexRecord]) -> None:
        for value in records:
            record = self._normalize(value)
            key = record.idempotency_key
            previous = self.records.get(record.id)
            if previous is not None and previous.idempotency_key != key:
                self._idempotency.pop(previous.idempotency_key, None)
            existing_id = self._idempotency.get(key)
            if existing_id is not None and existing_id != record.id:
                # The identity already succeeded under another deterministic
                # record ID; skipping prevents duplicate indexing on retries.
                continue
            self.records[record.id] = record
            self._idempotency[key] = record.id
            self.deleted_documents.discard(record.document_id)

    def delete_document(self, document_id: UUID) -> None:
        for record_id, record in list(self.records.items()):
            if record.document_id == document_id:
                self.records.pop(record_id, None)
                # Remove every key that points at this record, including keys
                # from parser/model versions that were superseded in-place.
                for key, mapped_id in list(self._idempotency.items()):
                    if mapped_id == record_id:
                        self._idempotency.pop(key, None)
        self.deleted_documents.add(document_id)

    def delete_record(self, record_id: str | UUID) -> None:
        """Delete one deterministic index record for reconciliation."""

        record_id = str(record_id)
        record = self.records.pop(record_id, None)
        if record is None:
            return
        for key, mapped_id in list(self._idempotency.items()):
            if mapped_id == record_id:
                self._idempotency.pop(key, None)

    def all_records(self) -> tuple[PrivateIndexRecord, ...]:
        """Return records in deterministic insertion order."""

        return tuple(self.records.values())

    def get(self, record_id: str) -> PrivateIndexRecord | None:
        return self.records.get(record_id)

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True, slots=True)
class IndexReceipt:
    """Result of one durable index-job attempt."""

    job_id: UUID
    status: IndexJobStatus
    indexed_count: int = 0
    retrieval_eligible: bool = False

    @property
    def indexed_records(self) -> int:
        """Compatibility alias useful to callers reporting batch size."""

        return self.indexed_count


class IndexWorker:
    """Execute durable private-index jobs after parsing has committed rows."""

    def __init__(
        self,
        session: Session,
        private_index_writer: PrivateIndexWriter,
        embedding_pipeline: EmbeddingPipeline | None = None,
        allow_unembedded: bool = False,
        audit_service: AuditService | None = None,
        audit_context: AccessContext | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        metrics: object | None = None,
    ) -> None:
        self.session = session
        self.private_index_writer = private_index_writer
        self.embedding_pipeline = embedding_pipeline
        self.allow_unembedded = allow_unembedded
        self.audit = audit_service
        self.audit_context = audit_context
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        # Task 11: optional bounded observability recorder (no-op when None).
        self.metrics = metrics
        if self.audit is not None and self.audit_context is None:
            raise ValueError("audit_context is required when audit_service is configured")

    @staticmethod
    def _record(metrics: object | None, name: str, **kwargs: object) -> None:
        """Emit one metric onto the optional recorder (no-op when None)."""
        if metrics is None:
            return
        method = getattr(metrics, name, None)
        if callable(method):
            method(**kwargs)

    def _audit_job(self, job: IndexJob) -> None:
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
                worker_id=self.worker_id,
                outcome=(
                    "success" if job.status == IndexJobStatus.SUCCEEDED
                    else "failure" if job.status in {IndexJobStatus.FAILED, IndexJobStatus.PERMANENT_FAILED}
                    else "attempt"
                ),
            )
        except TypeError as exc:
            raise RuntimeError(
                "audit adapter must accept worker_id and outcome for index jobs"
            ) from exc
        self.session.commit()

    def run(self, job_id: UUID) -> IndexReceipt:
        job = self.session.get(IndexJob, job_id)
        if job is None:
            raise ValueError(f"索引任务不存在: {job_id}")
        # A successful job is terminal and safe to replay without another
        # provider call.  This is important when a queue redelivers a message.
        if job.status == IndexJobStatus.SUCCEEDED:
            self._audit_job(job)
            return IndexReceipt(job.id, job.status, indexed_count=1)
        if self.worker_id is not None:
            from medical_rag.indexing.outbox import OutboxService

            if not OutboxService(self.session).claim(
                job.id, self.worker_id, datetime.now(UTC), self.lease_seconds
            ):
                return IndexReceipt(job.id, job.status, indexed_count=0)
            job = self.session.get(IndexJob, job.id)
            if job is None:  # pragma: no cover - the claim above proved it exists
                # An explicit raise rather than an assert: asserts vanish under
                # `python -O`, which would turn this into an AttributeError later.
                raise RuntimeError("claimed index job disappeared before processing")
            self.session.commit()

        evidence = self.session.get(EvidenceUnit, job.evidence_unit_id)
        version = self.session.get(DocumentVersion, job.document_version_id)
        if evidence is None or version is None:
            return self._fail(job, "source_not_found", ValueError("索引来源不存在"))
        document = self.session.get(Document, version.document_id)
        if document is None:
            return self._fail(job, "document_not_found", ValueError("索引文档不存在"))

        # A queued index job may race with deletion.  Never upsert a tombstone
        # back into the private index: perform physical cleanup instead and
        # complete the job with zero indexed records.  A cleanup failure stays
        # retryable and is fully observable through the same job/audit fields.
        if (
            document.status == DocumentStatus.DELETED
            or document.deleted_at is not None
            or version.status == DocumentStatus.DELETED
            or version.deleted_at is not None
            or evidence.deleted_at is not None
        ):
            job.status = IndexJobStatus.RUNNING
            job.attempts += 1
            job.started_at = datetime.now(UTC)
            job.error_code = None
            job.worker_id = None
            job.lease_expires_at = None
            self.session.commit()
            self._audit_job(job)
            try:
                self.private_index_writer.delete_document(document.id)
            except Exception as exc:
                job.status = IndexJobStatus.FAILED
                job.error_code = type(exc).__name__[:100]
                job.finished_at = datetime.now(UTC)
                job.worker_id = None
                job.lease_expires_at = None
                self.session.commit()
                self._audit_job(job)
                raise
            job.status = IndexJobStatus.SUCCEEDED
            job.finished_at = datetime.now(UTC)
            job.worker_id = None
            job.lease_expires_at = None
            job.next_retry_at = None
            self.session.commit()
            self._audit_job(job)
            return IndexReceipt(job.id, job.status, indexed_count=0, retrieval_eligible=False)

        # Review-required and deleted evidence remains durable, but is never
        # exposed as usable retrieval evidence.  The record itself is marked so
        # downstream adapters can enforce the same gate at index level.
        parse_job = self.session.scalar(
            select(ParseJob)
            .where(ParseJob.document_version_id == version.id)
            .order_by(ParseJob.created_at.desc())
        )
        if parse_job is None:
            return self._fail(job, "parse_job_not_found", ValueError("解析任务不存在"))
        # Eligibility is trusted from the PERSISTED evidence column (single
        # source of truth set by the quality gate / write-but-flag persistence),
        # AND'd with document/version readiness.  This closes the Task-5 leak
        # where index-side eligibility was derived from status and could
        # surface FAIL-reviewed evidence as retrievable.
        deleted = (
            evidence.deleted_at is not None
            or version.deleted_at is not None
            or document.deleted_at is not None
        )
        retrieval_eligible = bool(
            evidence.retrieval_eligible
            and not deleted
            and version.status == DocumentStatus.READY
            and document.status == DocumentStatus.READY
        )
        record = PrivateIndexRecord(
            id=str(evidence.id),
            text=evidence.text,
            tenant_id=evidence.tenant_id,
            patient_id=evidence.patient_id,
            document_id=document.id,
            document_version_id=version.id,
            evidence_unit_id=evidence.id,
            source_title=document.filename,
            source_version=f"v{version.version_number}",
            page_number=evidence.page_number,
            sheet_name=evidence.sheet_name,
            cell_range=evidence.cell_range,
            source_anchor=evidence.source_anchor,
            confidence=evidence.confidence,
            retrieval_eligible=retrieval_eligible,
            source_active=(
                version.status == DocumentStatus.READY
                and document.status == DocumentStatus.READY
                and version.deleted_at is None
                and document.deleted_at is None
            ),
            deleted=not retrieval_eligible and (
                evidence.deleted_at is not None
                or version.deleted_at is not None
                or document.deleted_at is not None
            ),
            embedding=None,
            embedding_model=job.embedding_model,
            index_version=job.index_version,
            parser_version=job.parser_version,
            content_hash=sha256(evidence.text.encode("utf-8")).hexdigest(),
            # Task 5 provenance mirrors carried from the persisted evidence unit.
            raw_text=evidence.raw_text,
            clean_text=evidence.clean_text,
            section_path=tuple(evidence.section_path) if evidence.section_path else None,
            parent_block_id=evidence.parent_block_id,
            chunk_index=evidence.chunk_index,
            quality_flags=tuple(evidence.quality_flags) if evidence.quality_flags else None,
            # Task 8: complete pipeline versions threaded from the IndexJob
            # (carrying the VersionBundle); fall back to persisted evidence
            # provenance when a legacy job predates the version columns.
            cleaner_version=job.cleaner_version or evidence.cleaner_version,
            chunker_version=job.chunker_version or evidence.chunker_version,
            extractor_version=job.extractor_version,
        )

        # Step 3 gating: a private record is embedded only when it is
        # retrieval-eligible AND has a source anchor.  Records that fail the
        # gate are rejected from the embedding stage yet still persisted flagged
        # (Task-6 write-but-flag invariant) — they are never searchable and are
        # not silently dropped.  A defensive downgrade here enforces the
        # "missing source anchor" rule even if the gate did not flag it.
        missing_private_anchor = record.retrieval_eligible and not record.source_anchor
        if missing_private_anchor:
            record = replace(record, retrieval_eligible=False)
        should_embed = bool(record.retrieval_eligible)

        if should_embed and self.embedding_pipeline is None and not self.allow_unembedded:
            return self._fail(job, "embedding_pipeline_required", ValueError("embedding pipeline is required"))

        job.status = IndexJobStatus.RUNNING
        job.attempts += 1
        job.started_at = datetime.now(UTC)
        job.error_code = None
        job.worker_id = self.worker_id
        self.session.commit()
        self._audit_job(job)
        try:
            embedded = False
            if self.embedding_pipeline is not None and should_embed:
                vectors = self.embedding_pipeline.embed_records([record.text], job.embedding_model)
                record = replace(
                    record,
                    embedding=vectors[0],
                    embedding_dimensions=self.embedding_pipeline.dimensions,
                    embedding_version=getattr(self.embedding_pipeline, "model_version", None),
                )
                embedded = True
                # Persist the embedding identity that actually ran back onto the
                # durable job.  ``IndexJob.embedding_dimensions`` is threaded
                # from ``VersionBundle``, which the parse side leaves empty, so
                # without this write-back the column stays NULL forever and a
                # provider-side dimension drift is undetectable from the job
                # record.  ``job.embedding_model`` is the identity actually sent
                # to the provider (NOT NULL, default "none"), so writing it back
                # here is an explicit assertion that the job records the
                # model/vector pair as one unit rather than relying on a column
                # default that would go stale if the model were ever re-resolved
                # during a retry.
                job.embedding_dimensions = self.embedding_pipeline.dimensions
                job.embedding_model = job.embedding_model
            self.private_index_writer.upsert([record])
            # Task 11 live stage observability (no-op when metrics is None).
            if embedded:
                self._record(
                    self.metrics, "record_embedding", outcome="success",
                    embedding_model=job.embedding_model, index_version=job.index_version,
                )
            self._record(
                self.metrics, "record_writer", outcome="success",
                index_version=job.index_version, parser_version=job.parser_version,
            )
        except Exception as exc:
            self._record(
                self.metrics, "record_embedding", outcome="failure",
                embedding_model=job.embedding_model, index_version=job.index_version,
            )
            self._record(
                self.metrics, "record_writer", outcome="failure",
                index_version=job.index_version, parser_version=job.parser_version,
            )
            job.status = IndexJobStatus.FAILED
            job.error_code = type(exc).__name__[:100]
            job.finished_at = datetime.now(UTC)
            job.worker_id = None
            job.lease_expires_at = None
            self.session.commit()
            self._audit_job(job)
            raise
        job.status = IndexJobStatus.SUCCEEDED
        job.finished_at = datetime.now(UTC)
        job.worker_id = None
        job.lease_expires_at = None
        job.next_retry_at = None
        self.session.commit()
        self._audit_job(job)
        # Report the post-gate eligibility (e.g. a gated record is not
        # retrievable even though its evidence row carried the flag).
        return IndexReceipt(
            job.id,
            job.status,
            indexed_count=1,
            retrieval_eligible=record.retrieval_eligible,
        )

    def _fail(self, job: IndexJob, code: str, exc: Exception) -> IndexReceipt:
        job.status = IndexJobStatus.FAILED
        job.error_code = code
        job.finished_at = datetime.now(UTC)
        job.worker_id = None
        job.lease_expires_at = None
        self.session.commit()
        self._audit_job(job)
        # Missing source metadata is permanent for this job; return a receipt
        # so workers can report it without making a provider call.
        del exc
        return IndexReceipt(job.id, job.status, indexed_count=0)


# Explicit aliases make the adapter discoverable without coupling callers to
# an implementation-specific class name.
DeterministicInMemoryPrivateIndexWriter = InMemoryPrivateIndexWriter
InMemoryPrivateIndexAdapter = InMemoryPrivateIndexWriter
InMemoryPrivateIndex = InMemoryPrivateIndexWriter


__all__ = [
    "PrivateIndexWriter",
    "InMemoryPrivateIndexWriter",
    "DeterministicInMemoryPrivateIndexWriter",
    "InMemoryPrivateIndexAdapter",
    "InMemoryPrivateIndex",
    "IndexReceipt",
    "IndexWorker",
]
