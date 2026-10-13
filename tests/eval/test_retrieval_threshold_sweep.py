from __future__ import annotations

from pathlib import Path

from scripts.retrieval_threshold_sweep import run_threshold_sweep


def test_threshold_sweep_orders_thresholds_and_computes_false_reject() -> None:
    def fake_runner(threshold: float, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "quality": {"recall_at_k": 1.0 - threshold / 2, "precision_at_k": 1.0, "mrr": 1.0},
            "rejection": {"accuracy": threshold, "n": 2},
            "safety": {"all_zero": True, "n": 1},
            "latency": {"p50_ms": 1.0, "p95_ms": 2.0},
            "cases": [
                {"case_id": "q1", "category": "quality", "returned_ids": [] if threshold > 0.75 else ["e1"]},
                {"case_id": "r1", "category": "rejection", "returned_ids": []},
            ],
        }

    result = run_threshold_sweep(
        [0.80, 0.70],
        Path("unused.jsonl"),
        "public",
        "private",
        1,
        runner=fake_runner,
    )
    assert [row["threshold"] for row in result["rows"]] == [0.70, 0.80]
    assert result["rows"][0]["rejection"]["false_reject_rate"] == 0.0
    assert result["rows"][1]["rejection"]["false_reject_rate"] == 1.0


def test_empty_candidate_rejection_is_preserved() -> None:
    def fake_runner(threshold: float, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "quality": {"recall_at_k": 0.0, "precision_at_k": 0.0, "mrr": 0.0},
            "rejection": {"accuracy": 1.0, "n": 1},
            "safety": {"all_zero": True, "n": 1},
            "latency": {},
            "cases": [{"case_id": "r1", "category": "rejection", "returned_ids": []}],
        }

    result = run_threshold_sweep([0.7], Path("unused.jsonl"), "public", "private", 1, runner=fake_runner)
    assert result["rows"][0]["rejection"]["accuracy"] == 1.0
