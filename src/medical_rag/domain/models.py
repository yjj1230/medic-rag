from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, String, Text, Uuid, text
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from medical_rag.domain.enums import (
    AuditAction,
    ConsentPurpose,
    DocumentStatus,
    EvidenceKind,
    IndexJobStatus,
    OutboxEventStatus,
    ParseJobStatus,
    RecordStatus,
)


@dataclass(frozen=True, slots=True)
class VersionBundle:
    """Frozen value object carrying every version identity for one index operation.

    The bundle is the single cohesive construction site for the per-record
    rebuild identity exposed on IndexJob.
    """

    parser_version: str
    cleaner_version: str
    chunker_version: str
    extractor_version: str
    embedding_model: str
    embedding_dimensions: int | None
    index_version: str


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Keep lease/retry timestamps comparable across SQLite and PostgreSQL."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[RecordStatus] = mapped_column(
        Enum(RecordStatus, native_enum=False), default=RecordStatus.ACTIVE, nullable=False
    )


class Patient(TimestampMixin, Base):
    __tablename__ = "patients"
    # NOTE: exactly one ``__table_args__`` per class body.  A second
    # declaration silently replaces the first, which previously dropped the
    # unique ``(tenant_id, external_user_id)`` index from the metadata.
    __table_args__ = (
        Index("ix_patients_tenant_id", "tenant_id"),
        Index("uq_patients_tenant_external_user", "tenant_id", "external_user_id", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[RecordStatus] = mapped_column(
        Enum(RecordStatus, native_enum=False), default=RecordStatus.ACTIVE, nullable=False
    )


class Consent(TimestampMixin, Base):
    __tablename__ = "consents"
    __table_args__ = (
        Index("ix_consents_patient_purpose", "patient_id", "purpose"),
        Index("ix_consents_tenant_patient", "tenant_id", "patient_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    patient_id: Mapped[UUID] = mapped_column(ForeignKey("patients.id"), nullable=False)
    purpose: Mapped[ConsentPurpose] = mapped_column(
        Enum(ConsentPurpose, native_enum=False), nullable=False
    )
    policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_tenant_patient", "tenant_id", "patient_id"),
        Index("ix_documents_patient_status", "patient_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    patient_id: Mapped[UUID] = mapped_column(ForeignKey("patients.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(150), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False), default=DocumentStatus.UPLOADED, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentVersion(TimestampMixin, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        Index("uq_document_versions_document_number", "document_id", "version_number", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False), default=DocumentStatus.UPLOADED, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ParseJob(TimestampMixin, Base):
    __tablename__ = "parse_jobs"
    __table_args__ = (
        Index("ix_parse_jobs_status", "status"),
        # Task 11 (n3b): the parsing→indexing handoff queries ParseJob by
        # document_version_id (orchestrator/IndexWorker), so index it rather
        # than scanning the whole table for every queued evidence job.
        Index("ix_parse_jobs_document_version", "document_version_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False
    )
    status: Mapped[ParseJobStatus] = mapped_column(
        Enum(ParseJobStatus, native_enum=False), default=ParseJobStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexJob(TimestampMixin, Base):
    """Durable outbox-style job for private evidence indexing.

    The composite idempotency identity of one evidence unit for a document
    version is captured by ``idempotency_key`` (parser + cleaner + chunker +
    extractor + embedding model + index version), so that any pipeline-composition
    change invalidates a rebuild rather than reusing a stale index record.  The
    four-field ``uq_index_jobs_idempotency`` uniqueness key is preserved for
    migration compatibility and is reinforced by the eight-field
    ``ix_index_jobs_rebuild_idempotency`` unique index (Task 8).
    """

    __tablename__ = "index_jobs"
    __table_args__ = (
        Index("ix_index_jobs_status", "status"),
        Index("ix_index_jobs_document_version", "document_version_id"),
        Index(
            "uq_index_jobs_idempotency",
            "document_version_id",
            "evidence_unit_id",
            "parser_version",
            "embedding_model",
            unique=True,
        ),
        # Task 8: per-record rebuild identity.  Unique so the database also
        # guards the composite; NULL legacy version fields are treated as
        # distinct by SQLite/PostgreSQL unique-index semantics, matching the
        # historical parser/embedding defaults that were never NULL.
        Index(
            "ix_index_jobs_rebuild_idempotency",
            "document_version_id",
            "evidence_unit_id",
            "parser_version",
            "cleaner_version",
            "chunker_version",
            "extractor_version",
            "embedding_model",
            "index_version",
            unique=True,
        ),
        Index("ix_index_jobs_lease", "status", "lease_expires_at", "next_retry_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False
    )
    evidence_unit_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_units.id"), nullable=False
    )
    status: Mapped[IndexJobStatus] = mapped_column(
        Enum(IndexJobStatus, native_enum=False), default=IndexJobStatus.PENDING, nullable=False
    )
    index_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parser_version: Mapped[str] = mapped_column(
        String(100), nullable=False, default="parser-v1"
    )
    embedding_model: Mapped[str] = mapped_column(
        String(200), nullable=False, default="none"
    )
    # --- Task 8: complete pipeline versions threaded from VersionBundle ---
    # Optional because older index rows predate these columns; a VersionBundle
    # always carries the authoritative (non-blank) values for new rows.
    cleaner_version: Mapped[str | None] = mapped_column(String(100))
    chunker_version: Mapped[str | None] = mapped_column(String(100))
    extractor_version: Mapped[str | None] = mapped_column(String(100))
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @property
    def idempotency_key(self) -> tuple[UUID, UUID, str, str | None, str | None, str | None, str, str]:
        """Return the per-record rebuild identity (Task 8 composite).

        A change to any pipeline stage (parser, cleaner, chunker, extractor,
        embedding model, or index version) yields a different identity, so a
        re-run with an upgraded component is never mistaken for the same
        index attempt.
        """

        return (
            self.document_version_id,
            self.evidence_unit_id,
            self.parser_version,
            self.cleaner_version,
            self.chunker_version,
            self.extractor_version,
            self.embedding_model,
            self.index_version,
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    # ``IndexJob.lease`` was removed: leasing is owned exclusively by
    # ``OutboxService.claim``, which applies the same eligibility, lease-expiry,
    # and due/backoff predicates inside a single conditional UPDATE (so the
    # acquisition is atomic at the database level).  The ORM-level variant had
    # no production caller at all -- only two tests used it -- and a second,
    # read-then-write implementation of the same rules could silently diverge
    # from the atomic one.

    def retry(self, error_code: str, now: datetime) -> None:
        """Clear ownership and schedule deterministic exponential backoff."""

        if self.status in {IndexJobStatus.SUCCEEDED, IndexJobStatus.PERMANENT_FAILED}:
            return
        current = self._utc(now)
        self.attempts += 1
        self.error_code = error_code[:100]
        self.worker_id = None
        self.lease_expires_at = None
        delay_seconds = min(3600, 5 * (2 ** max(0, self.attempts - 1)))
        self.next_retry_at = current + timedelta(seconds=delay_seconds)
        self.status = IndexJobStatus.RETRYABLE_FAILED


class OutboxEvent(TimestampMixin, Base):
    """Durable, privacy-safe handoff from source writes to indexing workers."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_status_retry", "status", "next_retry_at"),
        Index("uq_outbox_events_idempotency_key", "idempotency_key", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[OutboxEventStatus] = mapped_column(
        Enum(OutboxEventStatus, native_enum=False),
        default=OutboxEventStatus.PENDING,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)


class EvidenceUnit(TimestampMixin, Base):
    __tablename__ = "evidence_units"
    __table_args__ = (
        Index("ix_evidence_units_patient", "tenant_id", "patient_id"),
        Index("ix_evidence_units_document_version", "document_version_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    patient_id: Mapped[UUID | None] = mapped_column(ForeignKey("patients.id"))
    document_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("document_versions.id"))
    kind: Mapped[EvidenceKind] = mapped_column(Enum(EvidenceKind, native_enum=False), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    sheet_name: Mapped[str | None] = mapped_column(String(200))
    cell_range: Mapped[str | None] = mapped_column(String(100))
    source_anchor: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[float | None]
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- Task 5: expanded evidence + medical-fact provenance (additive, nullable) ---
    # 原文/清洗文本分层保留（原文永不覆盖）；retrieval text 恒为 clean_text。
    raw_text: Mapped[str | None] = mapped_column(Text)
    clean_text: Mapped[str | None] = mapped_column(Text)
    section_path: Mapped[list | None] = mapped_column(JSON)
    parent_block_id: Mapped[str | None] = mapped_column(String(200))
    chunk_index: Mapped[int | None] = mapped_column(Integer)
    parser_version: Mapped[str | None] = mapped_column(String(100))
    cleaner_version: Mapped[str | None] = mapped_column(String(100))
    chunker_version: Mapped[str | None] = mapped_column(String(100))
    quality_flags: Mapped[list | None] = mapped_column(JSON)
    retrieval_eligible: Mapped[bool] = mapped_column(default=True, nullable=False)


class MedicalFact(TimestampMixin, Base):
    __tablename__ = "medical_facts"
    __table_args__ = (
        Index("ix_medical_facts_patient", "tenant_id", "patient_id"),
        Index("ix_medical_facts_document_version", "document_version_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    patient_id: Mapped[UUID] = mapped_column(ForeignKey("patients.id"), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False
    )
    evidence_unit_id: Mapped[UUID | None] = mapped_column(ForeignKey("evidence_units.id"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(100))
    reference_range: Mapped[str | None] = mapped_column(String(200))
    collection_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abnormal_flag: Mapped[str | None] = mapped_column(String(50))
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_anchor: Mapped[str | None] = mapped_column(String(500), nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- Task 5: expanded fact provenance + review state (additive, nullable) ---
    # name/value/unit/reference_range 保持既有语义；以下为其规范化/溯源镜像。
    canonical_name: Mapped[str | None] = mapped_column(String(200))
    raw_name: Mapped[str | None] = mapped_column(String(200))
    value_number: Mapped[float | None] = mapped_column(Float)
    unit_raw: Mapped[str | None] = mapped_column(String(100))
    unit_normalized: Mapped[str | None] = mapped_column(String(100))
    reference_low: Mapped[float | None] = mapped_column(Float)
    reference_high: Mapped[float | None] = mapped_column(Float)
    extractor_version: Mapped[str | None] = mapped_column(String(100))
    review_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="ACCEPTED"
    )


class KnowledgeSource(TimestampMixin, Base):
    __tablename__ = "knowledge_sources"
    __table_args__ = (
        Index("uq_knowledge_sources_version", "source_version", "title", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    institution: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_version: Mapped[str] = mapped_column(String(100), nullable=False)
    publication_date: Mapped[date | None] = mapped_column(Date)
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=False, nullable=False)


class KnowledgeChunk(TimestampMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("uq_knowledge_chunks_hash", "source_id", "content_hash", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_sources.id"), nullable=False)
    source: Mapped[KnowledgeSource] = relationship()
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(String(500))
    page_number: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(String(1000))
    active: Mapped[bool] = mapped_column(default=False, nullable=False)

    def citation(self):
        from medical_rag.knowledge.contracts import Citation

        return Citation(
            source_id=str(self.source_id),
            title=self.source.title,
            version=self.source.source_version,
            location=self.location or self.section,
            url=self.url,
        )


class ChatSession(TimestampMixin, Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        # The answer path looks sessions up by their owner-pair; without this the
        # lookup is a sequential scan on the hottest table in the service.
        Index("ix_chat_sessions_tenant_patient", "tenant_id", "patient_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    patient_id: Mapped[UUID] = mapped_column(ForeignKey("patients.id"), nullable=False)
    status: Mapped[RecordStatus] = mapped_column(
        Enum(RecordStatus, native_enum=False), default=RecordStatus.ACTIVE, nullable=False
    )


class ChatMessage(TimestampMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        # History replay: WHERE session_id = ? ORDER BY created_at DESC LIMIT n.
        # ``id`` is the tiebreaker so the ordering is total and the index can
        # serve the sort instead of re-sorting the fetched rows.
        Index("ix_chat_messages_session_created", "session_id", "created_at", "id"),
        # Idempotency probe: the retry guard filters on a JSON key.  A functional
        # index on the extracted value keeps that lookup off a sequential scan;
        # SQLAlchemy renders the expression in the dialect's own syntax, so
        # PostgreSQL gets the same indexed predicate SQLite does.
        #
        # P1-1（评审 2026-10-12）：索引升级为 UNIQUE —— orchestrator 的幂等检查
        # 是先 SELECT 后 INSERT（TOCTOU），并发同 key 请求会双双通过检查并重复
        # 落库。唯一索引是数据库层的最终防线：败者收到 IntegrityError 后回读
        # 既有记录（见 chat/orchestrator._persist_answer）。JSON 提取值为 NULL
        # 的行（无 client_request_id 的消息）互不冲突（PG/SQLite 的 NULL 不
        # 参与唯一性判定），因此普通消息不受影响。
        Index(
            "ix_chat_messages_client_request_id",
            "session_id",
            text("(metadata_json ->> 'client_request_id')"),
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("chat_sessions.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)


class AuditEvent(TimestampMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_events_patient_created", "patient_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    patient_id: Mapped[UUID | None] = mapped_column(ForeignKey("patients.id"))
    actor_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction, native_enum=False), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    payload_hash: Mapped[str | None] = mapped_column(String(128))
    payload_metadata: Mapped[dict | None] = mapped_column(JSON)
