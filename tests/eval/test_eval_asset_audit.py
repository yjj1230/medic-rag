from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_eval_assets import audit_eval_assets


def _write(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return path


def test_audit_detects_duplicate_ids_and_missing_evidence(tmp_path: Path) -> None:
    cases = _write(
        tmp_path / "cases.jsonl",
        [
            {"id": "a", "question": "同一个问题", "gold_evidence_ids": ["e1"], "forbidden_evidence_ids": []},
            {"id": "a", "question": "另一个问题", "gold_evidence_ids": ["missing"], "forbidden_evidence_ids": ["e2"]},
        ],
    )
    report = audit_eval_assets(
        cases,
        {
            "public": {"count": 1, "evidence_ids": ["e1"]},
            "private": {"count": 0, "evidence_ids": []},
        },
    )
    assert report["duplicate_case_ids"] == ["a"]
    assert report["missing_gold_evidence_ids"] == ["missing"]
    assert report["missing_forbidden_evidence_ids"] == ["e2"]


def test_audit_detects_dev_test_question_overlap(tmp_path: Path) -> None:
    cases = _write(
        tmp_path / "rag_cases.test.v2.1.jsonl",
        [{"id": "test-1", "question": "重复问题", "gold_evidence_ids": [], "forbidden_evidence_ids": []}],
    )
    _write(
        tmp_path / "rag_cases.dev.jsonl",
        [{"id": "dev-1", "question": "重复问题", "gold_evidence_ids": [], "forbidden_evidence_ids": []}],
    )
    report = audit_eval_assets(cases, {})
    assert report["dev_test_question_overlap"] == ["重复问题"]


def test_audit_clean_manifest(tmp_path: Path) -> None:
    cases = _write(
        tmp_path / "cases.jsonl",
        [{"id": "a", "question": "唯一问题", "gold_evidence_ids": ["e1"], "forbidden_evidence_ids": []}],
    )
    report = audit_eval_assets(cases, {"public": {"count": 1, "evidence_ids": ["e1"]}})
    assert report["duplicate_case_ids"] == []
    assert report["missing_gold_evidence_ids"] == []
    assert report["dev_test_question_overlap"] == []
