from __future__ import annotations

import json
from pathlib import Path

from scripts.evaluate_citation_claims import evaluate_file


def test_runner_reports_invalid_annotations_without_dropping_them(tmp_path: Path) -> None:
    path = tmp_path / "claims.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "c1",
                "claim": "公开事实",
                "evidence_ids": ["e1", "missing"],
                "evidence_sha256": {"e1": "good", "missing": "x"},
                "expected_verdict": "entailed",
                "claim_kind": "numeric",
                "reviewer": "reviewer",
            }
        ),
        encoding="utf-8",
    )
    result = evaluate_file(
        path,
        {"e1": {"active": True, "retrieval_eligible": True, "content_hash": "wrong"}},
        reviewer="reviewer",
    )
    assert result["counts"]["claims"] == 1
    assert result["invalid_annotations"] == [
        {"claim_id": "c1", "evidence_id": "e1", "reason": "hash_mismatch"},
        {"claim_id": "c1", "evidence_id": "missing", "reason": "missing_evidence"},
    ]
    assert result["metrics"]["citation_accuracy"] == 0.0
