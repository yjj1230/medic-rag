"""私有索引写入器接缝与确定性测试适配器。"""

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
from medical_rag.provenance.contracts import SourceLocator
from medical_rag.retrieval.contracts import PrivateIndexRecord, PrivateIndexWriter


def _locator_from_payload(payload: object) -> SourceLocator | None:
    if not isinstance(payload, dict):
        return None
    values = dict(payload)
    if values.get("bbox") is not None:
        values["bbox"] = tuple(values["bbox"])
    values["section_path"] = tuple(values.get("section_path") or ())
    allowed = SourceLocator.__dataclass_fields__
    return SourceLocator(**{key: value for key, value in values.items() if key in allowed})


class InMemoryPrivateIndexWriter:
    """:class:`PrivateIndexWriter` 的确定性内存实现。

    记录按其公开记录 ID 为键，以便可预测地查找。第二张映射跟踪 P0 幂等键，因此
    重试同一次证据写入是空操作，而变更过的 parser/model 标识是一次新写入。
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
            locator=record.locator,
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
            # Task 5 溯源镜像经规范化转发。
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
                # 该标识已在另一个确定性记录 ID
                # 下成功；跳过可防止重试时重复索引。
                continue
            self.records[record.id] = record
            self._idempotency[key] = record.id
            self.deleted_documents.discard(record.document_id)

    def delete_document(self, document_id: UUID) -> None:
        for record_id, record in list(self.records.items()):
            if record.document_id == document_id:
                self.records.pop(record_id, None)
                # 移除指向该记录的每一个键，包括
                # 那些来自被就地取代的 parser/model 版本的键。
                for key, mapped_id in list(self._idempotency.items()):
                    if mapped_id == record_id:
                        self._idempotency.pop(key, None)
        self.deleted_documents.add(document_id)

    def delete_record(self, record_id: str | UUID) -> None:
        """为对账删除一条确定性索引记录。"""

        record_id = str(record_id)
        record = self.records.pop(record_id, None)
        if record is None:
            return
        for key, mapped_id in list(self._idempotency.items()):
            if mapped_id == record_id:
                self._idempotency.pop(key, None)

    def all_records(self) -> tuple[PrivateIndexRecord, ...]:
        """按确定性插入顺序返回记录。"""

        return tuple(self.records.values())

    def get(self, record_id: str) -> PrivateIndexRecord | None:
        return self.records.get(record_id)

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True, slots=True)
class IndexReceipt:
    """一次持久化索引作业尝试的结果。"""

    job_id: UUID
    status: IndexJobStatus
    indexed_count: int = 0
    retrieval_eligible: bool = False

    @property
    def indexed_records(self) -> int:
        """对上报批量大小的调用方有用的兼容别名。"""

        return self.indexed_count


class IndexWorker:
    """在解析已提交行之后执行持久化的私有索引作业。"""

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
        # Task 11：可选的有界可观测记录器（为 None 时为空操作）。
        self.metrics = metrics
        if self.audit is not None and self.audit_context is None:
            raise ValueError("audit_context is required when audit_service is configured")

    @staticmethod
    def _record(metrics: object | None, name: str, **kwargs: object) -> None:
        """向可选记录器发出一个指标（为 None 时为空操作）。"""
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
        # 成功的作业是终态的，可安全重放而无需再次
        # 调用提供方。当队列重新投递消息时这很重要。
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
            if job is None:  # pragma: no cover - 上述认领已证明它存在
                # 显式 raise 而非 assert：assert 会在
                # `python -O` 下消失，那会让此处稍后变成 AttributeError。
                raise RuntimeError("claimed index job disappeared before processing")
            self.session.commit()

        evidence = self.session.get(EvidenceUnit, job.evidence_unit_id)
        version = self.session.get(DocumentVersion, job.document_version_id)
        if evidence is None or version is None:
            return self._fail(job, "source_not_found", ValueError("索引来源不存在"))
        document = self.session.get(Document, version.document_id)
        if document is None:
            return self._fail(job, "document_not_found", ValueError("索引文档不存在"))

        # 排队中的索引作业可能与删除竞争。绝不把墓碑
        # upsert 回私有索引：改为执行物理清理，
        # 并以零索引记录完成该作业。清理失败保持
        # 可重试，并可通过相同的作业/审计字段完全可观测。
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

        # 需复核和已删除的证据仍然持久化，但绝不
        # 作为可用检索证据暴露。记录本身会被标记，使
        # 下游适配器能在索引层面施加相同门禁。
        parse_job = self.session.scalar(
            select(ParseJob)
            .where(ParseJob.document_version_id == version.id)
            .order_by(ParseJob.created_at.desc())
        )
        if parse_job is None:
            return self._fail(job, "parse_job_not_found", ValueError("解析任务不存在"))
        # 资格信任*持久化的*证据列（由质量门禁 /
        # write-but-flag 持久化设定的唯一真值来源），
        # 并与文档/版本就绪状态做 AND。这堵住了 Task-5 的泄漏：
        # 此前索引侧资格由状态推导，可能把
        # FAIL 复核过的证据暴露出
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
            locator=_locator_from_payload(evidence.locator),
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
            # 可检索。
            raw_text=evidence.raw_text,
            clean_text=evidence.clean_text,
            section_path=tuple(evidence.section_path) if evidence.section_path else None,
            parent_block_id=evidence.parent_block_id,
            chunk_index=evidence.chunk_index,
            quality_flags=tuple(evidence.quality_flags) if evidence.quality_flags else None,
            # Task 8：从携带 VersionBundle 的 IndexJob 贯穿而来的
            # 完整管线版本；当遗留作业早于这些版本列时
            # 回落到持久化的证据溯源。
            cleaner_version=job.cleaner_version or evidence.cleaner_version,
            chunker_version=job.chunker_version or evidence.chunker_version,
            extractor_version=job.extractor_version,
        )

        # Step 3 门禁：私有记录仅在可检索*
        # 且*具备源锚点时才向量化。未通过
        # 门禁的记录被拒绝进入向量化阶段，但仍以标记形式持久化
        # （Task-6 write-but-flag 不变量）—— 它们永不可检索，
        # 也不会被静默丢弃。此处一个防御性降级即便门禁
        # 未标记，也会强制执行"缺失源锚点"规则。
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
                # 把实际运行的向量化标识持久化回
                # 持久化作业。``IndexJob.embedding_dimensions`` 从
                # ``VersionBundle`` 贯穿而来，而解析侧让它留空，因此
                # 没有这次回写，该列会永远保持 NULL，提供方侧的
                # 维度漂移无法从作业记录中检测到。
                # ``job.embedding_model`` 是实际发送给提供方的
                # 标识（NOT NULL，默认 "none"），因此把它回写
                # 到这里是一次显式断言：作业把
                # 模型/向量对记录为一个整体，而不是依赖一个
                # 若在重试期间模型被重新解析就会变陈旧的
                # 列默认值。
                job.embedding_dimensions = self.embedding_pipeline.dimensions
                job.embedding_model = job.embedding_model
            self.private_index_writer.upsert([record])
            # Task 11 实时阶段可观测（metrics 为 None 时为空操作）。
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
        # 上报门禁后的资格（例如一条被门禁拒绝的记录
        # 即便其证据行带了标志也不可检索）。
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
        # 源元数据缺失对该作业是永久性的；返回一张回执
        # 让 worker 能上报它，而无需调用提供方。
        del exc
        return IndexReceipt(job.id, job.status, indexed_count=0)


# 显式别名让适配器可被发现，而不必把调用方
# 耦合到某个实现特有的类名。
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
