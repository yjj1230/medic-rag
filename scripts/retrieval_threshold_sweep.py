"""Scan two-level cosine gate thresholds on the dev corpus only."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

Runner = Callable[..., dict[str, object]]
_REAL_CONTEXT: tuple[Any, Any, Any, list[Any], str] | None = None
_REAL_CONTEXT_KEY: tuple[str, str, str] | None = None
_EMBEDDING_CACHE: dict[tuple[str, str], tuple[float, ...]] = {}

DEFAULT_BASE_THRESHOLDS = tuple(round(value / 100, 2) for value in range(68, 83, 2))
DEFAULT_VECTOR_ONLY_THRESHOLDS = tuple(round(value / 100, 2) for value in range(68, 91, 2))


def _false_reject_rate(cases: list[dict[str, object]]) -> float:
    answerable = [row for row in cases if row.get("category") in {"quality", "answerable"}]
    if not answerable:
        return 0.0
    return sum(1 for row in answerable if not row.get("returned_ids")) / len(answerable)


def _normalize_thresholds(values: list[float] | tuple[float, ...], name: str) -> list[float]:
    normalized = sorted({round(float(value), 10) for value in values})
    if not normalized or any(value < 0.0 or value > 1.0 for value in normalized):
        raise ValueError(f"{name} must contain values in [0, 1]")
    return normalized


def _candidate_partitions(result: dict[str, object], cases: list[dict[str, object]]) -> dict[str, object]:
    supplied = result.get("candidate_partitions")
    if isinstance(supplied, dict):
        return dict(supplied)
    keys = (
        "lexical_candidates",
        "vector_candidates",
        "vector_gated_candidates",
        "vector_candidates_gated_out",
        "vector_only_candidates",
        "vector_only_gated_out",
        "lexical_vector_intersection",
        "lexical_gated_out",
        "gate_passed_candidates",
    )
    aggregate: dict[str, int] = {}
    for case in cases:
        diagnostics = case.get("diagnostics")
        if not isinstance(diagnostics, dict):
            diagnostics = case.get("candidate_partitions")
        if not isinstance(diagnostics, dict):
            continue
        for key in keys:
            value = diagnostics.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                aggregate[key] = aggregate.get(key, 0) + int(value)
    return aggregate


def _safe_cases(cases: object) -> list[dict[str, object]]:
    if not isinstance(cases, list):
        return []
    safe: list[dict[str, object]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        safe.append(
            {
                "case_id": case.get("case_id"),
                "category": case.get("category"),
                "bucket": case.get("bucket"),
                "returned_ids": list(case.get("returned_ids", []))
                if isinstance(case.get("returned_ids", []), (list, tuple))
                else [],
                "reason": case.get("reason"),
                **(
                    {"candidate_partitions": dict(case["candidate_partitions"])}
                    if isinstance(case.get("candidate_partitions"), dict)
                    else {}
                ),
            }
        )
    return safe


def _aggregate_round_rows(rows: list[dict[str, object]], *, rounds: int) -> dict[str, object]:
    """Aggregate repeated per-case measurements while retaining one case row per ID."""
    from retrieval_tuning_lab import _aggregate_latency, _aggregate_quality, _aggregate_rejection, _aggregate_safety

    unique: dict[object, dict[str, object]] = {}
    for row in rows:
        unique.setdefault(row.get("case_id"), row)
    return {
        "quality": _aggregate_quality(rows),
        "rejection": _aggregate_rejection(rows),
        "safety": _aggregate_safety(rows),
        "latency": _aggregate_latency(
            [float(row.get("total_ms", 0.0)) for row in rows], rounds=rounds
        ),
        "case_rows": [unique[key] for key in unique],
    }


def run_threshold_sweep(
    base_thresholds: list[float] | tuple[float, ...],
    vector_only_thresholds: list[float] | tuple[float, ...],
    cases_file: Path,
    public_index: str,
    private_index: str,
    rounds: int,
    *,
    runner: Runner | None = None,
) -> dict[str, object]:
    """Run the Cartesian two-threshold sweep in deterministic order.

    The runner receives ``(base_threshold, vector_only_threshold, cases_file,
    public_index, private_index, rounds)`` and must use the production retrieval
    seams when no test runner is supplied.
    """

    bases = _normalize_thresholds(base_thresholds, "base_thresholds")
    vector_values = _normalize_thresholds(vector_only_thresholds, "vector_only_thresholds")
    if not any(vector >= base for base in bases for vector in vector_values):
        raise ValueError("vector_only_threshold must be at least base_threshold")
    # Always include the base threshold as the least restrictive vector-only value.
    vector_values = sorted(set(vector_values).union(bases))
    if isinstance(rounds, bool) or rounds < 1:
        raise ValueError("rounds must be positive")
    pairs = [
        (base, vector)
        for base in bases
        for vector in vector_values
        if vector >= base
    ]
    if not pairs:
        raise ValueError("vector_only_threshold must be at least base_threshold")
    run_one = runner or _real_runner
    rows: list[dict[str, object]] = []
    for base_threshold, vector_only_threshold in pairs:
        result = run_one(
            base_threshold,
            vector_only_threshold,
            cases_file,
            public_index,
            private_index,
            rounds,
        )
        if not isinstance(result, dict):
            raise TypeError("threshold sweep runner must return a mapping")
        score_space = result.get("score_space", "cosinesimil")
        if score_space != "cosinesimil":
            raise ValueError(
                f"threshold sweep requires cosinesimil score space; got {score_space!r}"
            )
        cases = _safe_cases(result.get("cases", []))
        rejection = dict(result.get("rejection", {})) if isinstance(result.get("rejection"), dict) else {}
        rejection["false_reject_rate"] = _false_reject_rate(cases)
        safety = dict(result.get("safety", {})) if isinstance(result.get("safety"), dict) else {}
        partitions = _candidate_partitions(result, cases)
        score_space = result.get("score_space", "cosinesimil")
        identity = result.get("score_space_identity")
        if not isinstance(identity, dict):
            identity = {"expected": "cosinesimil", "mapping": score_space, "match": score_space == "cosinesimil"}
        rows.append(
            {
                "base_threshold": base_threshold,
                "vector_only_threshold": vector_only_threshold,
                # Keep the old key as a compatibility alias for downstream readers.
                "threshold": base_threshold,
                "score_space": score_space,
                "score_space_identity": dict(identity),
                **(
                    {"calibration_context": dict(result["calibration_context"])}
                    if isinstance(result.get("calibration_context"), dict)
                    else {}
                ),
                "quality": dict(result.get("quality", {})) if isinstance(result.get("quality"), dict) else {},
                "rejection": rejection,
                "safety": safety,
                "candidate_partitions": partitions,
                "latency": dict(result.get("latency", {})) if isinstance(result.get("latency"), dict) else {},
                "case_ids": [case.get("case_id") for case in cases],
                "cases": cases,
                **(
                    {"case_rounds": _safe_cases(result["case_rounds"])}
                    if isinstance(result.get("case_rounds"), list)
                    else {}
                ),
            }
        )
    return {
        "run_meta": {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "cases_file": str(cases_file),
            "public_index": public_index,
            "private_index": private_index,
            "rounds": rounds,
            "grid": {
                "base_thresholds": bases,
                "vector_only_thresholds": vector_values,
                "valid_pairs": len(pairs),
            },
            "gate_policy": "COSINE_ABSOLUTE_TWO_LEVEL",
            "score_space": "cosinesimil",
        },
        "rows": rows,
    }


def _real_runner(
    base_threshold: float,
    vector_only_threshold: float,
    cases_file: Path,
    public_index: str,
    private_index: str,
    rounds: int,
) -> dict[str, object]:
    from medical_rag.core.config import get_settings
    from medical_rag.models.factory import resolve_embedding
    from medical_rag.retrieval.opensearch_store import OpenSearchStore
    from medical_rag.retrieval.ranking import HybridRanker
    from medical_rag.retrieval.service import RetrievalService
    from opensearchpy import OpenSearch
    from retrieval_tuning_lab import load_cases

    canonical_dev = (ROOT / "eval" / "rag_cases.dev.jsonl").resolve()
    if cases_file.resolve() != canonical_dev:
        raise RuntimeError(
            f"threshold selection requires the canonical dev dataset path: {canonical_dev}"
        )
    if not cases_file.is_file():
        raise RuntimeError(f"dev cases file is missing: {cases_file}")

    global _REAL_CONTEXT, _REAL_CONTEXT_KEY
    key = (str(cases_file.resolve()), public_index, private_index)
    if _REAL_CONTEXT is None or _REAL_CONTEXT_KEY != key:
        settings = get_settings()
        embedding = resolve_embedding(settings)
        if not embedding.enabled:
            raise RuntimeError("embedding backend unavailable; threshold sweep would be invalid")
        cases = load_cases(cases_file)
        index = OpenSearchStore(
            OpenSearch(settings.opensearch_url, timeout=10),
            public_index=public_index,
            private_index=private_index,
        )
        mapping_space = index.resolve_vector_score_space()
        if mapping_space != "cosinesimil":
            raise RuntimeError(
                f"physical index score space must be cosinesimil; resolved {mapping_space!r}"
            )
        _REAL_CONTEXT = (settings, embedding, index, cases, mapping_space)
        _REAL_CONTEXT_KEY = key
    settings, embedding, index, cases, mapping_space = _REAL_CONTEXT
    if mapping_space != "cosinesimil":
        raise RuntimeError(f"physical index score space must be cosinesimil; resolved {mapping_space!r}")

    def make_service() -> RetrievalService:
        service = RetrievalService(
            index,
            ranker=HybridRanker(rrf_k=60),
            embedding_model=embedding.model,
            embedding_model_name=embedding.model_name,
            embedding_dimensions=embedding.dimensions,
            candidate_limit=50,
            cosine_threshold=base_threshold,
            vector_only_cosine_threshold=vector_only_threshold,
            score_space=mapping_space,
        )
        original_with_embedding = service._with_query_embedding

        def cached_with_embedding(query: Any) -> tuple[Any, str | None]:
            if query.embedding is not None:
                return query, None
            cache_key = (embedding.model_name, query.text)
            cached = _EMBEDDING_CACHE.get(cache_key)
            if cached is not None:
                return replace(query, embedding=cached), None
            prepared, error = original_with_embedding(query)
            if error is None and prepared.embedding is not None:
                _EMBEDDING_CACHE[cache_key] = tuple(prepared.embedding)
            return prepared, error

        service._with_query_embedding = cached_with_embedding  # type: ignore[method-assign]
        return service

    def run_case(index_and_case: tuple[int, Any]) -> tuple[int, list[dict[str, object]]]:
        case_index, loaded = index_and_case
        service = make_service()
        captured: list[dict[str, object]] = []
        original_retrieve = service.retrieve

        def capture(query: Any, context: Any) -> Any:
            result = original_retrieve(query, context)
            captured.append(dict(result.diagnostics))
            return result

        service.retrieve = capture  # type: ignore[method-assign]
        rows: list[dict[str, object]] = []
        try:
            from retrieval_tuning_lab import evaluate_config

            for round_index in range(rounds):
                measured = evaluate_config(
                    f"cosine-{base_threshold:.3f}-{vector_only_threshold:.3f}",
                    {
                        "mode": "hybrid",
                        "rrf_k": 60,
                        "candidate_limit": 50,
                        "gate": True,
                        "top_k": 3,
                        "base_threshold": base_threshold,
                        "vector_only_threshold": vector_only_threshold,
                    },
                    service,
                    [loaded],
                    rounds=1,
                    top_k=3,
                    describe=f"real dev two-level threshold sweep round {round_index + 1}",
                )
                row = dict(measured["cases"][0])
                diagnostics = captured[-1] if captured else {}
                row["round"] = round_index + 1
                row["candidate_partitions"] = {
                    key: diagnostics.get(key)
                    for key in (
                        "lexical_candidates",
                        "vector_candidates",
                        "vector_gated_candidates",
                        "vector_candidates_gated_out",
                        "vector_only_candidates",
                        "vector_only_gated_out",
                        "lexical_vector_intersection",
                        "lexical_gated_out",
                        "gate_passed_candidates",
                    )
                    if key in diagnostics
                }
                rows.append(row)
            return case_index, rows
        finally:
            service.close()

    indexed_rows: dict[int, list[dict[str, object]]] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(cases)))) as pool:
        futures = [pool.submit(run_case, pair) for pair in enumerate(cases)]
        for future in as_completed(futures):
            case_index, rows = future.result()
            indexed_rows[case_index] = rows
    all_round_rows = [
        row
        for index in range(len(cases))
        for row in indexed_rows[index]
    ]
    aggregated = _aggregate_round_rows(all_round_rows, rounds=rounds)
    case_rows = aggregated["case_rows"]
    result = {
        "name": f"cosine-{base_threshold:.3f}-{vector_only_threshold:.3f}",
        "config": {
            "mode": "hybrid",
            "rrf_k": 60,
            "candidate_limit": 50,
            "gate": True,
            "top_k": 3,
            "base_threshold": base_threshold,
            "vector_only_threshold": vector_only_threshold,
        },
        "description": "real dev two-level threshold sweep",
        "quality": aggregated["quality"],
        "rejection": aggregated["rejection"],
        "safety": aggregated["safety"],
        "latency": aggregated["latency"],
        "cases": case_rows,
        "case_rounds": all_round_rows,
    }
    result["candidate_partitions"] = _candidate_partitions(result, all_round_rows)
    result["score_space"] = mapping_space
    result["score_space_identity"] = {
        "expected": "cosinesimil",
        "mapping": mapping_space,
        "match": mapping_space == "cosinesimil",
        "public_index": public_index,
        "private_index": private_index,
    }
    result["calibration_context"] = {
        "embedding_model": embedding.model_name,
        "public_index_version": "public-v1",
        "private_index_version": getattr(settings, "embedding_index_version", "private-v1"),
    }
    return result


def _select_lowest_risk(rows: list[dict[str, object]]) -> dict[str, object]:
    safe_rows = []
    for row in rows:
        safety = row.get("safety", {})
        if not isinstance(safety, dict):
            continue
        violations = sum(
            int(safety.get(key, 0) or 0)
            for key in ("permission_violations", "deletion_residuals", "review_leakage", "review_required_leakage")
            if isinstance(safety.get(key, 0), (int, float))
        )
        if violations == 0 and safety.get("all_zero", True) is True:
            safe_rows.append(row)
    if not safe_rows:
        raise RuntimeError("no zero-safety-violation threshold pair was measured")

    def key(row: dict[str, object]) -> tuple[float, float, float, float, float, float]:
        quality = row.get("quality", {})
        rejection = row.get("rejection", {})
        latency = row.get("latency", {})
        return (
            float(rejection.get("false_reject_rate", 1.0)) if isinstance(rejection, dict) else 1.0,
            1.0 - float(rejection.get("accuracy", 0.0)) if isinstance(rejection, dict) else 1.0,
            float(latency.get("p95_ms", float("inf"))) if isinstance(latency, dict) else float("inf"),
            -float(quality.get("recall_at_k", 0.0)) if isinstance(quality, dict) else 0.0,
            float(row.get("base_threshold", 1.0)),
            float(row.get("vector_only_threshold", 1.0)),
        )

    return min(safe_rows, key=key)


def _dataset_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_calibration(selected: dict[str, object], cases_file: Path, output: Path) -> None:
    context = selected.get("calibration_context")
    if not isinstance(context, dict):
        context = {}
    score_space = selected.get("score_space")
    if score_space != "cosinesimil":
        raise RuntimeError(f"cannot write calibration for score space {score_space!r}")
    payload = {
        "index_version": "artifact-2026-10-14",
        "embedding_model": context.get("embedding_model", ""),
        "rrf_k": 60,
        "threshold": 0.42,
        "eval_dataset_version": f"rag_cases.dev.jsonl:sha256:{_dataset_sha256(cases_file)}",
        "cosine_threshold": selected["base_threshold"],
        "vector_only_cosine_threshold": selected["vector_only_threshold"],
        "score_space": score_space,
        "public_index_version": context.get("public_index_version", "public-v1"),
        "private_index_version": context.get("private_index_version", "private-v1"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-thresholds", default=",".join(map(str, DEFAULT_BASE_THRESHOLDS)))
    parser.add_argument("--vector-only-thresholds", default=",".join(map(str, DEFAULT_VECTOR_ONLY_THRESHOLDS)))
    parser.add_argument("--cases-file", required=True)
    parser.add_argument("--public-index", required=True)
    parser.add_argument("--private-index", required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--out", default="eval_out/phase3_threshold_sweep_2d.json")
    parser.add_argument("--calibration-out", default="config/calibration/cosine-gate-2026-10-14.json")
    args = parser.parse_args(argv)
    cases_file = Path(args.cases_file)
    if not cases_file.is_absolute():
        cases_file = ROOT / cases_file
    try:
        result = run_threshold_sweep(
            [float(value.strip()) for value in args.base_thresholds.split(",") if value.strip()],
            [float(value.strip()) for value in args.vector_only_thresholds.split(",") if value.strip()],
            cases_file,
            args.public_index,
            args.private_index,
            args.rounds,
        )
        output = Path(args.out)
        if not output.is_absolute():
            output = ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        selected = _select_lowest_risk(result["rows"])
        result["selection"] = {
            "base_threshold": selected["base_threshold"],
            "vector_only_threshold": selected["vector_only_threshold"],
            "zero_safety_violations": True,
        }
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        calibration = Path(args.calibration_out)
        if not calibration.is_absolute():
            calibration = ROOT / calibration
        _write_calibration(selected, cases_file, calibration)
        print(output)
        print(calibration)
        return 0
    except Exception as exc:
        print(f"BLOCKED: real dev threshold sweep did not complete: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
