from __future__ import annotations

import json
from pathlib import Path

from scripts.source_gap_audit import find_source_gaps


def _jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


def test_covered_question_is_not_a_gap(tmp_path: Path) -> None:
    cases = _jsonl(
        tmp_path / "cases.jsonl",
        [{"id": "q1", "question": "问题", "category": "quality", "expected_status": "answered", "gold_evidence_ids": ["e1"]}],
    )
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"configs": [{"name": "A3", "cases": [{"case_id": "q1", "returned_ids": ["e1"]}]}]}), encoding="utf-8")
    report = find_source_gaps(cases, results, {"public": {"records": [{"id": "e1", "content_hash": "h", "active": True, "retrieval_eligible": True}]}})
    assert report["confirmed_source_gaps"] == []


def test_missing_direct_support_is_gap(tmp_path: Path) -> None:
    cases = _jsonl(
        tmp_path / "cases.jsonl",
        [{"id": "q1", "question": "问题", "category": "quality", "expected_status": "answered", "gold_evidence_ids": ["missing"]}],
    )
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"configs": [{"name": "A3", "cases": [{"case_id": "q1", "returned_ids": []}]}]}), encoding="utf-8")
    report = find_source_gaps(cases, results, {"public": {"records": []}})
    assert report["confirmed_source_gaps"][0]["case_id"] == "q1"


def test_stale_hash_is_not_counted_as_direct_support(tmp_path: Path) -> None:
    cases = _jsonl(
        tmp_path / "cases.jsonl",
        [{"id": "q1", "question": "问题", "category": "quality", "expected_status": "answered", "gold_evidence_ids": ["e1"]}],
    )
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"configs": [{"name": "A3", "cases": [{"case_id": "q1", "returned_ids": []}]}]}), encoding="utf-8")
    report = find_source_gaps(cases, results, {"public": {"records": [{"id": "e1", "content_hash": "stale", "active": False, "retrieval_eligible": True}]}})
    assert report["confirmed_source_gaps"][0]["reason"] == "no_active_eligible_direct_support"
