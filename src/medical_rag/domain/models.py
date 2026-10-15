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
    """承载一次索引操作全部版本标识的不可变值对象。

    该捆绑包是暴露在 IndexJob 上的单条记录重建标识的唯一、内聚的构造处。
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
    """让租约/重试时间戳在 SQLite 与 PostgreSQL 之间保持可比。"""

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
    # 注意：每个类体只能有一个 ``__table_args__``。第二处
    # 声明会静默替换第一处，此前这会让唯一的
    # ``(tenant_id, external_user_id)`` 索引从元数据中丢失。
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
        # Task 11（n3b）：解析→索引的交接按 document_version_id
        # 查询 ParseJob（orchestrator/IndexWorker），因此
        # 要给它建索引，而不是为每个排队中的证据作业扫描整张表。
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
    """持久化的 outbox 式私有证据索引作业。

    某个文档版本的一条证据单元的复合幂等标识由 ``idempotency_key`` 捕获
    （parser + cleaner + chunker + extractor + embedding model + index version），
    这样任何管线组合的变更都会使重建失效，而不是复用一条陈旧的索引记录。
    四字段的 ``uq_index_jobs_idempotency`` 唯一键为迁移兼容性而保留，
    并由八字段的 ``ix_index_jobs_rebuild_idempotency`` 唯一索引加固（Task 8）。
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
        # Task 8：单条记录的重建标识。设为唯一，这样数据库也会
        # 守护该复合键；NULL 的旧版本字段会被
        # SQLite/PostgreSQL 唯一索引语义视为互不相同，与
        # 历史上从未为 NULL 的 parser/embedding 默认值相匹配。
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
    # --- Task 8：从 VersionBundle 贯穿而来的完整管线版本 ---
    # 可选，因为更早的索引行早于这些列；而 VersionBundle
    # 总是为新行携带权威的（非空）取值。
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
        """返回单条记录的重建标识（Task 8 复合键）。

        任一管线阶段（parser、cleaner、chunker、extractor、embedding model 或
        index version）的变更都会产生不同的标识，因此用升级后的组件重跑
        绝不会被误认为是同一次索引尝试。
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

    # ``IndexJob.lease`` 已被移除：租约完全由
    # ``OutboxService.claim`` 负责，它在单条条件式 UPDATE 中
    # 施加相同的资格、租约到期
    # 与到期/退避谓词（因此获取在数据库层面是原子的）。
    # ORM 级别的变体完全没有
    # 生产调用方 —— 只有两个测试用它 —— 而同一套规则的第二份
    # 先读后写实现可能会与原子实现静默分歧。

    def retry(self, error_code: str, now: datetime) -> None:
        """清除所有权并安排确定性的指数退避。"""

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
    """从源写入到索引 worker 的持久化、隐私安全交接。"""

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
    # --- Task 5：扩展证据 + 医学事实溯源（增量式，可空）---
    # 原文/清洗文本分层保留（原文永不覆盖）；retrieval text 恒为 clean_text。
    raw_text: Mapped[str | None] = mapped_column(Text)
    clean_text: Mapped[str | None] = mapped_column(Text)
    section_path: Mapped[list | None] = mapped_column(JSON)
    locator: Mapped[dict | None] = mapped_column(JSON)
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
    # --- Task 5：扩展事实溯源 + 复核状态（增量式，可空）---
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
    locator: Mapped[dict | None] = mapped_column(JSON)
    url: Mapped[str | None] = mapped_column(String(1000))
    active: Mapped[bool] = mapped_column(default=False, nullable=False)

    def citation(self):
        from medical_rag.knowledge.contracts import Citation

        locator = self._locator_object()
        return Citation(
            source_id=str(self.source_id),
            title=self.source.title,
            version=self.source.source_version,
            location=self.location or (locator.display_location() if locator else self.section),
            url=self.url,
            chunk_id=str(self.id),
            locator=locator,
        )

    def _locator_object(self):
        if not self.locator:
            return None
        from medical_rag.provenance.contracts import SourceLocator

        payload = dict(self.locator)
        if payload.get("bbox") is not None:
            payload["bbox"] = tuple(payload["bbox"])
        payload["section_path"] = tuple(payload.get("section_path") or ())
        return SourceLocator(**{key: value for key, value in payload.items() if key in SourceLocator.__dataclass_fields__})


class ChatSession(TimestampMixin, Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        # 答案路径按所有者对查找会话；没有这个索引，
        # 该查找就是对本服务最热表的一次全表扫描。
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
        # 历史回放：WHERE session_id = ? ORDER BY created_at DESC LIMIT n。
        # ``id`` 作为平局裁决键，使排序是全序，索引也就能
        # 直接服务该排序，而无需对取回的行重新排序。
        Index("ix_chat_messages_session_created", "session_id", "created_at", "id"),
        # 幂等性探测：重试保护按一个 JSON 键过滤。在提取出的值上
        # 建立函数索引，可以让该查找免于全表扫描；
        # SQLAlchemy 会按方言自身的语法渲染该表达式，因此
        # PostgreSQL 得到与 SQLite 相同的带索引谓词。
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
