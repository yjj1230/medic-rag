from __future__ import annotations

from pathlib import Path

import pytest

from scripts.retrieval_threshold_sweep import (
    _aggregate_round_rows,
    _real_runner,
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
