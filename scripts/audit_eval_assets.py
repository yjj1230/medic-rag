"""审计冻结评测资产和索引元数据，不读取索引正文。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_no} must be a JSON object")
        rows.append(value)
    return rows


def _ids(rows: list[dict[str, Any]], key: str) -> set[str]:
    values: set[str] = set()
    for row in rows:
        raw = row.get(key, []) or []
        if isinstance(raw, list):
            values.update(str(item) for item in raw if str(item).strip())
    return values


def audit_eval_assets(
    cases_path: Path,
    index_metadata: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """返回可复现的评测资产审计报告。

    ``index_metadata`` 只接受计数、映射和 ID 元数据；调用方不应传入正文。
    """

    cases_path = Path(cases_path)
    rows = _load_jsonl(cases_path)
    ids = [str(row.get("id", "")) for row in rows]
    duplicate_case_ids = sorted({case_id for case_id in ids if ids.count(case_id) > 1 and case_id})
    category_counts = dict(Counter(str(row.get("category", "quality")) for row in rows))
    indexed_ids: set[str] = set()
    for metadata in index_metadata.values():
        raw_ids = metadata.get("evidence_ids", ())
        if isinstance(raw_ids, (list, tuple, set)):
            indexed_ids.update(str(item) for item in raw_ids)
    gold_ids = _ids(rows, "gold_evidence_ids")
    forbidden_ids = _ids(rows, "forbidden_evidence_ids")

    overlap: list[str] = []
    dev_path = cases_path.with_name("rag_cases.dev.jsonl")
    if dev_path.is_file():
        dev_questions = {str(row.get("question", "")).strip() for row in _load_jsonl(dev_path)}
        test_questions = {str(row.get("question", "")).strip() for row in rows}
        overlap = sorted(question for question in dev_questions & test_questions if question)

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "cases_file": str(cases_path),
        "dataset_sha256": _sha256(cases_path),
        "case_count": len(rows),
        "category_counts": category_counts,
        "duplicate_case_ids": duplicate_case_ids,
        "dev_test_question_overlap": overlap,
        "missing_gold_evidence_ids": sorted(gold_ids - indexed_ids),
        "missing_forbidden_evidence_ids": sorted(forbidden_ids - indexed_ids),
        "index_metadata": {str(name): dict(metadata) for name, metadata in index_metadata.items()},
    }


def _index_metadata(client: Any, name: str) -> dict[str, object]:
    count = int((client.count(index=name) or {}).get("count", 0))
    response = client.search(
        index=name,
        body={
            "size": min(max(count, 1), 10000),
            "_source": [
                "evidence_unit_id",
                "embedding_model",
                "embedding_dimensions",
                "index_version",
                "retrieval_eligible",
                "source_active",
                "deleted",
            ],
            "query": {"match_all": {}},
        },
    )
    evidence_ids: list[str] = []
    models: set[str] = set()
    dimensions: set[int] = set()
    versions: set[str] = set()
    for hit in (response or {}).get("hits", {}).get("hits", []):
        source = hit.get("_source") or {}
        evidence_id = source.get("evidence_unit_id") or hit.get("_id")
        if evidence_id:
            evidence_ids.append(str(evidence_id))
        if source.get("embedding_model"):
            models.add(str(source["embedding_model"]))
        if source.get("embedding_dimensions") is not None:
            dimensions.add(int(source["embedding_dimensions"]))
        if source.get("index_version"):
            versions.add(str(source["index_version"]))
    mapping = client.indices.get_mapping(index=name)
    props = next(iter(mapping.values()), {}).get("mappings", {}).get("properties", {})
    embedding = props.get("embedding", {}) if isinstance(props, dict) else {}
    method = embedding.get("method", {}) if isinstance(embedding, dict) else {}
    return {
        "index": name,
        "count": count,
        "evidence_ids": sorted(set(evidence_ids)),
        "embedding_models": sorted(models),
        "embedding_dimensions": sorted(dimensions),
        "index_versions": sorted(versions),
        "embedding_mapping": {
            "dimension": embedding.get("dimension"),
            "space_type": method.get("space_type"),
            "engine": method.get("engine"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases-file", required=True)
    parser.add_argument("--public-index", required=True)
    parser.add_argument("--private-index", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    from opensearchpy import OpenSearch

    client = OpenSearch(os.getenv("OPENSEARCH_URL", "http://127.0.0.1:9200"), timeout=10)
    metadata = {
        "public": _index_metadata(client, args.public_index),
        "private": _index_metadata(client, args.private_index),
    }
    report = audit_eval_assets(Path(args.cases_file), metadata)
    report["public_index"] = args.public_index
    report["private_index"] = args.private_index
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
