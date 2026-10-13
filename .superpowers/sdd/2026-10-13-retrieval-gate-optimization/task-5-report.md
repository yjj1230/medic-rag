# Task 5 final report

## Frozen test rerun

- Dataset: `rag_cases.test.v2.1.jsonl`, 120 cases; SHA-256 `ed33014ce05d20b8cf98eb915833183a8e9284691eb996d71be89b9a90c20e5b`.
- Real seams: OpenSearch local indices and local `BAAI/bge-small-zh-v1.5` (512 dimensions).
- Selected calibration: `config/calibration/cosine-gate-2026-10-14.json`; cosine threshold `0.76`, vector-only threshold `0.80`, score space `cosinesimil`.
- Five measured latency rounds completed.

## Metrics

Pre gate (`A3-hybrid-rrf60`): Recall@3 0.9429, Precision@3 0.3286, MRR 0.8119, nDCG@3 0.8470, rejection accuracy 0.0000, false-reject rate 0.0000.

Post gate (`G1-hybrid-rrf60-gate`): Recall@3 0.8714, Precision@3 0.3619, MRR 0.7476, nDCG@3 0.7808, rejection accuracy 0.6333, false-reject rate 5/70 (0.0714). Safety counts remained zero for permission violations, deletion residuals, and review leakage. Latency was p50 27.0 ms, p95 45.4 ms, p99 48.7 ms.

Citation metrics are reported against the separately identified claims dataset hash; the frozen test hash is recorded independently. Attribution totals are 120 cases: retrieval miss 4, gate false reject 5, gate false accept 11, manual review 100, with unresolved manual-review IDs preserved.

## Provenance and privacy fixes

`build_report` now requires and cross-checks retrieval calibration path/hash, selected base/vector thresholds, mapping and record score spaces, embedding model, index versions, citation identity/counts, and attribution case IDs/denominators. The committed retrieval artifact removes the duplicate raw case payload containing question and gold text. Regression coverage scans serialized final artifacts for forbidden fields and values.

Validation: focused report tests 8 passed; evaluation and rejection tests 78 passed; full suite 93 passed; Ruff passed.
