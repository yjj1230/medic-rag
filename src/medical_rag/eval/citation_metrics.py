"""可复现、fail-closed 的引用陈述指标。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

Verdict = Literal["entailed", "contradicted", "insufficient"]


@dataclass(frozen=True, slots=True)
class CitationClaim:
    id: str
    claim: str
    evidence_ids: tuple[str, ...]
    expected_verdict: Verdict
    claim_kind: str
    evidence_sha256: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("claim id cannot be empty")
        if not self.claim.strip():
            raise ValueError("claim text cannot be empty")
        if self.expected_verdict not in {"entailed", "contradicted", "insufficient"}:
            raise ValueError(f"invalid expected verdict: {self.expected_verdict}")
        if not self.evidence_ids:
            raise ValueError("claim must contain at least one evidence id")


@dataclass(frozen=True, slots=True)
class CitationEvaluation:
    grounded_claims: int
    valid_citations: int
    supported_claims: int
    contradicted_claims: int
    insufficient_claims: int
    citation_validity: float
    citation_accuracy: float
    citation_coverage: float


def _valid_evidence(
    evidence_id: str,
    expected_hash: str | None,
    evidence_lookup: Mapping[str, Mapping[str, object]],
) -> bool:
    evidence = evidence_lookup.get(evidence_id)
    if not evidence:
        return False
    if evidence.get("active", evidence.get("source_active", False)) is not True:
        return False
    if evidence.get("retrieval_eligible") is not True:
        return False
    stored_hash = evidence.get("content_hash")
    return bool(expected_hash and stored_hash and str(stored_hash) == str(expected_hash))


def evaluate_citation_claims(
    claims: Sequence[CitationClaim],
    evidence_lookup: Mapping[str, Mapping[str, object]],
) -> CitationEvaluation:
    if not claims:
        raise ValueError("claims input cannot be empty")
    claim_ids = [claim.id for claim in claims]
    duplicates = sorted({claim_id for claim_id in claim_ids if claim_ids.count(claim_id) > 1})
    if duplicates:
        raise ValueError(f"duplicate claim ids: {', '.join(duplicates)}")

    total_citations = sum(len(claim.evidence_ids) for claim in claims)
    valid_citations = 0
    grounded_claims = 0
    supported_claims = 0
    contradicted_claims = 0
    insufficient_claims = 0
    entailed_total = 0
    for claim in claims:
        valid_for_claim = sum(
            _valid_evidence(
                evidence_id,
                claim.evidence_sha256.get(evidence_id),
                evidence_lookup,
            )
            for evidence_id in claim.evidence_ids
        )
        valid_citations += valid_for_claim
        if valid_for_claim:
            grounded_claims += 1
        if claim.expected_verdict == "entailed":
            entailed_total += 1
            if valid_for_claim == len(claim.evidence_ids):
                supported_claims += 1
        elif claim.expected_verdict == "contradicted":
            contradicted_claims += 1
        else:
            insufficient_claims += 1

    return CitationEvaluation(
        grounded_claims=grounded_claims,
        valid_citations=valid_citations,
        supported_claims=supported_claims,
        contradicted_claims=contradicted_claims,
        insufficient_claims=insufficient_claims,
        citation_validity=valid_citations / total_citations if total_citations else 0.0,
        citation_accuracy=supported_claims / entailed_total if entailed_total else 0.0,
        citation_coverage=grounded_claims / len(claims),
    )


__all__ = ["CitationClaim", "CitationEvaluation", "evaluate_citation_claims"]
