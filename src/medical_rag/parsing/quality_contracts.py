"""Backward-compatible re-exports of the quality gate contracts.

The canonical definitions now live in :mod:`medical_rag.quality`; this module
is retained so existing imports from ``parsing.quality_contracts`` keep
working.
"""

from medical_rag.quality.contracts import QualityDecision

__all__ = ["QualityDecision"]
