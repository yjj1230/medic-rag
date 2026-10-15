from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from scripts.build_evaluation_report import build_report


def _write(path: Path, value: dict[str, object]) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _assets(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "assets.json",
        {
            "dataset_sha256": "d",
            "case_count": 2,
            "index_metadata": {
                "public": {
                    "index": "public",
                    "embedding_models": ["m"],
                    "embedding_dimensions": [512],
                    "embedding_mapping": {"space_type": "cosinesimil"},
                },
                "private": {
                    "index": "private",
                    "embedding_models": ["m"],
                    "embedding_dimensions": [512],
                    "embedding_mapping": {"space_type": "cosinesimil"},
                },
            },
        },
    )


def _retrieval(tmp_path: Path, threshold_path: Path | None = None) -> Path:
    payload = {
        "dataset_sha256": "d",
        "meta": {
            "case_count": 2,
            "public_index": "public",
            "private_index": "private",
            "embedding_model": "m",
            "embedding_dimensions": 512,
            "score_space": "cosinesimil",
            "mapping_score_space": "cosinesimil",
            "record_score_space": "cosinesimil",
            "cosine_threshold": 0.76,
            "vector_only_cosine_threshold": 0.8,
        },
        "configs": [
            {
                "name": "G1-hybrid-rrf60-gate",
                "config": {
                    "cosine_threshold": 0.76,
                    "vector_only_cosine_threshold": 0.8,
                },
                "quality": {"n": 1, "recall_at_k": 1.0},
                "rejection": {"n": 1, "accuracy": 1.0},
                "safety": {"n": 0, "all_zero": True},
                "latency": {"rounds": 5, "p95_ms": 1.0},
                "cases": [
                    {"case_id": "q", "category": "quality", "returned_ids": ["e"]},
                    {"case_id": "r", "category": "rejection", "returned_ids": []},
                ],
            }
        ],
    }
    if threshold_path:
        payload["meta"]["calibration_artifact_path"] = str(threshold_path)
        payload["meta"]["calibration_artifact_sha256"] = hashlib.sha256(threshold_path.read_bytes()).hexdigest()
    return _write(
        tmp_path / "retrieval.json",
        payload,
    )


def _citation(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "citation.json",
        {
            "dataset_kind": "claims",
            "claims_dataset_sha256": "c",
            "test_dataset_sha256": "d",
            "index_snapshot": {"public_index": "public", "private_index": "private"},
            "metrics": {"citation_accuracy": 1.0},
            "counts": {"claims": 1},
        },
    )


def _threshold(tmp_path: Path, *, base: float = 0.76) -> Path:
    return _write(
        tmp_path / "threshold.json",
        {
            "run_meta": {
                "public_index": "public",
                "private_index": "private",
                "embedding_model": "m",
                "score_space": "cosinesimil",
                "cosine_threshold": base,
                "vector_only_cosine_threshold": 0.8,
            }
        },
    )


def _attribution(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "attribution.json",
        {
            "dataset_sha256": "d",
            "run_meta": {
                "public_index": "public",
                "private_index": "private",
                "embedding_model": "m",
                "score_space": "cosinesimil",
            },
            "counts": {"total": 2, "manual_review": 2},
            "unresolved_manual_review": ["q", "r"],
            "per_case": [
                {"case_id": "q", "labels": ["manual_review"]},
                {"case_id": "r", "labels": ["manual_review"]},
            ],
        },
    )


def test_missing_attribution_fails(tmp_path: Path) -> None:
    threshold = _threshold(tmp_path)
    with pytest.raises(ValueError, match="attribution"):
        build_report(_retrieval(tmp_path, threshold), _citation(tmp_path), _assets(tmp_path), threshold)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda value: value.pop("dataset_kind"), "citation dataset identity"),
        (lambda value: value.pop("claims_dataset_sha256"), "claims dataset hash"),
        (lambda value: value.pop("test_dataset_sha256"), "frozen test dataset hash"),
    ],
)
def test_missing_citation_identity_fails_closed(
    tmp_path: Path, mutation, message: str
) -> None:
    citation = json.loads(_citation(tmp_path).read_text(encoding="utf-8"))
    mutation(citation)
    citation_path = _write(tmp_path / "citation-mutated.json", citation)
    threshold = _threshold(tmp_path)

    with pytest.raises(ValueError, match=message):
        build_report(
            _retrieval(tmp_path, threshold),
            citation_path,
            _assets(tmp_path),
            threshold,
            failure_attribution=_attribution(tmp_path),
        )


def test_claims_hash_must_not_be_used_as_frozen_test_hash(tmp_path: Path) -> None:
    citation = json.loads(_citation(tmp_path).read_text(encoding="utf-8"))
    citation["test_dataset_sha256"] = citation["claims_dataset_sha256"]
    citation_path = _write(tmp_path / "citation-ambiguous.json", citation)
    threshold = _threshold(tmp_path)

    with pytest.raises(ValueError, match="citation test dataset hash mismatch"):
        build_report(
            _retrieval(tmp_path, threshold),
            citation_path,
            _assets(tmp_path),
            threshold,
            failure_attribution=_attribution(tmp_path),
        )


def test_selected_threshold_identity_mismatch_fails(tmp_path: Path) -> None:
    threshold = _threshold(tmp_path, base=0.75)
    with pytest.raises(ValueError, match="threshold identity"):
        build_report(
            _retrieval(tmp_path, threshold),
            _citation(tmp_path),
            _assets(tmp_path),
            threshold,
            failure_attribution=_attribution(tmp_path),
        )


def test_calibration_provenance_mismatch_fails(tmp_path: Path) -> None:
    threshold = _threshold(tmp_path)
    retrieval = json.loads(_retrieval(tmp_path, threshold).read_text(encoding="utf-8"))
    retrieval["meta"]["calibration_artifact_sha256"] = "mutated"
    retrieval_path = _write(tmp_path / "retrieval-mutated.json", retrieval)
    with pytest.raises(ValueError, match="calibration artifact hash"):
        build_report(
            retrieval_path,
            _citation(tmp_path),
            _assets(tmp_path),
            threshold,
            failure_attribution=_attribution(tmp_path),
        )


@pytest.mark.parametrize("field", ["calibration_artifact_path", "calibration_artifact_sha256"])
def test_missing_calibration_provenance_fails(tmp_path: Path, field: str) -> None:
    threshold = _threshold(tmp_path)
    retrieval = json.loads(_retrieval(tmp_path, threshold).read_text(encoding="utf-8"))
    retrieval["meta"].pop(field)
    retrieval_path = _write(tmp_path / f"retrieval-missing-{field}.json", retrieval)
    with pytest.raises(ValueError, match="calibration artifact identity"):
        build_report(
            retrieval_path,
            _citation(tmp_path),
            _assets(tmp_path),
            threshold,
            failure_attribution=_attribution(tmp_path),
        )


@pytest.mark.parametrize("scope", ["public", "private"])
def test_missing_mapping_identity_fails(tmp_path: Path, scope: str) -> None:
    threshold = _threshold(tmp_path)
    assets = json.loads(_assets(tmp_path).read_text(encoding="utf-8"))
    assets["index_metadata"][scope].pop("embedding_mapping")
    assets_path = _write(tmp_path / "assets-missing-mapping.json", assets)
    with pytest.raises(ValueError, match="mapping score space metadata"):
        build_report(
            _retrieval(tmp_path, threshold),
            _citation(tmp_path),
            assets_path,
            threshold,
            failure_attribution=_attribution(tmp_path),
        )


def test_valid_combined_artifacts_preserve_labels_and_denominators(tmp_path: Path) -> None:
    threshold = _threshold(tmp_path)
    report = build_report(
        _retrieval(tmp_path, threshold),
        _citation(tmp_path),
        _assets(tmp_path),
        threshold,
        failure_attribution=_attribution(tmp_path),
    )
    assert report["retrieval"]["quality"]["n"] == 1
    assert report["failure_attribution"]["counts"]["total"] == 2
    assert report["failure_attribution"]["unresolved_manual_review"] == ["q", "r"]
    assert report["identities"]["mapping_score_space"] == "cosinesimil"
    assert report["citation"]["dataset_kind"] == "claims"
    assert report["citation"]["claims_dataset_sha256"] == "c"
    assert report["citation"]["test_dataset_sha256"] == "d"
    assert report["citation"]["identity"]["claims_count"] == 1


def test_committed_artifacts_are_privacy_safe() -> None:
    paths = [
        Path("eval_out/phase3_final_retrieval/retrieval_eval_results.json"),
        Path("eval_out/phase3_failure_attribution.json"),
        Path("eval_out/phase3_final_evaluation_manifest.json"),
    ]
    forbidden_keys = {"question", "gold", "forbidden", "note", "evidence_text", "vector", "embedding"}

    def walk(value: object) -> None:
        if isinstance(value, dict):
            assert not forbidden_keys.intersection(value), f"forbidden keys: {forbidden_keys.intersection(value)}"
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for path in paths:
        payload = path.read_text(encoding="utf-8")
        assert "question" not in payload
        assert "WHO 页面如何定义成人高血压" not in payload
        walk(json.loads(payload))
