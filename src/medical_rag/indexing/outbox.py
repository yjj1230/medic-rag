"""事务性、确定性、对 SQLite 友好的 Outbox 仓储。

该仓储有意在事件载荷中只保存标识符和内容哈希。患者文本留在源表中，
由索引 worker 在取得租约后读取。
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
    """在调用方的事务中持久化并认领索引交接记录。"""

    def __init__(self, session: Session, metrics: object | None = None) -> None:
        self.session = session
        # Task 11：可选的有界可观测记录器（为 None 时为空操作）。
        # 有意不做类型标注的导入，以保持接缝解耦。
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
        """原子地创建一条 OutboxEvent 和一个 IndexJob。

        ``VersionBundle`` 承载完整的管线版本组合
        （parser/cleaner/chunker/extractor/embedding/index），它会被持久化到
        IndexJob 上并回显进 JSON 载荷。提供时它对版本字段具有权威性；为了向后
        兼容仍保留单个的 ``index_version``/``parser_version``/``embedding_model``
        参数，省略时从捆绑包推导。以相同标识重复投递会在任何新行创建之前返回
        已存在的事件及其 IndexJob（幂等性）。

        不发起提交，这样源写入可以把这些行纳入其既有事务。一旦出错会话会回滚，
        两行会被一起移除。
        """

        bundle = version_bundle
        cleaner_version = bundle.cleaner_version if bundle is not None else None
        chunker_version = bundle.chunker_version if bundle is not None else None
        extractor_version = bundle.extractor_version if bundle is not None else None
        embedding_dimensions = (
            bundle.embedding_dimensions if bundle is not None else None
        )
        # 捆绑包存在时具有权威性；遗留的单个参数
        # 仍作为尚未迁移的调用方的兜底。
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

    # 对源写入器有用的简洁别名。
    create_event_and_job = create_index_event

    def create_lifecycle_event(
        self,
        *,
        event_type: str,
        aggregate_id: UUID | str,
        idempotency_key: str,
        payload: dict,
    ) -> OutboxEvent:
        """持久化一条仅含标识符/哈希的生命周期事件，不带 IndexJob。

        生命周期变更（删除、源停用、复核状态迁移、版本替换）由对账流程消费，
        而不是由单条证据索引作业消费。把它们留在同一张持久化 Outbox 表中，
        使重试和审计消费方共享一个隐私安全的接缝。该方法刻意不提交，以便调用方
        把事件纳入其源事务。
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

    # 供生命周期写入器使用的别名，它们无需知道某个
    # 事件是否也会创建一个 IndexJob。
    create_lifecycle = create_lifecycle_event

    @staticmethod
    def _record_metric(metrics: object | None, name: str) -> None:
        """向可选记录器发出一个有界计数器（未设置时为空操作）。"""
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
        # 候选 SELECT 必须施加与 ``claim`` 相同的到期/退避谓词。
        # 没有它，最旧的 ``limit`` 行 —— 包括那些停在一小时退避中的作业 ——
        # 会吃掉整个批次窗口，所有更年轻的、
        # 立即到期的作业都被饿死，让队列
        # 看起来像冻结了，而不只是变慢。
        due = or_(IndexJob.next_retry_at.is_(None), IndexJob.next_retry_at <= now)
        # 按有效可用时间排序，让逾期的重试先于
        # 新建的工作被拾取，而不是仅按行的年龄排序。
        available_at = func.coalesce(IndexJob.next_retry_at, IndexJob.created_at)
        jobs = self.session.scalars(
            select(IndexJob)
            .where(
                IndexJob.status.in_(
                    [
                        IndexJobStatus.PENDING,
                        IndexJobStatus.FAILED,
                        IndexJobStatus.RETRYABLE_FAILED,
                        # 过期的租约是可恢复的工作。``claim``
                        # 会原子地施加租约到期谓词。
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


# 仓储与当前的服务有意是同一个确定性接缝；
# 生产适配器可以在不改变调用方的情况下包装这个类。
OutboxRepository = OutboxService


def create_outbox_event_and_job(session: Session, **kwargs) -> OutboxDispatch:
    return OutboxService(session).create_index_event(**kwargs)


__all__ = [
    "OutboxDispatch",
    "OutboxRepository",
    "OutboxService",
    "create_outbox_event_and_job",
]
