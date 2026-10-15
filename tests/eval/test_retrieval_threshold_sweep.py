from __future__ import annotations

from pathlib import Path

import pytest

from scripts.retrieval_threshold_sweep import (
    _aggregate_round_rows,
    _real_runner,
    _select_lowest_risk,
    run_threshold_sweep,
)


def test_two_dimensional_sweep_has_deterministic_ordering() -> None:
    def fake_runner(
        base_threshold: float,
        vector_only_threshold: float,
        *_args: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "score_space": "cosinesimil",
            "quality": {"recall_at_k": base_threshold, "precision_at_k": 1.0, "mrr": 1.0},
            "rejection": {"accuracy": vector_only_threshold, "n": 2},
            "safety": {"all_zero": True, "n": 3, "permission_violations": 0},
            "candidate_partitions": {"vector_only_candidates": 4, "vector_only_gated_out": 1},
            "latency": {"p50_ms": 1.0, "p95_ms": 2.0},
            "cases": [{"case_id": f"q-{base_threshold}-{vector_only_threshold}"}],
        }

    result = run_threshold_sweep(
        [0.80, 0.70],
        [0.90, 0.75, 0.80],
        Path("unused.jsonl"),
        "public",
        "private",
        1,
        runner=fake_runner,
    )

    assert [
        (row["base_threshold"], row["vector_only_threshold"])
        for row in result["rows"]
    ] == [(0.70, 0.70), (0.70, 0.75), (0.70, 0.80), (0.70, 0.90), (0.80, 0.80), (0.80, 0.90)]


def test_vector_only_threshold_cannot_be_below_base_threshold() -> None:
    with pytest.raises(ValueError, match="vector_only_threshold"):
        run_threshold_sweep(
            [0.80],
            [0.79],
            Path("unused.jsonl"),
            "public",
            "private",
            1,
            runner=lambda *_args, **_kwargs: {},
        )


def test_safety_denominators_and_candidate_partitions_are_preserved() -> None:
    def fake_runner(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "score_space": "cosinesimil",
            "quality": {"recall_at_k": 0.0},
            "rejection": {"accuracy": 1.0, "n": 2},
            "safety": {
                "n": 7,
                "permission_violations": 0,
                "deletion_residuals": 0,
                "review_leakage": 0,
                "all_zero": True,
            },
            "candidate_partitions": {
                "vector_candidates": 5,
                "vector_only_candidates": 2,
                "vector_only_gated_out": 1,
                "lexical_vector_intersection": 3,
            },
            "latency": {"rounds": 5, "p95_ms": 4.0},
            "cases": [
                {"case_id": "q1", "category": "quality", "returned_ids": []},
                {"case_id": "r1", "category": "rejection", "returned_ids": []},
            ],
        }

    row = run_threshold_sweep(
        [0.70],
        [0.70],
        Path("unused.jsonl"),
        "public",
        "private",
        5,
        runner=fake_runner,
    )["rows"][0]

    assert row["safety"]["n"] == 7
    assert row["safety"]["permission_violations"] == 0
    assert row["candidate_partitions"]["vector_only_candidates"] == 2
    assert row["case_ids"] == ["q1", "r1"]


def test_aggregate_round_rows_reports_rounds_times_cases() -> None:
    rows = [
        {
            "case_id": "q1",
            "category": "quality",
            "total_ms": 1.0,
            "hit_rate": 1.0,
            "recall": 1.0,
            "precision": 1.0,
            "mrr": 1.0,
            "ndcg": 1.0,
            "permission_violations": 0,
            "deletion_residuals": 0,
            "review_leakage": 0,
        },
        {
            "case_id": "r1",
            "category": "rejection",
            "total_ms": 2.0,
            "returned_ids": [],
            "permission_violations": 0,
            "deletion_residuals": 0,
            "review_leakage": 0,
        },
    ] * 5
    result = _aggregate_round_rows(rows, rounds=5)
    assert result["latency"]["samples"] == 10
    assert result["latency"]["rounds"] == 5
    assert len(result["case_rows"]) == 2


def test_real_runner_requires_canonical_dev_path() -> None:
    with pytest.raises(RuntimeError, match="canonical dev dataset"):
        _real_runner(
            0.70,
            0.70,
            Path("renamed-rag_cases.dev.jsonl"),
            "public",
            "private",
            1,
        )


def test_injected_runner_rejects_non_cosine_score_space() -> None:
    def fake_runner(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"score_space": "l2_inv", "cases": []}

    with pytest.raises(ValueError, match="cosinesimil"):
        run_threshold_sweep(
            [0.70],
            [0.70],
            Path("unused.jsonl"),
            "public",
            "private",
            1,
            runner=fake_runner,
        )


def test_lowest_risk_uses_false_reject_then_false_accept_before_thresholds() -> None:
    rows = [
        {
            "base_threshold": 0.76,
            "vector_only_threshold": 0.80,
            "rejection": {"false_reject_rate": 0.1, "false_accept_rate": 0.0},
            "quality": {"recall_at_k": 0.99},
            "latency": {"p95_ms": 1.0},
            "safety": {"all_zero": True},
        },
        {
            "base_threshold": 0.70,
            "vector_only_threshold": 0.72,
            "rejection": {"false_reject_rate": 0.1, "false_accept_rate": 0.0},
            "quality": {"recall_at_k": 0.80},
            "latency": {"p95_ms": 99.0},
            "safety": {"all_zero": True},
        },
        {
            "base_threshold": 0.68,
            "vector_only_threshold": 0.70,
            "rejection": {"false_reject_rate": 0.1, "false_accept_rate": 0.2},
            "quality": {"recall_at_k": 1.0},
            "latency": {"p95_ms": 0.1},
            "safety": {"all_zero": True},
        },
    ]

    selected = _select_lowest_risk(rows)

    assert (selected["base_threshold"], selected["vector_only_threshold"]) == (0.70, 0.72)


def test_run_meta_uses_measured_identity_and_dataset_hash(tmp_path: Path) -> None:
    cases_file = tmp_path / "cases.jsonl"
    cases_file.write_text("{}\n", encoding="utf-8")

    def fake_runner(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "score_space": "cosinesimil",
            "score_space_identity": {
                "expected": "cosinesimil",
                "mapping": "cosinesimil",
                "match": True,
            },
            "calibration_context": {
                "embedding_model": "measured-model",
                "public_index": "measured-public",
                "private_index": "measured-private",
                "mapping_score_space": "cosinesimil",
            },
            "safety": {"all_zero": True},
            "cases": [],
        }

    result = run_threshold_sweep(
        [0.70], [0.70], cases_file, "arg-public", "arg-private", 1, runner=fake_runner
    )

    assert result["run_meta"]["dataset_sha256"]
    assert result["run_meta"]["embedding_model"] == "measured-model"
    assert result["run_meta"]["public_index"] == "measured-public"
    assert result["run_meta"]["private_index"] == "measured-private"
    assert result["run_meta"]["mapping_score_space"] == "cosinesimil"
