from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.retrieval_failure_attribution import attribute_failures


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return path


def _retrieval(path: Path, rows: list[dict[str, object]], dataset_sha256: str) -> Path:
    path.write_text(json.dumps({"dataset_sha256": dataset_sha256, "cases": rows}, ensure_ascii=False), encoding="utf-8")
    return path


def test_attributes_failure_precedence_and_privacy(tmp_path: Path) -> None:
    cases = _write_jsonl(
        tmp_path / "cases.jsonl",
        [
            {"id": "miss", "category": "quality", "question": "secret question", "gold_evidence_ids": ["gold-1"]},
            {"id": "false-reject", "category": "quality", "gold_evidence_ids": ["gold-2"]},
            {"id": "false-accept", "category": "rejection", "gold_evidence_ids": []},
            {"id": "permission", "category": "safety", "gold_evidence_ids": [], "patient_text": "secret"},
            {"id": "infra", "category": "quality", "gold_evidence_ids": ["gold-3"]},
            {"id": "review", "category": "quality", "gold_evidence_ids": ["gold-4"]},
        ],
    )
    dataset_sha256 = hashlib.sha256(cases.read_bytes()).hexdigest()
    retrieval = _retrieval(
        tmp_path / "retrieval.json",
        [
            {"case_id": "miss", "returned_ids": ["wrong-1"], "reason": "ok", "diagnostics": {}},
            {"case_id": "false-reject", "returned_ids": [], "reason": "gate_rejected", "diagnostics": {"gate_rejected": True, "vector_candidates_gated_out": 1}},
            {"case_id": "false-accept", "returned_ids": ["bad-1"], "reason": "ok", "diagnostics": {}},
            {"case_id": "permission", "returned_ids": ["forbidden"], "reason": "ok", "diagnostics": {"permission_violations": 1}},
            {"case_id": "infra", "returned_ids": [], "reason": "vector_path_unavailable", "diagnostics": {"vector_error": True}},
            {"case_id": "review", "returned_ids": ["gold-4"], "reason": "ok", "diagnostics": {}},
        ],
        dataset_sha256,
    )

    result = attribute_failures(cases, retrieval)
    labels = {row["case_id"]: row["labels"] for row in result["per_case"]}
    assert labels == {
        "miss": ["retrieval_miss"],
        "false-reject": ["gate_false_reject"],
        "false-accept": ["gate_false_accept"],
        "permission": ["permission_or_lifecycle"],
        "infra": ["infrastructure_failure"],
        "review": ["manual_review"],
    }
    assert result["counts"]["retrieval_miss"] == 1
    assert result["unresolved_manual_review"] == ["review"]
    serialized = json.dumps(result, ensure_ascii=False)
    assert "secret question" not in serialized
    assert "patient_text" not in serialized
    assert "gold-1" not in serialized
    assert all(set(row) == {"case_id", "category", "gold_evidence_sha256", "returned_ids", "reason", "diagnostics_summary", "labels"} for row in result["per_case"])


def test_validates_dataset_hash_and_asset_audit(tmp_path: Path) -> None:
    cases = _write_jsonl(tmp_path / "cases.jsonl", [{"id": "a", "category": "quality", "gold_evidence_ids": ["e1"]}])
    retrieval = _retrieval(tmp_path / "retrieval.json", [{"case_id": "a", "returned_ids": [], "reason": "gate_rejected"}], "bad")
    with pytest.raises(ValueError, match="dataset hash mismatch"):
        attribute_failures(cases, retrieval)

    dataset_sha256 = hashlib.sha256(cases.read_bytes()).hexdigest()
    retrieval = _retrieval(tmp_path / "retrieval.json", [{"case_id": "a", "returned_ids": [], "reason": "gate_rejected"}], dataset_sha256)
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"dataset_sha256": "wrong"}), encoding="utf-8")
    with pytest.raises(ValueError, match="asset audit dataset hash mismatch"):
        attribute_failures(cases, retrieval, asset_audit=audit)


def test_accepts_json_cases_and_selects_named_config(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"id": "a", "category": "rejection", "gold_evidence_ids": []}]), encoding="utf-8")
    dataset_sha256 = hashlib.sha256(cases.read_bytes()).hexdigest()
    retrieval = tmp_path / "retrieval.json"
    retrieval.write_text(
        json.dumps(
            {
                "dataset_sha256": dataset_sha256,
                "configs": [
                    {"name": "other", "cases": [{"case_id": "a", "returned_ids": []}]},
                    {"name": "G1-hybrid-rrf60-gate", "cases": [{"case_id": "a", "returned_ids": ["bad"]}]},
                ],
            }
        ),
        encoding="utf-8",
    )
    result = attribute_failures(cases, retrieval)
    assert result["per_case"][0]["labels"] == ["gate_false_accept"]
    assert result["run_meta"]["selected_config"] == "G1-hybrid-rrf60-gate"


def test_redacts_arbitrary_reason_and_uses_row_level_diagnostics(tmp_path: Path) -> None:
    cases = _write_jsonl(
        tmp_path / "cases.jsonl",
        [
            {"id": "permission", "category": "rejection", "gold_evidence_ids": []},
            {"id": "mapping", "category": "quality", "gold_evidence_ids": ["g1"]},
        ],
    )
    dataset_sha256 = hashlib.sha256(cases.read_bytes()).hexdigest()
    retrieval = _retrieval(
        tmp_path / "retrieval.json",
        [
            {
                "case_id": "permission",
                "returned_ids": ["forbidden"],
                "reason": "patient-evidence-secret-token",
                "permission_violations": 1,
                "status": "ok",
            },
            {
                "case_id": "mapping",
                "returned_ids": [],
                "reason": "mapping_unavailable",
                "status": "degraded",
            },
        ],
        dataset_sha256,
    )
    result = attribute_failures(cases, retrieval)
    rows = {row["case_id"]: row for row in result["per_case"]}
    assert rows["permission"]["labels"] == ["permission_or_lifecycle"]
    assert rows["permission"]["reason"] == "unknown"
    assert rows["permission"]["diagnostics_summary"] == {"permission_violations": 1, "status": "ok"}
    assert rows["mapping"]["labels"] == ["infrastructure_failure"]
    serialized = json.dumps(result, ensure_ascii=False)
    assert "patient-evidence-secret-token" not in serialized


def test_validates_selected_config_hash_and_extra_rows_and_redacts_paths(tmp_path: Path) -> None:
    cases = _write_jsonl(tmp_path / "private-cases.jsonl", [{"id": "a", "category": "quality", "gold_evidence_ids": []}])
    dataset_sha256 = hashlib.sha256(cases.read_bytes()).hexdigest()
    retrieval = tmp_path / "private-retrieval.json"
    retrieval.write_text(
        json.dumps(
            {
                "dataset_sha256": dataset_sha256,
                "configs": [
                    {
                        "name": "G1-hybrid-rrf60-gate",
                        "dataset_sha256": "wrong-selected-hash",
                        "cases": [{"case_id": "a", "returned_ids": []}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="selected config dataset hash mismatch"):
        attribute_failures(cases, retrieval)

    retrieval.write_text(
        json.dumps(
            {
                "dataset_sha256": dataset_sha256,
                "configs": [
                    {
                        "name": "G1-hybrid-rrf60-gate",
                        "dataset_sha256": dataset_sha256,
                        "cases": [
                            {"case_id": "a", "returned_ids": []},
                            {"case_id": "extra", "returned_ids": []},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="retrieval cases not present"):
        attribute_failures(cases, retrieval)

    retrieval.write_text(
        json.dumps({"dataset_sha256": dataset_sha256, "cases": [{"case_id": "a", "returned_ids": []}]}),
        encoding="utf-8",
    )
    result = attribute_failures(cases, retrieval)
    assert result["run_meta"]["cases_file"] == cases.name
    assert result["run_meta"]["retrieval_result"] == retrieval.name
