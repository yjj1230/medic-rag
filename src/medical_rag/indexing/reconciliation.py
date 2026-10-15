"""隐私安全的数据库/索引对账与确定性修复。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.audit.service import AuditService
from medical_rag.auth.policy import AccessContext
from medical_rag.domain.enums import DocumentStatus, IndexJobStatus
from medical_rag.domain.models import (
    Document,
    DocumentVersion,
    EvidenceUnit,
    IndexJob,
    KnowledgeChunk,
    KnowledgeSource,
)
from medical_rag.indexing.outbox import OutboxService
from medical_rag.observability.metrics import MetricsRecorder


@dataclass(frozen=True, slots=True)
class ReconciliationItem:
    """仅由标识符与索引元数据表示的残留项。"""

    item_id: str
    scope: str
    reason: str
    action: str
    observed: Mapping[str, object] = field(default_factory=dict)
    expected: Mapping[str, object] = field(default_factory=dict)
    record_id: str | None = None

    @property
    def id(self) -> str:
        return self.item_id

    @property
    def issue(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    scope: str
    scanned: int
    items: tuple[ReconciliationItem, ...]

    @property
    def residuals(self) -> int:
        return len(self.items)

    @property
    def residual_count(self) -> int:
        return self.residuals

    @property
    def stale(self) -> int:
        return sum(item.reason in {"stale", "version_mismatch", "hash_mismatch"} for item in self.items)

    @property
    def stale_count(self) -> int:
        return self.stale

    @property
    def missing(self) -> int:
        return sum(item.reason == "missing" for item in self.items)

    @property
    def missing_count(self) -> int:
        return self.missing


@dataclass(frozen=True, slots=True)
class RepairReceipt:
    item_id: str
    scope: str
    action: str
    success: bool
    outcome: str
    error_code: str | None = None

    @property
    def repaired(self) -> bool:
        return self.success

    @property
    def ok(self) -> bool:
        return self.success

    @property
    def status(self) -> str:
        return "succeeded" if self.success else "failed"


class _LifecycleChanged(RuntimeError):
    """源在扫描与修复之间变为不合格。"""


class _RebuildRequired(RuntimeError):
    """该残留项无法在不写入一条裸记录的情况下就地修复。

    这里选择抛错，而不是 upsert 一条没有向量的、可索引记录：重建必须走
    拥有向量化的路径（私有证据走 ``IndexWorker``，公开块走 ingest 路径）。
    """


class ReconciliationService:
    """把源的 lifecycle/version/hash 元数据与索引元数据做比较。

    扫描路径绝不读取或序列化索引正文。修复绝不写入缺少向量的可索引记录：
    陈旧/缺失的记录要么被移除并交给常规写入路径（一个新的 ``IndexJob``，重新
    向量化、重新检查资格与锚点），要么 —— 当其内容哈希未变、只是索引标识变了时 ——
    用其已携带的向量重新发出。已删除、未激活和需复核的行总是被物理移除，
    而不是作为墓碑被重新索引。
    """

    #: 期望的公开标识属于配置，而非常量。这些
    #: 默认值与 :class:`~medical_rag.knowledge.ingest_service.KnowledgeIngestService`
    #: 保持一致，因此扫描会把记录与 ingest 路径真正
    #: 写入的标识做比较；组合不同的部署把自己的取值传进来。
    DEFAULT_PUBLIC_INDEX_VERSION = "public-v1"
    DEFAULT_PUBLIC_PARSER_VERSION = "knowledge-parser-v1"

    def __init__(
        self,
        session: Session,
        *,
        private_index: Any | None = None,
        public_index: Any | None = None,
        private_writer: Any | None = None,
        public_writer: Any | None = None,
        audit_service: AuditService | None = None,
        audit_context: AccessContext | None = None,
        metrics: MetricsRecorder | Any | None = None,
        public_index_version: str | None = None,
        public_parser_version: str | None = None,
        outbox_service: Any | None = None,
    ) -> None:
        self.session = session
        self.private_index = private_index if private_index is not None else private_writer
        self.public_index = public_index if public_index is not None else public_writer
        # 在已组合时，修复（删除/upsert）会交给一个专用写入器；
        # 扫描侧的索引对象作为兜底，因此使用单一
        # 合并适配器的调用方行为与以前完全一致。这一拆分
        # 正是生产接线所需要的：``OpenSearchStore`` 能枚举
        # 元数据但没有删除接缝，而 ``OpenSearchIndexWriter`` 能
        # 删除却无法枚举。把它们合并为一个属性会使得
        # 公开残留项在构造上就无法修复。
        self.private_writer = (
            private_writer if private_writer is not None else self.private_index
        )
        self.public_writer = (
            public_writer if public_writer is not None else self.public_index
        )
        self.audit = audit_service
        self.audit_context = audit_context
        self.metrics = metrics
        self.public_index_version = (
            public_index_version or self.DEFAULT_PUBLIC_INDEX_VERSION
        )
        self.public_parser_version = (
            public_parser_version or self.DEFAULT_PUBLIC_PARSER_VERSION
        )
        self.outbox = outbox_service or OutboxService(session)
        if self.audit is not None and self.audit_context is None:
            raise ValueError("audit_context is required when audit_service is configured")

    @staticmethod
    def _metadata(value: object) -> dict[str, object]:
        if isinstance(value, Mapping):
            getter = value.get
        else:

            def getter(key: object, default: object = None) -> object:
                return getattr(value, key, default)
        result: dict[str, object] = {}
        for key in (
            "id", "visibility", "tenant_id", "patient_id", "document_id",
            "document_version_id", "evidence_unit_id", "source_id", "source_version",
            "content_hash", "parser_version", "embedding_model", "index_version",
            "retrieval_eligible", "source_active", "deleted", "locator",
        ):
            item = getter(key, None)
            if item is not None:
                if hasattr(item, "to_dict") and callable(item.to_dict):
                    item = item.to_dict()
                result[key] = str(item) if isinstance(item, UUID) else item
        # 读模型（``SearchRecord``）把公开源标识放在
        # 其 ``citation`` 里，没有对应的顶层字段，而写模型
        # （``IndexRecord``）两者都有。只读顶层会让扫描
        # 把 ``expected["source_id"]`` / ``["source_version"]`` 与缺失的
        # 观测值比较，并把*每一条*健康的公开记录都报成 ``stale`` ——
        # 而修复也清不掉这种残留项，因为公开重建需要
        # ingest 写入路径。在真实索引上，这种扫描会
        # 永久标红，比无用更糟：它会把运维训练成
        # 忽视它。
        citation = getter("citation", None)
        if citation is not None:
            for key, value in (
                ("source_id", getattr(citation, "source_id", None)),
                ("source_version", getattr(citation, "version", None)),
            ):
                if value is not None and key not in result:
                    result[key] = value
        return result

    @staticmethod
    def _records(writer: object | None, scope: str, limit: int) -> list[object]:
        if writer is None or limit <= 0:
            return []
        for name in ("scan", "scan_records", "iter_records", "all_records"):
            method = getattr(writer, name, None)
            if not callable(method):
                continue
            try:
                result = method(scope=scope, limit=limit)
            except TypeError:
                try:
                    result = method(limit=limit)
                except TypeError:
                    result = method()
            return list(result)[:limit]
        records = getattr(writer, "records", None)
        if isinstance(records, Mapping):
            return list(records.values())[:limit]
        if isinstance(records, (list, tuple, set)):
            return list(records)[:limit]
        return []

    def _expected_private(self, limit: int) -> dict[str, dict[str, object]]:
        rows = self.session.execute(
            select(EvidenceUnit, DocumentVersion, Document)
            .join(DocumentVersion, EvidenceUnit.document_version_id == DocumentVersion.id)
            .join(Document, DocumentVersion.document_id == Document.id)
            .where(EvidenceUnit.patient_id.is_not(None))
            .order_by(EvidenceUnit.id)
            .limit(limit)
        ).all()
        expected: dict[str, dict[str, object]] = {}
        for evidence, version, document in rows:
            job = self.session.scalar(
                select(IndexJob)
                .where(IndexJob.evidence_unit_id == evidence.id)
                .order_by(IndexJob.created_at.desc())
            )
            deleted = any(
                (
                    document.status == DocumentStatus.DELETED,
                    document.deleted_at is not None,
                    version.status == DocumentStatus.DELETED,
                    version.deleted_at is not None,
                    evidence.deleted_at is not None,
                )
            )
            eligible = (
                not deleted
                and bool(evidence.retrieval_eligible)
                and document.status == DocumentStatus.READY
                and version.status == DocumentStatus.READY
            )
            expected[str(evidence.id)] = {
                "id": str(evidence.id),
                "visibility": "private",
                "tenant_id": str(evidence.tenant_id),
                "patient_id": str(evidence.patient_id),
                "document_id": str(document.id),
                "document_version_id": str(version.id),
                "evidence_unit_id": str(evidence.id),
                "source_version": f"v{version.version_number}",
                "content_hash": sha256(evidence.text.encode("utf-8")).hexdigest(),
                "parser_version": (job.parser_version if job else None) or version.parser_version or "parser-v1",
                "embedding_model": (job.embedding_model if job else None) or "none",
                "index_version": (job.index_version if job else None) or "private-v1",
                "retrieval_eligible": eligible,
                "source_active": eligible,
                "deleted": deleted,
                "locator": evidence.locator,
            }
        return expected

    def _expected_public(self, limit: int) -> dict[str, dict[str, object]]:
        rows = self.session.execute(
            select(KnowledgeChunk, KnowledgeSource)
            .join(KnowledgeSource, KnowledgeChunk.source_id == KnowledgeSource.id)
            .order_by(KnowledgeChunk.id)
            .limit(limit)
        ).all()
        expected: dict[str, dict[str, object]] = {}
        for chunk, source in rows:
            active = bool(source.active and chunk.active)
            expected[str(chunk.id)] = {
                "id": str(chunk.id),
                "visibility": "public",
                "source_id": str(source.id),
                "source_version": source.source_version,
                "content_hash": chunk.content_hash,
                # 取自已配置的 ingest 标识。把这里硬编码
                # 会把由不同配置的 ingest 写入的每条记录
                # 都判为陈旧（而旧的修复随后会用这个常量
                # 重写它，且不带向量）。
                "parser_version": self.public_parser_version,
                "index_version": self.public_index_version,
                "retrieval_eligible": active,
                "source_active": active,
                "deleted": False,
                "locator": chunk.locator,
            }
        return expected

    @staticmethod
    def _version_mismatch(
        expected: Mapping[str, object],
        observed: Mapping[str, object],
        version_keys: tuple[str, ...],
    ) -> bool:
        """当某个版本字段确实发生变化时返回 True。

        只比较数据在两侧都真正携带的字段：一条从未存储过版本的记录
        （例如公开 ingest 路径上的 ``embedding_model``）不得被报告为一次迁移。
        """

        for key in version_keys:
            wanted = expected.get(key)
            if wanted is None:
                continue
            current = observed.get(key)
            if current is None:
                continue
            if current != wanted:
                return True
        return False

    @staticmethod
    def _reason(
        expected: Mapping[str, object],
        observed: Mapping[str, object],
        *,
        version_keys: tuple[str, ...],
    ) -> str | None:
        if not observed:
            return "missing"
        if not bool(expected.get("retrieval_eligible")):
            if bool(expected.get("deleted")) or bool(observed.get("deleted")):
                return "deleted"
            return "inactive"
        if expected.get("content_hash") is not None and (
            observed.get("content_hash") != expected.get("content_hash")
        ):
            # 内容变了：该记录的正文（因而其向量）已
            # 陈旧。在版本之前检查，这样真实的内容变更绝不会
            # 被误标为仅元数据的迁移。
            return "hash_mismatch"
        if ReconciliationService._version_mismatch(expected, observed, version_keys):
            # 索引/解析器升级不是内容陈旧：哈希仍然
            # 匹配，因此该记录及其向量都是最新的。
            return "version_migration"
        for key in (
            "visibility", "tenant_id", "patient_id", "document_id", "document_version_id",
            "evidence_unit_id", "source_id", "source_version", "retrieval_eligible",
            "source_active", "deleted",
            "locator",
        ):
            if key in expected and observed.get(key) != expected.get(key):
                return "stale"
        return None

    def _record_by_id(self, writer: object, record_id: str) -> object | None:
        """从写入器读取一条观测记录的元数据（若写入器暴露该能力）。"""

        getter = getattr(writer, "get", None)
        if callable(getter):
            try:
                return getter(record_id)
            except (KeyError, TypeError, ValueError):
                return None
        records = getattr(writer, "records", None)
        if isinstance(records, Mapping):
            return records.get(record_id)
        return None

    @staticmethod
    def _has_embedding(record: object | None) -> bool:
        """仅当记录携带可用向量时才返回 True。"""

        if record is None:
            return False
        embedding = getattr(record, "embedding", None)
        if embedding is None:
            return False
        try:
            return len(embedding) > 0
        except TypeError:
            return False

    #: 按作用域比较的版本字段。私有记录携带完整的
    #: 管线标识；公开 ingest 记录只携带 parser/index。
    _PRIVATE_VERSION_KEYS = ("parser_version", "embedding_model", "index_version")
    _PUBLIC_VERSION_KEYS = ("parser_version", "index_version")

    @staticmethod
    def _action_for(reason: str, scope: str) -> str:
        if reason in {"deleted", "inactive", "orphaned", "duplicate"}:
            return "delete"
        if reason == "version_migration":
            # 私有记录可以交回持久化写入路径；
            # 公开记录没有 IndexJob 接缝，因此会带着其
            # 已携带的向量重新发出。
            return "rebuild" if scope == "private" else "reindex"
        return "rebuild"

    def scan(self, scope: str = "all", limit: int = 100) -> ReconciliationReport:
        scope = "all" if scope is None else str(scope).lower()
        if scope not in {"all", "private", "public"}:
            raise ValueError("scope must be all, private, or public")
        if limit <= 0:
            return ReconciliationReport(scope, 0, ())
        scopes = ("private", "public") if scope == "all" else (scope,)
        items: list[ReconciliationItem] = []
        scanned = 0
        for current_scope in scopes:
            if len(items) >= limit:
                break
            writer = self.private_index if current_scope == "private" else self.public_index
            expected = self._expected_private(limit) if current_scope == "private" else self._expected_public(limit)
            version_keys = (
                self._PRIVATE_VERSION_KEYS
                if current_scope == "private"
                else self._PUBLIC_VERSION_KEYS
            )
            records = self._records(writer, current_scope, limit)
            observed_by_id: dict[str, list[dict[str, object]]] = {}
            for record in records:
                metadata = self._metadata(record)
                record_id = str(metadata.get("id", ""))
                if not record_id:
                    continue
                observed_by_id.setdefault(record_id, []).append(metadata)
            scanned += len(expected) + len(records)
            for record_id, metadata_list in observed_by_id.items():
                if record_id in expected:
                    expected_metadata = expected[record_id]
                    if len(metadata_list) > 1:
                        items.append(ReconciliationItem(record_id, current_scope, "duplicate", "delete", metadata_list[0], expected_metadata, record_id))
                    reason = self._reason(
                        expected_metadata,
                        metadata_list[0],
                        version_keys=version_keys,
                    )
                    if reason is not None:
                        items.append(ReconciliationItem(record_id, current_scope, reason, self._action_for(reason, current_scope), metadata_list[0], expected_metadata, record_id))
                else:
                    items.append(ReconciliationItem(record_id, current_scope, "orphaned", "delete", metadata_list[0], {}, record_id))
                if len(items) >= limit:
                    break
            if len(items) < limit:
                for record_id, expected_metadata in expected.items():
                    if record_id in observed_by_id:
                        continue
                    if not bool(expected_metadata.get("retrieval_eligible")):
                        continue
                    items.append(ReconciliationItem(record_id, current_scope, "missing", "rebuild", {}, expected_metadata, record_id))
                    if len(items) >= limit:
                        break
        report = ReconciliationReport(scope, scanned, tuple(items[:limit]))
        self._metric(scope=scope, scanned=scanned, residuals=report.residuals)
        return report

    def _writer_for(self, scope: str) -> object | None:
        return self.private_writer if scope == "private" else self.public_writer

    @staticmethod
    def _delete(writer: object, item: ReconciliationItem) -> None:
        for name in ("delete_record", "delete_id", "delete"):
            method = getattr(writer, name, None)
            if callable(method):
                try:
                    method(item.record_id or item.item_id)
                except TypeError:
                    method(UUID(item.record_id or item.item_id))
                return
        # 文档级删除是最后的兼容兜底。仅在
        # 精确记录接缝不可用时才使用。
        method = getattr(writer, "delete_document", None)
        if callable(method):
            document_id = item.observed.get("document_id") or item.expected.get("document_id")
            if document_id:
                method(UUID(str(document_id)))
                return
        raise RuntimeError("index writer does not provide a safe delete seam")

    def _rebuild_private(self, writer: object, item: ReconciliationItem) -> None:
        """把陈旧/缺失的私有记录交回持久化写入路径。

        替代记录*不*在这里构建。直接写一条意味着硬编码版本和
        ``embedding=None``，这会把一条可检索记录变成一条未向量化的记录，
        并绕过 ``IndexWorker`` 的资格、锚点和向量维度检查。改为让一个新的
        ``IndexJob`` 重新进入该路径；观测到的记录会被移除，这样重建的记录
        是替换它，而不是与之并存。
        """

        expected = item.expected
        evidence = self.session.get(EvidenceUnit, UUID(str(expected["evidence_unit_id"])))
        version = self.session.get(DocumentVersion, UUID(str(expected["document_version_id"])))
        document = self.session.get(Document, UUID(str(expected["document_id"])))
        if evidence is None or version is None or document is None:
            raise _LifecycleChanged("private source is missing")
        current_eligible = (
            bool(evidence.retrieval_eligible)
            and document.status == DocumentStatus.READY
            and version.status == DocumentStatus.READY
            and document.deleted_at is None
            and version.deleted_at is None
            and evidence.deleted_at is None
        )
        if not current_eligible:
            raise _LifecycleChanged("private source became ineligible")
        job = self.session.scalar(
            select(IndexJob)
            .where(IndexJob.evidence_unit_id == evidence.id)
            .order_by(IndexJob.created_at.desc())
        )
        parser_version = (job.parser_version if job else None) or version.parser_version or "parser-v1"
        embedding_model = (job.embedding_model if job else None) or "none"
        index_version = (job.index_version if job else None) or "private-v1"
        current_hash = sha256(evidence.text.encode("utf-8")).hexdigest()
        # 四字段幂等标识（version、evidence、parser、model）是
        # 唯一索引的，因此已存在同标识的作业就是
        # 同一个工作单元 —— 重新入队，而不是与约束对抗。
        requeued = self.session.scalar(
            select(IndexJob).where(
                IndexJob.document_version_id == version.id,
                IndexJob.evidence_unit_id == evidence.id,
                IndexJob.parser_version == parser_version,
                IndexJob.embedding_model == embedding_model,
            )
        )
        if requeued is not None:
            requeued.index_version = index_version
            requeued.status = IndexJobStatus.PENDING
            requeued.worker_id = None
            requeued.lease_expires_at = None
            requeued.next_retry_at = None
            requeued.finished_at = None
            requeued.error_code = None
            self.session.flush()
        else:
            self.outbox.create_index_event(
                document_version_id=version.id,
                evidence_unit_id=evidence.id,
                index_version=index_version,
                parser_version=parser_version,
                embedding_model=embedding_model,
                idempotency_key=f"reconciliation-rebuild:{evidence.id}:{current_hash}",
                payload={
                    "document_version_id": str(version.id),
                    "evidence_unit_id": str(evidence.id),
                    "content_hash": current_hash,
                    "reconciliation_reason": item.reason,
                },
            )
        self._delete(writer, item)

    @staticmethod
    def _reindex_record(record: object, expected: Mapping[str, object]) -> object:
        """在期望的索引标识下重新发出观测到的记录。

        只有标识元数据变化；文本、内容哈希以及 —— 关键地 ——
        该记录已携带的向量都保持不变。
        """

        patch: dict[str, object] = {}
        for key in ("index_version", "parser_version"):
            wanted = expected.get(key)
            if wanted is not None and getattr(record, key, None) != wanted:
                patch[key] = wanted
        if not patch:
            return record
        return replace(record, **patch) if hasattr(record, "__dataclass_fields__") else record

    def _reindex(self, writer: object, item: ReconciliationItem) -> None:
        record = self._record_by_id(writer, item.record_id or item.item_id)
        if not self._has_embedding(record):
            # 绝不在没有向量的情况下重写一条可索引记录。
            raise _RebuildRequired("observed record has no embedding to preserve")
        updated = self._reindex_record(record, item.expected)
        upsert = getattr(writer, "upsert", None)
        if not callable(upsert):
            raise RuntimeError("index writer does not provide upsert")
        upsert([updated])

    def _upsert(self, writer: object, item: ReconciliationItem) -> None:
        if item.action == "reindex":
            self._reindex(writer, item)
            return
        if item.scope == "private":
            self._rebuild_private(writer, item)
            return
        # 公开记录没有 IndexJob 接缝（其写入器是 ingest
        # 路径），而本服务没有向量化提供方，因此它无法
        # 在不违反"不得写入未向量化记录"规则的前提下重建一条。
        raise _RebuildRequired("public rebuild requires the ingest write path")

    def repair(self, item: ReconciliationItem) -> RepairReceipt:
        writer = self._writer_for(item.scope)
        if writer is None:
            receipt = RepairReceipt(item.item_id, item.scope, item.action, False, "failed", "writer_not_configured")
            self._audit(item, receipt)
            return receipt
        try:
            if item.action == "delete":
                self._delete(writer, item)
            elif item.action in {"rebuild", "reindex", "upsert"}:
                try:
                    self._upsert(writer, item)
                except _LifecycleChanged:
                    # 陈旧扫描绝不能在并发的
                    # 墓碑/停用之后把证据复活。移除
                    # 观测到的残留项，而不是写入一条陈旧记录。
                    self._delete(writer, item)
                    receipt = RepairReceipt(item.item_id, item.scope, "delete", True, "lifecycle_blocked")
                    self._audit(item, receipt)
                    return receipt
                except _RebuildRequired:
                    # 报告为硬残留项，供运维或 ingest
                    # 路径用真实向量重建它；什么都不
                    # 写入，因此未向量化的记录永远不会出现。
                    receipt = RepairReceipt(
                        item.item_id, item.scope, item.action, False,
                        "rebuild_required", "rebuild_required",
                    )
                    self._audit(item, receipt)
                    return receipt
            else:
                raise ValueError("unsupported reconciliation action")
            receipt = RepairReceipt(item.item_id, item.scope, item.action, True, "repaired")
        except Exception as exc:
            receipt = RepairReceipt(item.item_id, item.scope, item.action, False, "failed", type(exc).__name__[:100])
        self._audit(item, receipt)
        return receipt

    def _metric(self, **metadata: object) -> None:
        if self.metrics is None:
            return
        method = getattr(self.metrics, "record_reconciliation", None)
        if callable(method):
            method(**metadata)

    def _audit(self, item: ReconciliationItem, receipt: RepairReceipt) -> None:
        self._metric(item_id=item.item_id, scope=item.scope, outcome=receipt.outcome, action=item.action)
        if self.audit is None or self.audit_context is None:
            return
        self.audit.record_reconciliation(
            self.audit_context,
            scope=item.scope,
            outcome=receipt.outcome,
            item_id=item.item_id,
            reason=item.reason,
            metadata={"action": item.action, "error_code": receipt.error_code},
        )
        self.session.commit()


__all__ = [
    "ReconciliationItem",
    "ReconciliationReport",
    "RepairReceipt",
    "ReconciliationService",
]
