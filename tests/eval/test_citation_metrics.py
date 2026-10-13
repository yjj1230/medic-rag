from __future__ import annotations

import pytest

from medical_rag.eval.citation_metrics import CitationClaim, evaluate_citation_claims


def _claim(**overrides: object) -> CitationClaim:
    data: dict[str, object] = {
        "id": "c1",
        "claim": "每天盐摄入量不超过 5 克",
        "evidence_ids": ("e1",),
        "expected_verdict": "entailed",
        "claim_kind": "numeric",
        "evidence_sha256": {"e1": "hash1"},
    }
    data.update(overrides)
    return CitationClaim(**data)


def test_entailment_requires_active_eligible_matching_hash() -> None:
    result = evaluate_citation_claims(
        [_claim()],
        {"e1": {"active": True, "retrieval_eligible": True, "content_hash": "hash1"}},
    )
    assert result.valid_citations == 1
    assert result.supported_claims == 1
    assert result.citation_validity == 1.0
    assert result.citation_accuracy == 1.0
    assert result.citation_coverage == 1.0


def test_hash_mismatch_and_missing_id_are_fail_closed() -> None:
    result = evaluate_citation_claims(
        [_claim(), _claim(id="c2", evidence_ids=("missing",), evidence_sha256={"missing": "x"}, expected_verdict="insufficient")],
        {"e1": {"active": True, "retrieval_eligible": True, "content_hash": "wrong"}},
    )
    assert result.valid_citations == 0
    assert result.supported_claims == 0
    assert result.insufficient_claims == 1
    assert result.citation_validity == 0.0


def test_contradicted_and_insufficient_are_exposed() -> None:
    claims = [
        _claim(expected_verdict="contradicted"),
        _claim(id="c2", expected_verdict="insufficient"),
    ]
    result = evaluate_citation_claims(
        claims,
        {"e1": {"active": True, "retrieval_eligible": True, "content_hash": "hash1"}},
    )
    assert result.contradicted_claims == 1
    assert result.insufficient_claims == 1
    assert result.supported_claims == 0
    assert result.citation_accuracy == 0.0


def test_duplicate_ids_and_empty_input_fail() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_citation_claims([_claim(), _claim()], {})
    with pytest.raises(ValueError, match="empty"):
        evaluate_citation_claims([], {})
