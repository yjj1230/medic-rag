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
    # Parsing completed, but one or more evidence/fact checks require manual
    # review before the document can be used as retrieval evidence.
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
    """Runtime-validated QualityDecision status (Task 6, Absorbed Req 1).

    A StrEnum (not a bare Literal) so a mistyped status cannot silently
    construct a :class:`~medical_rag.quality.contracts.QualityDecision`.
    Values match the legacy labels so ``status == "PASS"`` comparisons and
    ``status="PASS"`` construction keep working.
    """

    # The name is fixed by the legacy wire values; it is a quality verdict label,
    # not a credential.
    PASS = "PASS"  # noqa: S105
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAIL = "FAIL"


class IndexJobStatus(StrEnum):
    """Lifecycle states for durable private-index write jobs."""

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
    # DELIVERED is retained as a readable compatibility alias for consumers
    # that use delivery-oriented terminology.
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
