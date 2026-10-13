"""仅在 dev 集上扫描检索门禁阈值。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


Runner = Callable[..., dict[str, object]]
_REAL_CONTEXT: tuple[Any, Any, Any, list[Any]] | None = None


def _false_reject_rate(cases: list[dict[str, object]]) -> float:
    answerable = [row for row in cases if row.get("category") in {"quality", "answerable"}]
    if not answerable:
        return 0.0
    return sum(1 for row in answerable if not row.get("returned_ids")) / len(answerable)


def run_threshold_sweep(
    thresholds: list[float] | tuple[float, ...],
    cases_file: Path,
    public_index: str,
    private_index: str,
    rounds: int,
    *,
    runner: Runner | None = None,
) -> dict[str, object]:
    normalized = sorted({float(value) for value in thresholds})
    if not normalized or any(value < 0.0 or value > 1.0 for value in normalized):
        raise ValueError("thresholds must contain values in [0, 1]")
    run_one = runner or _real_runner
    rows: list[dict[str, object]] = []
    for threshold in normalized:
        result = run_one(threshold, cases_file, public_index, private_index, rounds)
        cases = list(result.get("cases", []))
        row = {
            "threshold": threshold,
            "score_space": "cosinesimil",
            "quality": result.get("quality", {}),
            "rejection": {
                **dict(result.get("rejection", {})),
                "false_reject_rate": _false_reject_rate(cases),
            },
            "safety": result.get("safety", {}),
            "latency": result.get("latency", {}),
            "cases": [
                {
                    "case_id": row.get("case_id"),
                    "category": row.get("category"),
                    "returned_ids": row.get("returned_ids", []),
                    "reason": row.get("reason"),
                }
                for row in cases
            ],
        }
        rows.append(row)
    return {
        "run_meta": {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "cases_file": str(cases_file),
            "public_index": public_index,
            "private_index": private_index,
            "rounds": rounds,
            "gate_policy": "COSINE_ABSOLUTE",
            "score_space": "cosinesimil",
        },
        "rows": rows,
    }


def _real_runner(
    threshold: float,
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

    from retrieval_tuning_lab import evaluate_config, load_cases

    global _REAL_CONTEXT
    if _REAL_CONTEXT is None:
        settings = get_settings()
        embedding = resolve_embedding(settings)
        if not embedding.enabled:
            raise RuntimeError("embedding backend unavailable; threshold sweep would be invalid")
        cases = load_cases(Path(cases_file))
        index = OpenSearchStore(
            OpenSearch(settings.opensearch_url, timeout=10),
            public_index=public_index,
            private_index=private_index,
        )
        _REAL_CONTEXT = (settings, embedding, index, cases)
    settings, embedding, index, cases = _REAL_CONTEXT
    service = RetrievalService(
        index,
        ranker=HybridRanker(rrf_k=60),
        embedding_model=embedding.model,
        embedding_model_name=embedding.model_name,
        embedding_dimensions=embedding.dimensions,
        candidate_limit=50,
        cosine_threshold=threshold,
        score_space="cosinesimil",
    )
    try:
        return evaluate_config(
            f"cosine-{threshold:.3f}",
            {"mode": "hybrid", "rrf_k": 60, "candidate_limit": 50, "gate": True, "top_k": 3},
            service,
            cases,
            rounds=rounds,
            top_k=3,
            describe="real dev threshold sweep",
        )
    finally:
        service.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thresholds", required=True, help="comma-separated values")
    parser.add_argument("--cases-file", required=True)
    parser.add_argument("--public-index", required=True)
    parser.add_argument("--private-index", required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    thresholds = [float(value.strip()) for value in args.thresholds.split(",") if value.strip()]
    result = run_threshold_sweep(
        thresholds,
        Path(args.cases_file),
        args.public_index,
        args.private_index,
        args.rounds,
    )
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
