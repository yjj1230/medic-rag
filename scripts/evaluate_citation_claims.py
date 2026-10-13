"""运行人工标注的引用陈述校准集，输出不含正文的可复现结果。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from medical_rag.eval.citation_metrics import CitationClaim, evaluate_citation_claims


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_claims(path: Path) -> list[CitationClaim]:
    claims: list[CitationClaim] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        claims.append(
            CitationClaim(
                id=str(raw["id"]),
                claim=str(raw.get("claim", "")),
                evidence_ids=tuple(str(value) for value in raw.get("evidence_ids", ())),
                expected_verdict=str(raw["expected_verdict"]),  # type: ignore[arg-type]
                claim_kind=str(raw.get("claim_kind", "unknown")),
                evidence_sha256={str(key): str(value) for key, value in (raw.get("evidence_sha256") or {}).items()},
            )
        )
    return claims


def _invalid_reason(claim: CitationClaim, evidence_id: str, lookup: Mapping[str, Mapping[str, object]]) -> str | None:
    record = lookup.get(evidence_id)
    if record is None:
        return "missing_evidence"
    if record.get("active", record.get("source_active", False)) is not True:
        return "inactive_source"
    if record.get("retrieval_eligible") is not True:
        return "ineligible_evidence"
    if str(record.get("content_hash", "")) != str(claim.evidence_sha256.get(evidence_id, "")):
        return "hash_mismatch"
    return None


def evaluate_file(
    claims_path: Path,
    evidence_lookup: Mapping[str, Mapping[str, object]],
    *,
    reviewer: str | None = None,
) -> dict[str, object]:
    claims = _load_claims(Path(claims_path))
    metrics = evaluate_citation_claims(claims, evidence_lookup)
    invalid_annotations: list[dict[str, str]] = []
    per_claim: list[dict[str, object]] = []
    contradictions: list[str] = []
    for claim in claims:
        valid_ids: list[str] = []
        invalid: list[dict[str, str]] = []
        for evidence_id in claim.evidence_ids:
            reason = _invalid_reason(claim, evidence_id, evidence_lookup)
            if reason:
                item = {"claim_id": claim.id, "evidence_id": evidence_id, "reason": reason}
                invalid_annotations.append(item)
                invalid.append(item)
            else:
                valid_ids.append(evidence_id)
        if claim.expected_verdict == "contradicted":
            contradictions.append(claim.id)
        per_claim.append(
            {
                "claim_id": claim.id,
                "claim_kind": claim.claim_kind,
                "expected_verdict": claim.expected_verdict,
                "evidence_ids": list(claim.evidence_ids),
                "valid_evidence_ids": valid_ids,
                "invalid_evidence": invalid,
                "supported": claim.expected_verdict == "entailed" and len(valid_ids) == len(claim.evidence_ids),
            }
        )
    return {
        "run_meta": {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "reviewer": reviewer,
            "runner": "scripts/evaluate_citation_claims.py",
        },
        "dataset_sha256": _sha256(Path(claims_path)),
        "counts": {
            "claims": len(claims),
            "evidence_links": sum(len(claim.evidence_ids) for claim in claims),
            "invalid_annotations": len(invalid_annotations),
        },
        "metrics": {
            "grounded_claims": metrics.grounded_claims,
            "valid_citations": metrics.valid_citations,
            "supported_claims": metrics.supported_claims,
            "contradicted_claims": metrics.contradicted_claims,
            "insufficient_claims": metrics.insufficient_claims,
            "citation_validity": metrics.citation_validity,
            "citation_accuracy": metrics.citation_accuracy,
            "citation_coverage": metrics.citation_coverage,
        },
        "invalid_annotations": invalid_annotations,
        "contradictions": contradictions,
        "per_claim": per_claim,
    }


def _lookup_index(client: Any, index: str, evidence_ids: list[str]) -> dict[str, dict[str, object]]:
    if not evidence_ids:
        return {}
    response = client.search(
        index=index,
        body={
            "size": len(evidence_ids),
            "_source": ["evidence_unit_id", "content_hash", "retrieval_eligible", "source_active", "deleted"],
            "query": {
                "bool": {
                    "should": [
                        {"terms": {"evidence_unit_id": evidence_ids}},
                        {"terms": {"id": evidence_ids}},
                        {"ids": {"values": evidence_ids}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        },
    )
    lookup: dict[str, dict[str, object]] = {}
    for hit in (response or {}).get("hits", {}).get("hits", []):
        source = dict(hit.get("_source") or {})
        key = str(source.get("evidence_unit_id") or source.get("id") or hit.get("_id"))
        source["active"] = source.get("source_active", False) and not source.get("deleted", False)
        lookup[key] = source
    return lookup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claims-file", required=True)
    parser.add_argument("--public-index", required=True)
    parser.add_argument("--private-index", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--reviewer", required=True)
    args = parser.parse_args(argv)
    from opensearchpy import OpenSearch

    claims_path = Path(args.claims_file)
    claims = _load_claims(claims_path)
    evidence_ids = sorted({evidence_id for claim in claims for evidence_id in claim.evidence_ids})
    client = OpenSearch(os.getenv("OPENSEARCH_URL", "http://127.0.0.1:9200"), timeout=10)
    lookup = _lookup_index(client, args.public_index, evidence_ids)
    private_lookup = _lookup_index(client, args.private_index, evidence_ids)
    lookup.update(private_lookup)
    report = evaluate_file(claims_path, lookup, reviewer=args.reviewer)
    report["index_snapshot"] = {
        "public_index": args.public_index,
        "private_index": args.private_index,
        "matched_evidence_ids": sorted(lookup),
    }
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
