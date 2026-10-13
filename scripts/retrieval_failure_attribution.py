"""Privacy-safe attribution of retrieval failures from frozen evaluation artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_SELECTED_CONFIG = "G1-hybrid-rrf60-gate"
_LABELS = (
    "retrieval_miss",
    "gate_false_reject",
    "gate_false_accept",
    "annotation_issue",
    "insufficient_corpus",
    "permission_or_lifecycle",
    "infrastructure_failure",
    "manual_review",
)
_REASON_TOKENS = {
    "ok",
    "empty",
    "no_results",
    "gate_rejected",
    "below_threshold",
    "insufficient_evidence",
    "vector_path_unavailable",
    "infrastructure_failure",
    "calibration_artifact_mismatch",
    "mapping_unavailable",
    "score_space_mismatch",
    "embedding_unavailable",
    "opensearch_unavailable",
    "index_unavailable",
    "vector_error",
    "degraded",
    "infrastructure",
    "error",
    "exception",
    "failed",
    "unavailable",
    "calibration_mismatch",
    "backend_error",
    "timeout",
    "permission_denied",
    "lifecycle_filtered",
    "annotation_issue",
    "insufficient_corpus",
    "unknown",
    "missing_retrieval_result",
}
_SAFE_STRING_KEYS = {
    "status",
    "gate_skipped_reason",
    "mapping_score_space",
    "vector_score_space",
    "score_space",
    "calibration_artifact_reason",
}
_SAFE_DIAGNOSTIC_KEYS = {
    "status",
    "gate_rejected",
    "gate_skipped_reason",
    "vector_error",
    "embedding_degraded",
    "score_space_match",
    "mapping_score_space",
    "vector_score_space",
    "score_space",
    "vector_candidates_gated_out",
    "vector_gated_candidates",
    "vector_only_candidates",
    "vector_only_gated_out",
    "lexical_vector_intersection",
    "lexical_gated_out",
    "permission_violations",
    "permission_violation",
    "deletion_residuals",
    "deletion_residual",
    "review_leakage",
    "lifecycle_violations",
    "lifecycle_violation",
    "forbidden_returned",
    "acl_denied",
    "candidate_count",
    "returned_count",
    "threshold",
    "vector_only_gate_threshold",
    "vector_only_threshold_fallback",
    "calibration_artifact_mismatch",
    "calibration_mismatch_count",
    "index_identity_match",
    "calibration_artifact_reason",
}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,80}$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no} must be a JSON object")
            rows.append(value)
        return rows
    value = json.loads(path.read_text(encoding="utf-8"))
    return value


def _rows(value: Any, *, kind: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        keys = ("cases", "per_case", "rows", "items")
        found = next((value[key] for key in keys if isinstance(value.get(key), list)), None)
        if found is None:
            raise ValueError(f"{kind} artifact does not contain a list of cases")
        rows = found
    else:
        raise ValueError(f"{kind} artifact must be a JSON object, array, or JSONL")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{kind} cases must be JSON objects")
    return [dict(row) for row in rows]


def _case_id(row: Mapping[str, Any], *, result: bool = False) -> str:
    keys = ("case_id", "id", "event_id") if result else ("id", "case_id", "event_id")
    for key in keys:
        raw = row.get(key)
        if raw is not None and str(raw).strip():
            return str(raw)
    raise ValueError("case is missing an ID")


def _gold_ids(row: Mapping[str, Any]) -> list[str]:
    for key in ("gold_evidence_ids", "gold", "gold_ids"):
        raw = row.get(key)
        if raw is None:
            continue
        if not isinstance(raw, (list, tuple, set)):
            raise ValueError(f"{key} must be a list")
        return [str(item) for item in raw if str(item).strip()]
    return []


def _gold_hash(ids: list[str]) -> str:
    canonical = json.dumps(sorted(set(ids)), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_rows(value: Any) -> tuple[list[dict[str, Any]], str | None, Mapping[str, Any] | None]:
    if not isinstance(value, dict):
        return _rows(value, kind="retrieval"), None, None
    configs = value.get("configs")
    selected_name: str | None = None
    rows_value: Any = value
    if isinstance(configs, list) and configs:
        selected = next((item for item in configs if isinstance(item, dict) and item.get("name") == _SELECTED_CONFIG), configs[0])
        if not isinstance(selected, dict):
            raise ValueError("retrieval config must be an object")
        selected_name = str(selected.get("name", "")) or None
        rows_value = selected
        return _rows(rows_value, kind="retrieval"), selected_name, selected
    return _rows(rows_value, kind="retrieval"), selected_name, value


def _artifact_hash(value: Mapping[str, Any]) -> str | None:
    for key in ("dataset_sha256",):
        raw = value.get(key)
        if raw:
            return str(raw)
    for key in ("meta", "run_meta"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and nested.get("dataset_sha256"):
            return str(nested["dataset_sha256"])
    return None


def _safe_reason(raw: Any) -> str:
    value = str(raw or "").strip()
    if value in _REASON_TOKENS:
        return value
    return "unknown"


def _safe_diagnostics(raw: Any) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        return {}
    safe: dict[str, object] = {}
    for key, value in raw.items():
        name = str(key)
        if name not in _SAFE_DIAGNOSTIC_KEYS:
            continue
        if isinstance(value, bool):
            safe[name] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            safe[name] = value
        elif isinstance(value, str) and name in _SAFE_STRING_KEYS and _TOKEN_RE.fullmatch(value):
            safe[name] = value
    return safe


def _has_violation(diag: Mapping[str, object], *keys: str) -> bool:
    for key in keys:
        value = diag.get(key)
        if value is True:
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    return False


def _diagnostic_view(result: Mapping[str, Any]) -> dict[str, object]:
    merged: dict[str, object] = {}
    for key in ("diagnostics", "diagnostics_summary"):
        nested = result.get(key)
        if isinstance(nested, Mapping):
            merged.update(nested)
    for key in _SAFE_DIAGNOSTIC_KEYS:
        if key in result:
            merged[key] = result[key]
    return _safe_diagnostics(merged)


def _has_reason(reason: str, *tokens: str) -> bool:
    return any(token in reason for token in tokens)


def _is_answerable(case: Mapping[str, Any]) -> bool:
    category = str(case.get("category", "")).lower()
    if category in {"quality", "answerable"}:
        return True
    status = str(case.get("expected_status", "")).lower()
    if status in {"answered", "answerable"}:
        return True
    answerability = str(case.get("answerability", case.get("label_answerability", ""))).lower()
    return answerability in {"answerable", "partially_answerable"}


def _is_rejection(case: Mapping[str, Any]) -> bool:
    category = str(case.get("category", "")).lower()
    if category in {"rejection", "reject", "unanswerable"}:
        return True
    status = str(case.get("expected_status", "")).lower()
    return status in {"insufficient_evidence", "rejected", "unanswerable"}


def _classify(case: Mapping[str, Any], result: Mapping[str, Any], reason: str, diagnostics: Mapping[str, object], returned: list[str]) -> str:
    if (
        _has_reason(
            reason,
            "infrastructure",
            "backend",
            "timeout",
            "vector_path_unavailable",
            "exception",
            "error",
            "missing_retrieval_result",
            "calibration_artifact_mismatch",
            "mapping_unavailable",
            "score_space_mismatch",
            "embedding_unavailable",
            "opensearch_unavailable",
            "index_unavailable",
            "vector_error",
            "degraded",
        )
        or diagnostics.get("vector_error") is True
        or diagnostics.get("embedding_degraded") is True
        or diagnostics.get("calibration_artifact_mismatch") is True
        or diagnostics.get("score_space_match") is False
        or str(diagnostics.get("status", "")).lower() in {"error", "degraded", "unavailable", "failed"}
    ):
        return "infrastructure_failure"
    if (
        _has_reason(reason, "permission", "lifecycle", "forbidden", "deleted", "acl")
        or _has_violation(
            diagnostics,
            "permission_violations",
            "permission_violation",
            "deletion_residuals",
            "deletion_residual",
            "lifecycle_violations",
            "lifecycle_violation",
            "forbidden_returned",
            "acl_denied",
            "review_leakage",
        )
    ):
        return "permission_or_lifecycle"

    gold = set(_gold_ids(case))
    if _is_answerable(case) and not returned:
        return "gate_false_reject"
    if _is_rejection(case) and returned:
        return "gate_false_accept"
    if returned and not (gold & set(returned)):
        return "retrieval_miss"

    explicit = str(result.get("label", result.get("failure_label", ""))).lower()
    if explicit in {"annotation_issue", "insufficient_corpus"}:
        return explicit
    if bool(case.get("annotation_issue")):
        return "annotation_issue"
    if bool(case.get("insufficient_corpus")):
        return "insufficient_corpus"
    return "manual_review"


def attribute_failures(cases_file: Path, retrieval_result: Path, *, asset_audit: Path | None = None) -> dict[str, object]:
    """Attribute retrieval failures without emitting question, evidence, or vector content."""
    cases_file = Path(cases_file)
    retrieval_result = Path(retrieval_result)
    dataset_sha256 = _sha256(cases_file)
    cases_value = _load(cases_file)
    case_rows = _rows(cases_value, kind="cases")
    case_map: dict[str, dict[str, Any]] = {}
    for row in case_rows:
        case_id = _case_id(row)
        if case_id in case_map:
            raise ValueError(f"duplicate case ID: {case_id}")
        case_map[case_id] = row

    retrieval_value = _load(retrieval_result)
    retrieval_rows, selected_config, selected_value = _result_rows(retrieval_value)
    retrieval_hash = _artifact_hash(retrieval_value) if isinstance(retrieval_value, Mapping) else None
    if retrieval_hash and retrieval_hash != dataset_sha256:
        raise ValueError(f"dataset hash mismatch: retrieval={retrieval_hash} cases={dataset_sha256}")
    selected_hash = _artifact_hash(selected_value) if selected_value is not None else None
    if selected_hash and selected_hash != dataset_sha256:
        raise ValueError(f"selected config dataset hash mismatch: retrieval={selected_hash} cases={dataset_sha256}")
    if asset_audit is not None:
        audit_value = _load(Path(asset_audit))
        if not isinstance(audit_value, Mapping):
            raise ValueError("asset audit must be a JSON object")
        audit_hash = _artifact_hash(audit_value)
        if audit_hash and audit_hash != dataset_sha256:
            raise ValueError(f"asset audit dataset hash mismatch: audit={audit_hash} cases={dataset_sha256}")

    result_map: dict[str, dict[str, Any]] = {}
    for row in retrieval_rows:
        case_id = _case_id(row, result=True)
        if case_id in result_map:
            raise ValueError(f"duplicate retrieval case ID: {case_id}")
        result_map[case_id] = row
    extra_ids = sorted(set(result_map) - set(case_map))
    if extra_ids:
        raise ValueError(f"retrieval cases not present in cases file: {', '.join(extra_ids)}")

    per_case: list[dict[str, object]] = []
    label_counts: Counter[str] = Counter()
    unresolved: list[str] = []
    for case_id, case in case_map.items():
        result = result_map.get(case_id, {})
        raw_returned = result.get("returned_ids", result.get("retrieved_evidence_ids", []))
        returned = [str(item) for item in raw_returned] if isinstance(raw_returned, (list, tuple)) else []
        reason = _safe_reason(result.get("reason")) if result else "missing_retrieval_result"
        diagnostics = _diagnostic_view(result)
        label = _classify(case, result, reason, diagnostics, returned)
        row = {
            "case_id": case_id,
            "category": str(case.get("category", "")),
            "gold_evidence_sha256": _gold_hash(_gold_ids(case)),
            "returned_ids": returned,
            "reason": reason,
            "diagnostics_summary": diagnostics,
            "labels": [label],
        }
        per_case.append(row)
        label_counts[label] += 1
        if label == "manual_review":
            unresolved.append(case_id)

    counts: dict[str, object] = {label: int(label_counts.get(label, 0)) for label in _LABELS}
    counts["total"] = len(per_case)
    return {
        "run_meta": {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "cases_file": cases_file.name,
            "retrieval_result": retrieval_result.name,
            "selected_config": selected_config,
            "case_count": len(per_case),
        },
        "dataset_sha256": dataset_sha256,
        "counts": counts,
        "per_case": per_case,
        "unresolved_manual_review": unresolved,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Attribute retrieval failures without emitting PHI")
    parser.add_argument("--cases-file", required=True)
    parser.add_argument("--retrieval-results", required=True)
    parser.add_argument("--asset-audit")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    report = attribute_failures(Path(args.cases_file), Path(args.retrieval_results), asset_audit=Path(args.asset_audit) if args.asset_audit else None)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
