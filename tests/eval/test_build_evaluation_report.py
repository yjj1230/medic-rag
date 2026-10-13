from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_evaluation_report import build_report


def _write(path: Path, value: dict[str, object]) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _assets(tmp_path: Path, dataset_hash: str = "d") -> Path:
    return _write(
        tmp_path / "assets.json",
        {
            "dataset_sha256": dataset_hash,
            "case_count": 1,
            "index_metadata": {
                "public": {"index": "public", "count": 1, "embedding_models": ["m"], "embedding_dimensions": [512]},
                "private": {"index": "private", "count": 1, "embedding_models": ["m"], "embedding_dimensions": [512]},
            },
        },
    )


def _retrieval(tmp_path: Path, dataset_hash: str = "d") -> Path:
    return _write(
        tmp_path / "retrieval.json",
        {
            "dataset_sha256": dataset_hash,
            "meta": {"case_count": 1, "public_index": "public", "private_index": "private", "embedding_model": "m", "embedding_dimensions": 512},
            "configs": [{"name": "G1", "quality": {"recall_at_k": 1.0}, "rejection": {}, "safety": {}, "latency": {}, "cases": []}],
        },
    )


def _citation(tmp_path: Path) -> Path:
    return _write(tmp_path / "citation.json", {"dataset_sha256": "c", "index_snapshot": {"public_index": "public", "private_index": "private"}, "metrics": {"citation_accuracy": 1.0}, "counts": {"claims": 1}})


def test_missing_artifact_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_report(tmp_path / "missing.json", _citation(tmp_path), _assets(tmp_path), None)


def test_dataset_hash_mismatch_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dataset hash"):
        build_report(_retrieval(tmp_path, "wrong"), _citation(tmp_path), _assets(tmp_path, "d"), None)


def test_valid_combined_run_preserves_denominators(tmp_path: Path) -> None:
    report = build_report(_retrieval(tmp_path), _citation(tmp_path), _assets(tmp_path), None)
    assert report["retrieval"]["dataset_sha256"] == "d"
    assert report["citation"]["counts"]["claims"] == 1
    assert report["identities"]["public_index"] == "public"
