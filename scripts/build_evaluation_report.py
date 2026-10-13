"""合并真实评测工件，并在合并前检查数据与索引身份。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"artifact must be an object: {path}")
    return value


def _selected_config(retrieval: dict[str, Any]) -> dict[str, Any]:
    configs = retrieval.get("configs", [])
    if not configs:
        raise ValueError("retrieval artifact has no configs")
    return next((item for item in configs if item.get("name") == "G1-hybrid-rrf60-gate"), configs[0])


def build_report(
    retrieval_result: Path,
    citation_result: Path,
    asset_audit: Path,
    threshold_result: Path | None,
    source_gap_audit: Path | None = None,
    failure_attribution: Path | None = None,
) -> dict[str, object]:
    retrieval = _read(Path(retrieval_result))
    citation = _read(Path(citation_result))
    assets = _read(Path(asset_audit))
    expected_hash = str(assets.get("dataset_sha256", ""))
    retrieval_hash = str(retrieval.get("dataset_sha256", expected_hash))
    if expected_hash and retrieval_hash != expected_hash:
        raise ValueError(f"dataset hash mismatch: retrieval={retrieval_hash} assets={expected_hash}")
    retrieval_meta = retrieval.get("meta", {})
    index_metadata = assets.get("index_metadata", {})
    public_index = str(retrieval_meta.get("public_index", ""))
    private_index = str(retrieval_meta.get("private_index", ""))
    if public_index and str(index_metadata.get("public", {}).get("index", public_index)) != public_index:
        raise ValueError("public index identity mismatch")
    if private_index and str(index_metadata.get("private", {}).get("index", private_index)) != private_index:
        raise ValueError("private index identity mismatch")
    snapshot = citation.get("index_snapshot", {})
    if snapshot and (snapshot.get("public_index") != public_index or snapshot.get("private_index") != private_index):
        raise ValueError("citation index identity mismatch")
    citation_meta = citation.get("run_meta", {})
    citation_hash = str(citation.get("dataset_sha256", ""))
    citation_test_hash = citation.get("test_dataset_sha256") or citation_meta.get("test_dataset_sha256")
    citation_kind = str(citation.get("dataset_kind", citation_meta.get("dataset_kind", "claims")))
    if citation_test_hash and str(citation_test_hash) != expected_hash:
        raise ValueError("citation test dataset hash mismatch")
    if citation_kind in {"test", "frozen_test"} and citation_hash and citation_hash != expected_hash:
        raise ValueError("citation dataset hash mismatch")
    counts = citation.get("counts", {})
    for name in ("claims", "evidence_links", "invalid_annotations"):
        if name in counts and (not isinstance(counts[name], int) or counts[name] < 0):
            raise ValueError("citation counts invalid")
    threshold = _read(Path(threshold_result)) if threshold_result else None
    if threshold:
        threshold_meta = threshold.get("run_meta", threshold)
        if threshold_meta.get("public_index") and threshold_meta.get("public_index") != public_index:
            raise ValueError("threshold index identity mismatch")
        if threshold_meta.get("private_index") and threshold_meta.get("private_index") != private_index:
            raise ValueError("threshold index identity mismatch")
        threshold_score_space = threshold_meta.get("score_space", threshold.get("score_space"))
        if threshold_score_space != "cosinesimil":
            raise ValueError("threshold score space mismatch")
        threshold_embedding = threshold_meta.get("embedding_model", threshold.get("embedding_model"))
        if threshold_embedding and retrieval_meta.get("embedding_model") and threshold_embedding != retrieval_meta.get("embedding_model"):
            raise ValueError("threshold embedding model mismatch")
        for scope, key in (("public", "public_index_version"), ("private", "private_index_version")):
            version = threshold_meta.get(key)
            known_versions = index_metadata.get(scope, {}).get("index_versions", [])
            if version and known_versions and version not in known_versions:
                raise ValueError("threshold index version mismatch")
        known_models = {
            model
            for scope in ("public", "private")
            for model in index_metadata.get(scope, {}).get("embedding_models", [])
        }
        if threshold_embedding and known_models and threshold_embedding not in known_models:
            raise ValueError("threshold embedding model mismatch")
    selected = _selected_config(retrieval)
    selected_cfg = selected.get("config", {})
    threshold_meta = (threshold.get("run_meta", threshold) if threshold else {})
    base_threshold = threshold_meta.get("cosine_threshold", threshold.get("cosine_threshold") if threshold else None)
    vector_threshold = threshold_meta.get(
        "vector_only_cosine_threshold",
        threshold.get("vector_only_cosine_threshold") if threshold else None,
    )
    selected_thresholds = selected.get("selected_thresholds", {})
    config_base = selected_cfg.get("cosine_threshold", selected.get("cosine_threshold", selected_thresholds.get("cosine_threshold")))
    config_vector = selected_cfg.get(
        "vector_only_cosine_threshold",
        selected.get("vector_only_cosine_threshold", selected_thresholds.get("vector_only_cosine_threshold")),
    )
    if threshold:
        required_identity = {
            "cosine_threshold": retrieval_meta.get("cosine_threshold"),
            "vector_only_cosine_threshold": retrieval_meta.get("vector_only_cosine_threshold"),
            "mapping_score_space": retrieval_meta.get("mapping_score_space"),
            "record_score_space": retrieval_meta.get("record_score_space"),
            "score_space": retrieval_meta.get("score_space"),
        }
        if any(value is None for value in required_identity.values()):
            raise ValueError("retrieval threshold identity is incomplete")
        if base_threshold is None or vector_threshold is None:
            raise ValueError("threshold identity is incomplete")
        if float(required_identity["cosine_threshold"]) != float(base_threshold) or float(required_identity["vector_only_cosine_threshold"]) != float(vector_threshold):
            raise ValueError("retrieval threshold identity mismatch")
        if required_identity["score_space"] != "cosinesimil" or required_identity["mapping_score_space"] != required_identity["record_score_space"]:
            raise ValueError("retrieval score space identity mismatch")
    if config_base is not None and base_threshold is not None and float(config_base) != float(base_threshold):
        raise ValueError("selected threshold identity mismatch")
    if config_vector is not None and vector_threshold is not None and float(config_vector) != float(vector_threshold):
        raise ValueError("selected threshold identity mismatch")
    mapping_spaces = {
        str(index_metadata.get(scope, {}).get("embedding_mapping", {}).get("space_type", ""))
        for scope in ("public", "private")
        if index_metadata.get(scope, {}).get("embedding_mapping", {}).get("space_type")
    }
    mapping_score_space = next(iter(mapping_spaces), None)
    if len(mapping_spaces) > 1:
        raise ValueError("mapping score space mismatch")
    record_score_space = retrieval_meta.get("record_score_space")
    if threshold and not record_score_space:
        raise ValueError("record score space identity is missing")
    if mapping_score_space and record_score_space and mapping_score_space != record_score_space:
        raise ValueError("record score space mismatch")
    attribution = _read(Path(failure_attribution)) if failure_attribution else None
    # Legacy reports without a threshold remain supported; final gate reports require attribution.
    if threshold and attribution is None:
        raise ValueError("failure attribution artifact is required")
    if attribution:
        attribution_hash = str(attribution.get("dataset_sha256", ""))
        if attribution_hash and expected_hash and attribution_hash != expected_hash:
            raise ValueError("failure attribution dataset hash mismatch")
        attribution_meta = attribution.get("run_meta", {})
        for key, expected in (("public_index", public_index), ("private_index", private_index), ("embedding_model", retrieval_meta.get("embedding_model")), ("score_space", record_score_space)):
            observed = attribution_meta.get(key)
            if observed and expected and observed != expected:
                raise ValueError(f"failure attribution {key} mismatch")
    selected_cases = list(selected.get("cases", []))
    case_ids = {str(row.get("case_id")) for row in selected_cases if row.get("case_id") is not None}
    if threshold and (len(case_ids) != len(selected_cases) or len(selected_cases) != int(assets.get("case_count", len(selected_cases)))):
        raise ValueError("retrieval case set/count mismatch")
    if attribution and threshold:
        per_case = attribution.get("per_case", [])
        attribution_ids = [str(row.get("case_id")) for row in per_case if isinstance(row, dict)]
        if len(attribution_ids) != len(set(attribution_ids)) or set(attribution_ids) != case_ids:
            raise ValueError("failure attribution case IDs mismatch")
        attribution_counts = attribution.get("counts", {})
        if attribution_counts.get("total") != len(case_ids):
            raise ValueError("failure attribution denominator mismatch")
        unresolved = set(str(value) for value in attribution.get("unresolved_manual_review", []))
        if not unresolved.issubset(case_ids):
            raise ValueError("failure attribution unresolved IDs mismatch")
    quality_cases = [row for row in selected_cases if row.get("category") == "quality"]
    rejection_cases = [row for row in selected_cases if row.get("category") == "rejection"]
    selected_rejection = dict(selected.get("rejection", {}))
    failure_summary = {
        "quality_false_rejects": sum(1 for row in quality_cases if not row.get("returned_ids")),
        "rejection_false_accepts": sum(1 for row in rejection_cases if row.get("returned_ids")),
        "reason_counts": {
            str(reason): sum(1 for row in selected_cases if row.get("reason") == reason)
            for reason in sorted({row.get("reason") for row in selected_cases if row.get("reason")})
        },
    }
    selected_rejection["false_reject_rate"] = (
        failure_summary["quality_false_rejects"] / len(quality_cases) if quality_cases else 0.0
    )
    source_gaps = _read(Path(source_gap_audit)) if source_gap_audit else None
    pre_config = next((item for item in retrieval.get("configs", []) if item.get("name") in {"A3-hybrid-rrf60", "A3-hybrid-rrf60-gate"}), None)
    if pre_config:
        pre_cases = list(pre_config.get("cases", []))
        pre_quality = [row for row in pre_cases if row.get("category") == "quality"]
        pre_rejection = dict(pre_config.get("rejection", {}))
        pre_rejection["false_reject_rate"] = (
            sum(1 for row in pre_quality if not row.get("returned_ids")) / len(pre_quality) if pre_quality else 0.0
        )
        pre_config = {**pre_config, "rejection": pre_rejection}
    return {
        "run_meta": {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "dataset_sha256": expected_hash,
            "case_count": assets.get("case_count"),
            "frozen_test": True,
        },
        "identities": {
            "public_index": public_index,
            "private_index": private_index,
            "embedding_model": retrieval_meta.get("embedding_model"),
            "embedding_dimensions": retrieval_meta.get("embedding_dimensions"),
            "mapping_score_space": mapping_score_space,
            "record_score_space": record_score_space,
            "score_space": record_score_space,
            "public_index_version": threshold_meta.get("public_index_version") if threshold else None,
            "private_index_version": threshold_meta.get("private_index_version") if threshold else None,
        },
        "selected_thresholds": {
            "cosine_threshold": base_threshold,
            "vector_only_cosine_threshold": vector_threshold,
            "score_space": record_score_space,
        },
        "retrieval": {
            "config": selected.get("name"),
            "dataset_sha256": retrieval_hash,
            "quality": selected.get("quality", {}),
            "rejection": selected_rejection,
            "safety": selected.get("safety", {}),
            "latency": selected.get("latency", {}),
            "case_count": len(selected_cases),
            "failure_summary": failure_summary,
            "pre": pre_config or {},
            "post": {**selected, "rejection": selected_rejection},
        },
        "citation": {**citation, "identity": {"dataset_kind": citation_kind, "dataset_sha256": citation_hash or None, "test_dataset_sha256": citation_test_hash or None}},
        "threshold": threshold,
        "failure_attribution": attribution,
        "source_gap_audit": source_gaps,
        "asset_audit": {
            "case_count": assets.get("case_count"),
            "category_counts": assets.get("category_counts"),
            "missing_gold_evidence_ids": assets.get("missing_gold_evidence_ids", []),
            "missing_forbidden_evidence_ids": assets.get("missing_forbidden_evidence_ids", []),
        },
    }


def _markdown(report: dict[str, object]) -> str:
    retrieval = report["retrieval"]
    citation = report["citation"]
    pre = retrieval.get("pre", {})
    post = retrieval.get("post", {})

    def metric_lines(label: str, value: dict[str, object]) -> list[str]:
        quality = value.get("quality", {})
        rejection = value.get("rejection", {})
        safety = value.get("safety", {})
        latency = value.get("latency", {})
        return [
            f"- {label} Recall@3：{quality.get('recall_at_k')}",
            f"- {label} Precision@3：{quality.get('precision_at_k')}",
            f"- {label} MRR：{quality.get('mrr')}",
            f"- {label} nDCG@3：{quality.get('ndcg_at_k')}",
            f"- {label} 拒答准确率：{rejection.get('accuracy')}",
            f"- {label} 误拒率：{rejection.get('false_reject_rate')}",
            f"- {label} 安全：{safety}",
            f"- {label} 延迟：p50={latency.get('p50_ms')} ms，p95={latency.get('p95_ms')} ms，p99={latency.get('p99_ms')} ms",
        ]

    return "\n".join(
        [
            "# 医疗 RAG 检索评测报告（2026-10-13）",
            "",
            f"- 冻结集：{report['run_meta']['case_count']} 条；SHA-256：`{report['run_meta']['dataset_sha256']}`",
            f"- 索引：`{report['identities']['public_index']}` / `{report['identities']['private_index']}`",
            f"- 模型：`{report['identities']['embedding_model']}`，维度 {report['identities']['embedding_dimensions']}",
            f"- 分数空间：映射 `{report['identities']['mapping_score_space']}`；记录 `{report['identities']['record_score_space']}`",
            f"- 门禁阈值：cosine={report['selected_thresholds']['cosine_threshold']}；vector-only={report['selected_thresholds']['vector_only_cosine_threshold']}",
            "",
            "## 检索与拒答",
            "",
            f"- 配置：`{retrieval['config']}`",
            *metric_lines("pre", pre),
            *metric_lines("post", post),
            f"- answerable 误拒：{retrieval['failure_summary'].get('quality_false_rejects')} 条；拒答误接纳：{retrieval['failure_summary'].get('rejection_false_accepts')} 条",
            f"- pre/post：{retrieval.get('pre', {}).get('name', 'unavailable')} → {retrieval.get('post', {}).get('name', retrieval.get('config'))}",
            f"- 延迟：p50={retrieval['latency'].get('p50_ms')} ms，p95={retrieval['latency'].get('p95_ms')} ms，p99={retrieval['latency'].get('p99_ms')} ms",
            "",
            "## 引用",
            "",
            f"- 标注陈述数：{citation.get('counts', {}).get('claims')}",
            f"- Citation validity：{citation.get('metrics', {}).get('citation_validity')}",
            f"- Citation accuracy：{citation.get('metrics', {}).get('citation_accuracy')}",
            f"- Citation coverage：{citation.get('metrics', {}).get('citation_coverage')}",
            "",
            "## 失败归因",
            "",
            f"- 标签计数（分母 {((report.get('failure_attribution') or {}).get('counts') or {}).get('total', 0)}）：{((report.get('failure_attribution') or {}).get('counts') or {})}",
            f"- 未决人工复核 ID：{((report.get('failure_attribution') or {}).get('unresolved_manual_review') or [])}",
            "",
            "## 知识缺口",
            "",
            f"- 已确认缺口：{(report.get('source_gap_audit') or {}).get('gap_count', 0)}",
            "",
            "阈值只使用 dev 集选择；冻结 test 集仅用于本次最终确认。",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval-result", required=True)
    parser.add_argument("--citation-result", required=True)
    parser.add_argument("--asset-audit", required=True)
    parser.add_argument("--threshold-result")
    parser.add_argument("--failure-attribution")
    parser.add_argument("--source-gap-audit")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    report = build_report(
        Path(args.retrieval_result),
        Path(args.citation_result),
        Path(args.asset_audit),
        Path(args.threshold_result) if args.threshold_result else None,
        Path(args.source_gap_audit) if args.source_gap_audit else None,
        Path(args.failure_attribution) if args.failure_attribution else None,
    )
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".md":
        output.write_text(_markdown(report), encoding="utf-8")
    else:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
