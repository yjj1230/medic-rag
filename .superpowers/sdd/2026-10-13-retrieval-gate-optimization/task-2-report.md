# Task 2 report: two-level retrieval gate

Implemented the optional vector-only cosine threshold and candidate partitioning.

## Changes

- Added `CalibrationArtifact.vector_only_cosine_threshold`.
  - Must be numeric and within `[0, 1]`.
  - Cannot be supplied without `cosine_threshold` and its score space.
  - Legacy artifacts with the field omitted remain valid.
- Added `RetrievalService.vector_only_cosine_threshold` and fallback tracking.
- Applied the gate after ACL, lifecycle, and citation filtering:
  - lexical/vector intersection uses the base cosine threshold;
  - vector-only candidates use the stricter threshold;
  - lexical-only candidates are removed before RRF;
  - surviving candidates retain their input ordering for RRF.
- Added privacy-safe diagnostics:
  `vector_only_candidates`, `vector_only_gated_out`,
  `lexical_vector_intersection`, `vector_only_gate_threshold`, and
  `vector_only_threshold_fallback`.
- Preserved `vector_path_unavailable` behavior.
- Added `vector_only_cosine_threshold: null` to the checked-in 2026-10-13 artifact so it remains structurally valid while Task 4 determines a calibrated value.

## Tests

- `.venv/Scripts/python.exe -m pytest tests/eval/test_two_level_gate.py -q` — 4 passed.
- `.venv/Scripts/python.exe -m pytest tests/test_rejection_gate.py -q` — 37 passed.
- `.venv/Scripts/python.exe -m pytest tests/test_startup_gate_selfcheck.py -q` — 5 passed.
- `.venv/Scripts/ruff.exe check src/medical_rag/observability/metrics.py src/medical_rag/retrieval/service.py tests/test_rejection_gate.py tests/eval/test_two_level_gate.py` — passed.

## Concerns

The checked-in vector-only field is intentionally `null`; no frozen test data was used to choose a production threshold. Task 4 should populate it from the development scan.
