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
    threshold = _read(Path(threshold_result)) if threshold_result else None
    if threshold:
        threshold_meta = threshold.get("run_meta", {})
        if threshold_meta.get("public_index") != public_index or threshold_meta.get("private_index") != private_index:
            raise ValueError("threshold index identity mismatch")
        if threshold_meta.get("score_space") != "cosinesimil":
            raise ValueError("threshold score space mismatch")
    selected = _selected_config(retrieval)
    selected_cases = list(selected.get("cases", []))
    quality_cases = [row for row in selected_cases if row.get("category") == "quality"]
    rejection_cases = [row for row in selected_cases if row.get("category") == "rejection"]
    failure_summary = {
        "quality_false_rejects": sum(1 for row in quality_cases if not row.get("returned_ids")),
        "rejection_false_accepts": sum(1 for row in rejection_cases if row.get("returned_ids")),
        "reason_counts": {
            str(reason): sum(1 for row in selected_cases if row.get("reason") == reason)
            for reason in sorted({row.get("reason") for row in selected_cases if row.get("reason")})
        },
    }
    source_gaps = _read(Path(source_gap_audit)) if source_gap_audit else None
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
            "score_space": threshold.get("run_meta", {}).get("score_space") if threshold else None,
        },
        "retrieval": {
            "config": selected.get("name"),
            "dataset_sha256": retrieval_hash,
            "quality": selected.get("quality", {}),
            "rejection": selected.get("rejection", {}),
            "safety": selected.get("safety", {}),
            "latency": selected.get("latency", {}),
            "case_count": len(selected_cases),
            "failure_summary": failure_summary,
        },
        "citation": citation,
        "threshold": threshold,
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
    return "\n".join(
        [
            "# 医疗 RAG 检索评测报告（2026-10-13）",
            "",
            f"- 冻结集：{report['run_meta']['case_count']} 条；SHA-256：`{report['run_meta']['dataset_sha256']}`",
            f"- 索引：`{report['identities']['public_index']}` / `{report['identities']['private_index']}`",
            f"- 模型：`{report['identities']['embedding_model']}`，维度 {report['identities']['embedding_dimensions']}",
            "",
            "## 检索与拒答",
            "",
            f"- 配置：`{retrieval['config']}`",
            f"- Recall@3：{retrieval['quality'].get('recall_at_k')}",
            f"- Precision@3：{retrieval['quality'].get('precision_at_k')}",
            f"- MRR：{retrieval['quality'].get('mrr')}",
            f"- nDCG@3：{retrieval['quality'].get('ndcg_at_k')}",
            f"- 拒答准确率：{retrieval['rejection'].get('accuracy')}",
            f"- 安全全零：{retrieval['safety'].get('all_zero')}",
            f"- answerable 误拒：{retrieval['failure_summary'].get('quality_false_rejects')} 条；拒答误接纳：{retrieval['failure_summary'].get('rejection_false_accepts')} 条",
            "",
            "## 引用",
            "",
            f"- 标注陈述数：{citation.get('counts', {}).get('claims')}",
            f"- Citation validity：{citation.get('metrics', {}).get('citation_validity')}",
            f"- Citation accuracy：{citation.get('metrics', {}).get('citation_accuracy')}",
            f"- Citation coverage：{citation.get('metrics', {}).get('citation_coverage')}",
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
    parser.add_argument("--source-gap-audit")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    report = build_report(
        Path(args.retrieval_result),
        Path(args.citation_result),
        Path(args.asset_audit),
        Path(args.threshold_result) if args.threshold_result else None,
        Path(args.source_gap_audit) if args.source_gap_audit else None,
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
