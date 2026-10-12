"""Transactional, deterministic SQLite-friendly Outbox repository.

The repository intentionally stores only identifiers and content hashes in
event payloads.  Patient text remains in the source tables and is read by the
index worker after a lease has been acquired.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from medical_rag.domain.enums import IndexJobStatus, OutboxEventStatus
from medical_rag.domain.models import IndexJob, OutboxEvent, VersionBundle


_FORBIDDEN_PAYLOAD_KEYS = {"text", "patient_text", "raw_text", "content", "body"}


@dataclass(frozen=True, slots=True)
class OutboxDispatch:
    event: OutboxEvent
    job: IndexJob


class OutboxService:
    """Persist and claim index handoff records in the caller's transaction."""

    def __init__(self, session: Session, metrics: object | None = None) -> None:
        self.session = session
        # Task 11: optional bounded observability recorder (no-op when None).
        # Deliberately untyped import to keep the seam decoupled.
        self.metrics = metrics

    @staticmethod
    def _validate_payload(payload: dict) -> None:
        if not isinstance(payload, dict):
            raise TypeError("outbox payload must be a mapping")
        def walk(value: object) -> bool:
            if isinstance(value, dict):
                if _FORBIDDEN_PAYLOAD_KEYS.intersection(value):
                    return True
                return any(walk(item) for item in value.values())
            if isinstance(value, (list, tuple)):
                return any(walk(item) for item in value)
            return False

        if walk(payload):
            raise ValueError("outbox payload may contain IDs/version/hash metadata only")

    def create_index_event(
        self,
        *,
        document_version_id: UUID,
        evidence_unit_id: UUID,
        index_version: str | None = None,
        parser_version: str | None = None,
        embedding_model: str | None = None,
        idempotency_key: str,
        payload: dict,
        event_type: str = "index.evidence",
        fail_after_event: bool = False,
        version_bundle: VersionBundle | None = None,
    ) -> OutboxDispatch:
        """Create one OutboxEvent and IndexJob atomically.

        ``VersionBundle`` carries the complete pipeline-version composition
        (parser/cleaner/chunker/extractor/embedding/index), which is persisted
        onto the IndexJob and echoed into the JSON payload.  When supplied it is
        authoritative for the version fields; the individual
        ``index_version``/``parser_version``/``embedding_model`` parameters are
        kept for backward compatibility and derived from the bundle when omitted.
        Duplicate delivery with the same identity returns the existing event and
        its IndexJob before any new rows are created (idempotency).

        No commit is issued so source writes can include these rows in their
        existing transaction.  On any error the session is rolled back, which
        removes both rows together.
        """

        bundle = version_bundle
        cleaner_version = bundle.cleaner_version if bundle is not None else None
        chunker_version = bundle.chunker_version if bundle is not None else None
        extractor_version = bundle.extractor_version if bundle is not None else None
        embedding_dimensions = (
            bundle.embedding_dimensions if bundle is not None else None
        )
        # The bundle is authoritative when present; legacy individual params
        # remain the fallback for callers that have not migrated yet.
        effective_parser = (
            bundle.parser_version if bundle is not None else parser_version
        )
        effective_model = (
            bundle.embedding_model if bundle is not None else embedding_model
        )
        effective_index = (
            bundle.index_version if bundle is not None else index_version
        )
        if not effective_parser or not effective_model or not effective_index:
            raise ValueError(
                "create_index_event requires parser_version, embedding_model, and index_version "
                "(either directly or via version_bundle)"
            )

        self._validate_payload(payload)
        existing_event = self.session.scalar(
            select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key)
        )
        if existing_event is not None:
            if bundle is not None:
                existing_job = self.session.scalar(
                    select(IndexJob)
                    .where(
                        IndexJob.document_version_id == document_version_id,
                        IndexJob.evidence_unit_id == evidence_unit_id,
                        IndexJob.parser_version == effective_parser,
                        IndexJob.cleaner_version == cleaner_version,
                        IndexJob.chunker_version == chunker_version,
                        IndexJob.extractor_version == extractor_version,
                        IndexJob.embedding_model == effective_model,
                        IndexJob.index_version == effective_index,
                    )
                    .order_by(IndexJob.created_at.desc())
                )
            else:
                existing_job = self.session.scalar(
                    select(IndexJob).where(
                        IndexJob.document_version_id == document_version_id,
                        IndexJob.evidence_unit_id == evidence_unit_id,
                        IndexJob.parser_version == effective_parser,
                        IndexJob.embedding_model == effective_model,
                    )
                )
            if existing_job is None:
                raise RuntimeError("outbox event exists without its index job")
            return OutboxDispatch(existing_event, existing_job)

        try:
            event = OutboxEvent(
                event_type=event_type,
                aggregate_id=str(document_version_id),
                payload=dict(payload),
                status=OutboxEventStatus.PENDING,
                idempotency_key=idempotency_key,
            )
            self.session.add(event)
            self.session.flush()
            if fail_after_event:
                raise ValueError("injected outbox transaction failure")
            job = IndexJob(
                document_version_id=document_version_id,
                evidence_unit_id=evidence_unit_id,
                index_version=effective_index,
                parser_version=effective_parser,
                embedding_model=effective_model,
                cleaner_version=cleaner_version,
                chunker_version=chunker_version,
                extractor_version=extractor_version,
                embedding_dimensions=embedding_dimensions,
                status=IndexJobStatus.PENDING,
            )
            self.session.add(job)
            self.session.flush()
            return OutboxDispatch(event, job)
        except Exception:
            self.session.rollback()
            raise

    # Concise alias useful to source writers.
    create_event_and_job = create_index_event

    def create_lifecycle_event(
        self,
        *,
        event_type: str,
        aggregate_id: UUID | str,
        idempotency_key: str,
        payload: dict,
    ) -> OutboxEvent:
        """Persist an identifier/hash-only lifecycle event without an IndexJob.

        Lifecycle changes (deletion, source deactivation, review transitions,
        and version replacement) are consumed by reconciliation rather than a
        single evidence indexing job.  Keeping them in the same durable
        Outbox table gives retries and audit consumers one privacy-safe seam.
        The method deliberately does not commit so callers can include the
        event in their source transaction.
        """

        if not event_type or not event_type.strip():
            raise ValueError("event_type is required")
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        self._validate_payload(payload)
        existing = self.session.scalar(
            select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
        event = OutboxEvent(
            event_type=event_type,
            aggregate_id=str(aggregate_id),
            payload=dict(payload),
            status=OutboxEventStatus.PENDING,
            idempotency_key=idempotency_key,
        )
        self.session.add(event)
        self.session.flush()
        return event

    # Alias used by lifecycle writers that do not need to know whether an
    # event also creates an IndexJob.
    create_lifecycle = create_lifecycle_event

    @staticmethod
    def _record_metric(metrics: object | None, name: str) -> None:
        """Emit one bounded counter onto the optional recorder (no-op when unset)."""
        if metrics is None:
            return
        increment = getattr(metrics, "increment", None)
        if callable(increment):
            increment(name)

    def claim(self, job_id: UUID, worker_id: str, now: datetime, lease_seconds: int = 60) -> bool:
        if not worker_id or lease_seconds <= 0:
            raise ValueError("worker_id and a positive lease_seconds are required")
        expires = now + timedelta(seconds=lease_seconds)
        eligible_status = IndexJob.status.in_(
            [
                IndexJobStatus.PENDING,
                IndexJobStatus.FAILED,
                IndexJobStatus.RETRYABLE_FAILED,
                IndexJobStatus.LEASED,
                IndexJobStatus.RUNNING,
            ]
        )
        reclaimable = or_(
            IndexJob.lease_expires_at.is_(None),
            IndexJob.lease_expires_at <= now,
            IndexJob.worker_id == worker_id,
        )
        due = or_(IndexJob.next_retry_at.is_(None), IndexJob.next_retry_at <= now)
        result = self.session.execute(
            update(IndexJob)
            .where(IndexJob.id == job_id, eligible_status, reclaimable, due)
            .values(
                worker_id=worker_id,
                lease_expires_at=expires,
                next_retry_at=None,
                status=IndexJobStatus.LEASED,
            )
        )
        self.session.flush()
        acquired = result.rowcount == 1
        self._record_metric(self.metrics, "lease_acquired" if acquired else "lease_rejected")
        return acquired

    def claim_pending(
        self,
        worker_id: str,
        now: datetime,
        *,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[IndexJob]:
        if limit <= 0:
            return []
        # The candidate SELECT must apply the same due/backoff predicate as
        # ``claim``.  Without it the oldest ``limit`` rows -- including jobs
        # parked in a backoff of up to an hour -- consume the whole batch
        # window and every younger, immediately-due job is starved, making the
        # queue look frozen instead of merely slow.
        due = or_(IndexJob.next_retry_at.is_(None), IndexJob.next_retry_at <= now)
        # Order by the effective availability time so overdue retries are
        # picked before newly created work, instead of by row age alone.
        available_at = func.coalesce(IndexJob.next_retry_at, IndexJob.created_at)
        jobs = self.session.scalars(
            select(IndexJob)
            .where(
                IndexJob.status.in_(
                    [
                        IndexJobStatus.PENDING,
                        IndexJobStatus.FAILED,
                        IndexJobStatus.RETRYABLE_FAILED,
                        # Expired leases are recoverable work.  ``claim``
                        # applies the lease-expiry predicate atomically.
                        IndexJobStatus.LEASED,
                        IndexJobStatus.RUNNING,
                    ]
                ),
                due,
            )
            .order_by(available_at, IndexJob.created_at, IndexJob.id)
            .limit(limit)
        ).all()
        claimed: list[IndexJob] = []
        for job in jobs:
            if self.claim(job.id, worker_id, now, lease_seconds):
                self.session.refresh(job)
                claimed.append(job)
        return claimed

    def publish(self, event_id: UUID) -> OutboxEvent | None:
        event = self.session.get(OutboxEvent, event_id)
        if event is None:
            return None
        if event.status != OutboxEventStatus.PUBLISHED:
            event.attempts += 1
            event.status = OutboxEventStatus.PUBLISHED
            event.next_retry_at = None
            self.session.flush()
            self._record_metric(self.metrics, "outbox_publish")
        return event

    def retry_event(self, event_id: UUID, now: datetime) -> OutboxEvent | None:
        event = self.session.get(OutboxEvent, event_id)
        if event is None:
            return None
        event.attempts += 1
        event.status = OutboxEventStatus.FAILED
        delay = min(3600, 5 * (2 ** max(0, event.attempts - 1)))
        event.next_retry_at = now + timedelta(seconds=delay)
        self.session.flush()
        if self.metrics is not None:
            record_retry = getattr(self.metrics, "record_retry", None)
            if callable(record_retry):
                record_retry()
        return event


# Repository and service are intentionally the same deterministic seam for
# now; a production adapter can wrap this class without changing callers.
OutboxRepository = OutboxService


def create_outbox_event_and_job(session: Session, **kwargs) -> OutboxDispatch:
    return OutboxService(session).create_index_event(**kwargs)


__all__ = [
    "OutboxDispatch",
    "OutboxRepository",
    "OutboxService",
    "create_outbox_event_and_job",
]
