# Task 1 report: retrieval score-space identity contract

## Status

Implemented and committed the score-space identity contract for OpenSearch retrieval.

- Mapping discovery reads only `embedding.method.space_type`.
- Accepted spaces are `l2_inv` and `cosinesimil`.
- Missing, malformed, unsupported, or unavailable mappings resolve to `mapping_unavailable`; no OpenSearch default is guessed.
- An explicit `OpenSearchStore(vector_score_space=...)` value is validated and retained.
- Vector `SearchRecord` instances inherit the resolved mapping space; unknown mapping leaves the record space unset.
- Strict cosine gates fail closed when mapping space or record space does not match the configured gate space.
- Vector backend failures continue to use `gate_skipped_reason=vector_path_unavailable`.
- Diagnostics expose `mapping_score_space` and `score_space_match` and contain only identities, counts, scores, and status fields.

## Tests

Focused command:

`.venv/Scripts/python.exe -m pytest tests/eval/test_retrieval_score_space.py tests/test_rejection_gate.py -q`

Result: **37 passed**.

Additional evaluation tests:

`.venv/Scripts/python.exe -m pytest tests/eval -q`

Result: **20 passed**.

Ruff:

`.venv/Scripts/ruff.exe check src/medical_rag/retrieval/opensearch_store.py src/medical_rag/retrieval/service.py tests/test_rejection_gate.py tests/eval/test_retrieval_score_space.py`

Result: **All checks passed**.

## Commits

- `a4d4c86` — `fix retrieval score-space identity contract`
- `1f07c27` — `docs: add score-space task report`

## Concerns

The existing in-memory/fake retrieval seams do not expose physical mapping metadata. For those seams, the service preserves compatibility by inferring a single annotated record space; production `OpenSearchStore` always exposes the mapping resolver and therefore fails closed on unavailable mapping metadata.
