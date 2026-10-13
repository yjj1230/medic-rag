from __future__ import annotations

import json
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


def _retrieval(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "retrieval.json",
        {
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
        },
    )


def _citation(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "citation.json",
        {
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
            "counts": {"total": 2, "manual_review": 1},
            "unresolved_manual_review": ["q"],
            "per_case": [{"case_id": "q", "labels": ["manual_review"]}],
        },
    )


def test_missing_attribution_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="attribution"):
        build_report(_retrieval(tmp_path), _citation(tmp_path), _assets(tmp_path), _threshold(tmp_path))


def test_selected_threshold_identity_mismatch_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="threshold identity"):
        build_report(
            _retrieval(tmp_path),
            _citation(tmp_path),
            _assets(tmp_path),
            _threshold(tmp_path, base=0.75),
            failure_attribution=_attribution(tmp_path),
        )


def test_valid_combined_artifacts_preserve_labels_and_denominators(tmp_path: Path) -> None:
    report = build_report(
        _retrieval(tmp_path),
        _citation(tmp_path),
        _assets(tmp_path),
        _threshold(tmp_path),
        failure_attribution=_attribution(tmp_path),
    )
    assert report["retrieval"]["quality"]["n"] == 1
    assert report["failure_attribution"]["counts"]["total"] == 2
    assert report["failure_attribution"]["unresolved_manual_review"] == ["q"]
    assert report["identities"]["mapping_score_space"] == "cosinesimil"
