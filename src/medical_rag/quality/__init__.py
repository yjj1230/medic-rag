"""Document quality gate: policy thresholds, metrics, and decisions.

Task 7 adds the read-time error-code mapping and derived document-quality
signal as separate modules (``codes`` / ``documents``) imported directly by
the API layer.
"""

from medical_rag.domain.enums import QualityStatus
from medical_rag.quality.contracts import QualityDecision

__all__ = ["QualityDecision", "QualityStatus"]
