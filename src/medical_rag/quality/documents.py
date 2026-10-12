"""Read-time derived document quality status (Task 7).

Pure, side-effect-free derivation used by the documents list endpoint.  The
rules are deliberately explicit:

* ``FAILED_PARSE`` — the latest stable ParseJob failed OR the document has no
  usable (active, non-deleted) version.
* ``NEEDS_REVIEW`` — any active version is ``DocumentStatus.NEEDS_REVIEW``.
* ``READY`` — otherwise.

``failure_reason_code`` / ``failure_reason_label`` derive from the most recent
stable ParseJob's persisted ``error_code`` via the single mapping site in
:mod:`medical_rag.quality.codes`.  ``READY`` always returns ``None``.

Notes on ``retrieval_eligible``: Task 6 persists ``DocumentVersion.status`` to
``NEEDS_REVIEW`` (and its evidence ``retrieval_eligible=False``) whenever the
soft threshold gate fires, so the version-status check below is the operative
signal.  A hard FAIL sets version status ``FAILED`` and is captured by the
FAILED_PARSE branch.  No separate evidence scan is required for the pilot
exit signal.
"""

from __future__ import annotations

from dataclasses import dataclass

from medical_rag.domain.enums import DocumentStatus, ParseJobStatus
from medical_rag.domain.models import Document, DocumentVersion, ParseJob
from medical_rag.quality.codes import failure_reason_label, normalize_error_code

# Operative values surfaced as ``quality_status``.
FAILED_PARSE = "FAILED_PARSE"
NEEDS_REVIEW = "NEEDS_REVIEW"
READY = "READY"

# Statuses that represent a settled, stable ParseJob (the ones whose
# error_code we trust for the failure reason).
#
# Design note (option A): a ParseJob stuck in PENDING/RUNNING with a
# ``worker_publish_failed`` (or any non-final) error_code is intentionally NOT
# part of this set, so it does not surface in the quality column in the pilot.
# The task left publish failures out of the quality decision; surfacing them
# is out of scope for Task 7.
_FINAL_PARSE_STATUSES = frozenset(
    {
        ParseJobStatus.SUCCEEDED,
        ParseJobStatus.FAILED,
        ParseJobStatus.NEEDS_REVIEW,
    }
)


@dataclass(frozen=True, slots=True)
class DerivedQuality:
    """Derived read-time document quality signal (additive, never stored)."""

    quality_status: str  # READY | NEEDS_REVIEW | FAILED_PARSE
    failure_reason_code: str | None
    failure_reason_label: str | None


def _latest_parse_job(
    jobs: list[ParseJob],
) -> ParseJob | None:
    """Return the most recent stable ParseJob (newest finished/created first)."""
    stable = [j for j in jobs if j.status in _FINAL_PARSE_STATUSES]
    if not stable:
        return None
    return max(stable, key=lambda j: (j.finished_at or j.created_at, j.created_at))


def _active_versions(versions: list[DocumentVersion]) -> list[DocumentVersion]:
    return [v for v in versions if v.status != DocumentStatus.DELETED]


def derive_document_quality(
    document: Document,
    versions: list[DocumentVersion],
    parse_jobs: list[ParseJob],
) -> DerivedQuality:
    """Derive the read-time quality status + reason for one document."""
    del document  # kept in the signature for call clarity / future signals
    active = _active_versions(versions)
    latest_job = _latest_parse_job(parse_jobs)

    # FAILED_PARSE: no usable version, or the latest stable parse failed.
    if not active or (latest_job is not None and latest_job.status == ParseJobStatus.FAILED):
        return _with_reason(FAILED_PARSE, latest_job)

    # NEEDS_REVIEW: any active version awaiting review.
    if any(v.status == DocumentStatus.NEEDS_REVIEW for v in active):
        return _with_reason(NEEDS_REVIEW, latest_job)

    # READY.
    return DerivedQuality(READY, None, None)


def _with_reason(status: str, job: ParseJob | None) -> DerivedQuality:
    slug = normalize_error_code(job.error_code if job else None)
    return DerivedQuality(status, slug, failure_reason_label(slug))


__all__ = [
    "FAILED_PARSE",
    "NEEDS_REVIEW",
    "READY",
    "DerivedQuality",
    "derive_document_quality",
]
