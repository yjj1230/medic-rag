from enum import StrEnum


class RecordStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"


class ConsentPurpose(StrEnum):
    REPORT_PROCESSING = "report_processing"
    EXTERNAL_MODEL = "external_model"
    HEALTH_EDUCATION = "health_education"


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    # 解析已完成，但有一项或多项证据/事实检查需要人工
    # 复核，之后该文档才能用作检索证据。
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    DELETED = "deleted"


class ParseJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class QualityStatus(StrEnum):
    """运行期校验的 QualityDecision 状态（Task 6，吸收需求 1）。

    用 StrEnum（而非裸 Literal），这样拼错的 status 无法静默构造出
    :class:`~medical_rag.quality.contracts.QualityDecision`。
    取值与旧标签一致，因此 ``status == "PASS"`` 的比较和
    ``status="PASS"`` 的构造照常可用。
    """

    # 该名称由旧有的线上取值固定；它是质量判定的标签，
    # 不是凭据。
    PASS = "PASS"  # noqa: S105
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAIL = "FAIL"


class IndexJobStatus(StrEnum):
    """持久化私有索引写入作业的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    LEASED = "leased"
    EMBEDDING = "embedding"
    WRITING = "writing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYABLE_FAILED = "retryable_failed"
    PERMANENT_FAILED = "permanent_failed"


class OutboxEventStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    # DELIVERED 作为可读的兼容别名保留，供使用
    # 面向投递术语的消费方使用。
    DELIVERED = "published"
    FAILED = "failed"


class EvidenceKind(StrEnum):
    REPORT_TEXT = "report_text"
    REPORT_TABLE = "report_table"
    KNOWLEDGE_CHUNK = "knowledge_chunk"


class AuditAction(StrEnum):
    LOGIN = "login"
    CONSENT_GRANTED = "consent_granted"
    CONSENT_REVOKED = "consent_revoked"
    DOCUMENT_ACCESSED = "document_accessed"
    EXTERNAL_MODEL_CALL = "external_model_call"
    RETRIEVAL = "retrieval"
    SAFETY_BLOCK = "safety_block"
    ANSWER_DELIVERED = "answer_delivered"
    DELETION_REQUESTED = "deletion_requested"
    DELETION_COMPLETED = "deletion_completed"
    LIFECYCLE = "lifecycle"
    RECONCILIATION = "reconciliation"


class RiskClass(StrEnum):
    ORDINARY = "ordinary"
    URGENT = "urgent"
    MEDICATION_CHANGE = "medication_change"
    DIAGNOSIS_REQUEST = "diagnosis_request"
