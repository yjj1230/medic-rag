# Task 3 report: retrieval failure attribution

## Status

Implemented and committed the privacy-safe failure attribution utility.

## Changes

- Added `scripts/retrieval_failure_attribution.py` with the required `attribute_failures(cases_file, retrieval_result, *, asset_audit=None)` interface and CLI flags:
  `--cases-file`, `--retrieval-results`, `--asset-audit`, and `--out`.
- Supports JSONL case files, JSON case arrays/objects, direct retrieval result rows, and the existing `configs[].cases` retrieval artifact shape. The named `G1-hybrid-rrf60-gate` configuration is selected when present.
- Validates the cases file SHA-256 against retrieval and asset-audit artifacts when those artifacts provide a dataset hash. Duplicate case IDs are rejected to keep attribution deterministic.
- Emits only the specified privacy-safe fields: run metadata, dataset hash, counts, per-case IDs/categories/gold-ID hashes/returned IDs/reason/whitelisted diagnostics/labels, and unresolved manual-review case IDs. Question text, patient text, evidence text, and embeddings are never copied.
- Applies deterministic precedence: infrastructure/retrieval artifact failures, permission/lifecycle failures, answerable empty results, rejection non-empty results, returned results without a gold ID, explicit annotation/corpus markers, then manual review.
- Whitelists retrieval gate and safety diagnostics and redacts arbitrary reason/diagnostic text.
- Added focused tests covering retrieval miss, gate false reject, gate false accept, permission/lifecycle, infrastructure, manual review, privacy, hash validation, and named-config selection.

## Tests

- `.venv/Scripts/python.exe -m pytest tests/eval/test_retrieval_failure_attribution.py -q` — **3 passed**
- `.venv/Scripts/python.exe -m pytest tests/eval -q` — **29 passed**
- `.venv/Scripts/ruff.exe check scripts/retrieval_failure_attribution.py tests/eval/test_retrieval_failure_attribution.py` — **All checks passed**

## Concerns

- `gold_evidence_sha256` is the SHA-256 of a canonical JSON array containing sorted, de-duplicated gold evidence IDs. The raw IDs remain out of the report.
- Retrieval results without a matching case row are treated as infrastructure failures; missing result rows are attributed the same way instead of being silently counted as gate rejects.
