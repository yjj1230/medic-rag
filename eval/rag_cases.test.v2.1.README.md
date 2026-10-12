# RAG Test v2.1 (Frozen Revision Set)

Test v2.1 is derived from the frozen `rag_cases.test.v2.jsonl`; the original Test v2 remains unchanged. It contains 15 revised quality entries from the manual review and source-coverage follow-up:

- 3 cases use direct-support chunks from the explicit WHO supplement.
- The revised entries express equivalent official evidence, direct-support supplements, and equivalent full/short private-report evidence with `gold_evidence_groups`.

## Freeze basis

1. v2.1 completed equivalent-evidence review, hybrid retrieval regression, and safety regression on 2026-10-08.
2. A3-hybrid-rrf60 grouped evaluation: quality n=70, accepted Hit Rate `1.0000`, accepted MRR `0.8190`, unmatched cases `0`.
3. `gold_evidence_ids` contains one canonical evidence ID for legacy evaluators; `gold_evidence_groups` contains the medically reviewed alternatives.
4. The original `rag_cases.test.v2.jsonl` and its manifest are retained as a historical baseline and must not be overwritten.

Future parameter tuning must use the Dev set or a separate experiment set. Test v2.1 is an independent confirmation set and must not be used for reverse tuning.

The dataset SHA256 is recorded in `rag_cases.test.v2.1.manifest.json`.
