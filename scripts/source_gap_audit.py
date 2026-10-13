"""确认知识缺口后再允许补充官方来源。"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _records(index_metadata: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for metadata in index_metadata.values():
        for record in metadata.get("records", ()) if isinstance(metadata.get("records", ()), list) else ():
            if isinstance(record, Mapping) and record.get("id"):
                records[str(record["id"])] = dict(record)
        for evidence_id in metadata.get("evidence_ids", ()) if isinstance(metadata.get("evidence_ids", ()), list) else ():
            records.setdefault(str(evidence_id), {"id": str(evidence_id), "active": True, "retrieval_eligible": True})
    return records


def _valid(record: Mapping[str, object] | None) -> bool:
    if record is None:
        return False
    return record.get("active", record.get("source_active", False)) is True and record.get("retrieval_eligible") is True


def find_source_gaps(
    cases_file: Path,
    retrieval_results: Path,
    index_metadata: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    cases = _load_jsonl(Path(cases_file))
    payload = json.loads(Path(retrieval_results).read_text(encoding="utf-8"))
    configs = payload.get("configs", []) if isinstance(payload, dict) else []
    config = next((item for item in configs if item.get("name") in {"A3-hybrid-rrf60", "cosine-0.780"}), configs[0] if configs else {})
    result_by_id = {str(item.get("case_id")): item for item in config.get("cases", [])}
    records = _records(index_metadata)
    confirmed: list[dict[str, object]] = []
    retrieval_misses: list[dict[str, object]] = []
    for case in cases:
        if case.get("expected_status") != "answered" and case.get("category") not in {"quality", "answerable"}:
            continue
        alternatives = case.get("gold_evidence_groups") or [[value] for value in case.get("gold_evidence_ids", [])]
        alternatives = [[str(value) for value in group] for group in alternatives]
        direct_ids = {evidence_id for group in alternatives for evidence_id in group}
        returned = set(str(value) for value in (result_by_id.get(str(case.get("id")), {}).get("returned_ids", []) or []))
        valid_direct = {evidence_id for evidence_id in direct_ids if _valid(records.get(evidence_id))}
        if valid_direct and not (returned & valid_direct):
            retrieval_misses.append({"case_id": case.get("id"), "gold_evidence_ids": sorted(valid_direct), "reason": "direct_support_exists_but_not_returned"})
        elif not valid_direct:
            confirmed.append({"case_id": case.get("id"), "question_sha256": hashlib.sha256(str(case.get("question", "")).encode()).hexdigest(), "gold_evidence_ids": sorted(direct_ids), "reason": "no_active_eligible_direct_support"})
    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "cases_file": str(cases_file),
        "retrieval_results": str(retrieval_results),
        "confirmed_source_gaps": confirmed,
        "retrieval_misses": retrieval_misses,
        "gap_count": len(confirmed),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases-file", required=True)
    parser.add_argument("--retrieval-results", required=True)
    parser.add_argument("--asset-audit", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    audit = json.loads(Path(args.asset_audit).read_text(encoding="utf-8"))
    report = find_source_gaps(Path(args.cases_file), Path(args.retrieval_results), audit.get("index_metadata", {}))
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
