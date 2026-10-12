"""Privacy-safe database/index reconciliation and deterministic repair."""

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
    """A residual represented only by identifiers and index metadata."""

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
    """The source became ineligible between scan and repair."""


class _RebuildRequired(RuntimeError):
    """The residual cannot be repaired in place without writing a bare record.

    Raised instead of upserting an index-ready record with no embedding: the
    rebuild must go through a path that owns embeddings (the ``IndexWorker`` for
    private evidence, the ingest path for public chunks).
    """


class ReconciliationService:
    """Compare source lifecycle/version/hash metadata with index metadata.

    The scan path never reads or serializes index passage text.  Repair never
    writes an index-ready record that lacks an embedding: a stale/missing record
    is either removed and handed to the normal write path (a fresh ``IndexJob``
    that re-embeds, re-checks eligibility and anchors), or -- when its content
    hash is unchanged and only the index identity moved -- re-emitted with the
    vector it already carries.  Deleted, inactive, and review-required rows are
    always physically removed instead of being re-indexed as tombstones.
    """

    #: Expected public identity is configuration, not a constant.  These
    #: defaults mirror :class:`~medical_rag.knowledge.ingest_service.KnowledgeIngestService`
    #: so a scan compares records against the identity the ingest path really
    #: writes; a deployment with a different composition passes its values in.
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
        # Repair (delete/upsert) goes to a dedicated writer when one is
        # composed; the scan-side index object is the fallback so callers with
        # a single combined adapter behave exactly as before.  This split is
        # what the production wiring needs: ``OpenSearchStore`` can enumerate
        # metadata but has no delete seam, while ``OpenSearchIndexWriter`` can
        # delete but cannot enumerate.  Collapsing them into one attribute made
        # public residuals unrepaired by construction.
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
            "retrieval_eligible", "source_active", "deleted",
        ):
            item = getter(key, None)
            if item is not None:
                result[key] = str(item) if isinstance(item, UUID) else item
        # The read model (``SearchRecord``) keeps the public source identity inside
        # its ``citation`` and has no top-level fields for it, while the write model
        # (``IndexRecord``) has both.  Reading only the top level made the scan
        # compare ``expected["source_id"]`` / ``["source_version"]`` against missing
        # observed values and report *every* healthy public record as ``stale`` --
        # a residual that repair cannot clear either, since a public rebuild needs
        # the ingest write path.  On a live index the scan would have been
        # permanently red, which is worse than useless: it trains operators to
        # ignore it.
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
                # Taken from the configured ingest identity.  Hardcoding these
                # here classified every record written by a differently
                # configured ingest as stale (and the old repair then rewrote it
                # with the constant and no embedding).
                "parser_version": self.public_parser_version,
                "index_version": self.public_index_version,
                "retrieval_eligible": active,
                "source_active": active,
                "deleted": False,
            }
        return expected

    @staticmethod
    def _version_mismatch(
        expected: Mapping[str, object],
        observed: Mapping[str, object],
        version_keys: tuple[str, ...],
    ) -> bool:
        """Return True when a version field genuinely moved.

        Only fields the data actually carries on both sides are compared: a
        record that never stored a version (for example ``embedding_model`` on
        the public ingest path) must not be reported as a migration.
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
            # Content moved: the record's prose (and therefore its vector) is
            # stale.  Checked before versions so a real content change is never
            # mislabelled as a metadata-only migration.
            return "hash_mismatch"
        if ReconciliationService._version_mismatch(expected, observed, version_keys):
            # An index/parser upgrade is not stale content: the hash still
            # matches, so the record and its vector are current.
            return "version_migration"
        for key in (
            "visibility", "tenant_id", "patient_id", "document_id", "document_version_id",
            "evidence_unit_id", "source_id", "source_version", "retrieval_eligible",
            "source_active", "deleted",
        ):
            if key in expected and observed.get(key) != expected.get(key):
                return "stale"
        return None

    def _record_by_id(self, writer: object, record_id: str) -> object | None:
        """Read one observed record's metadata from the writer, if it exposes it."""

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
        """Return True only for a record that carries a usable vector."""

        if record is None:
            return False
        embedding = getattr(record, "embedding", None)
        if embedding is None:
            return False
        try:
            return len(embedding) > 0
        except TypeError:
            return False

    #: Version fields compared per scope.  Private records carry the full
    #: pipeline identity; public ingest records carry parser/index only.
    _PRIVATE_VERSION_KEYS = ("parser_version", "embedding_model", "index_version")
    _PUBLIC_VERSION_KEYS = ("parser_version", "index_version")

    @staticmethod
    def _action_for(reason: str, scope: str) -> str:
        if reason in {"deleted", "inactive", "orphaned", "duplicate"}:
            return "delete"
        if reason == "version_migration":
            # Private records can be handed back to the durable write path;
            # public records have no IndexJob seam, so they are re-emitted with
            # the vector they already carry.
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
        # Document-level deletion is the final compatibility fallback.  It is
        # only used when an exact-record seam is unavailable.
        method = getattr(writer, "delete_document", None)
        if callable(method):
            document_id = item.observed.get("document_id") or item.expected.get("document_id")
            if document_id:
                method(UUID(str(document_id)))
                return
        raise RuntimeError("index writer does not provide a safe delete seam")

    def _rebuild_private(self, writer: object, item: ReconciliationItem) -> None:
        """Hand a stale/missing private record back to the durable write path.

        The replacement record is *not* built here.  Writing one directly meant
        hardcoded versions and ``embedding=None``, which turned a searchable
        record into an unembedded one and bypassed the ``IndexWorker``'s
        eligibility, anchor, and embedding-dimension checks.  A fresh
        ``IndexJob`` re-enters that path instead; the observed record is removed
        so the rebuilt record replaces it rather than coexisting with it.
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
        # The 4-field idempotency identity (version, evidence, parser, model) is
        # uniquely indexed, so a job with this identity already existing is the
        # same unit of work -- requeue it rather than fighting the constraint.
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
        """Re-emit an observed record under the expected index identity.

        Only identity metadata changes; the text, content hash, and -- critically
        -- the vector the record already carries are preserved.
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
            # Never rewrite an index-ready record without a vector.
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
        # Public records have no IndexJob seam (their writer is the ingest
        # path), and this service has no embedding provider, so it cannot
        # rebuild one without violating the no-unembedded-write rule.
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
                    # A stale scan must never resurrect evidence after a
                    # concurrent tombstone/deactivation.  Remove the
                    # observed residual instead of writing a stale record.
                    self._delete(writer, item)
                    receipt = RepairReceipt(item.item_id, item.scope, "delete", True, "lifecycle_blocked")
                    self._audit(item, receipt)
                    return receipt
                except _RebuildRequired:
                    # Reported as a hard residual so an operator or the ingest
                    # path can rebuild it with a real embedding; nothing is
                    # written, so no unembedded record can ever appear.
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
