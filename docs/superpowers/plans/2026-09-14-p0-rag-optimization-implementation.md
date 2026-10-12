# P0 RAG Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved P0 design so patient reports are indexed privately, real embeddings power queries, BM25/KNN retrieval is fused measurably, and quality/security regressions are evaluated automatically.

**Architecture:** Keep the existing modular monolith. Add an explicit private index writer and durable index job seam, a reusable embedding pipeline, separate lexical/vector candidate retrieval with RRF fusion, and an evaluation/diagnostics layer. Do not add autonomous Agent behavior in P0.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL/SQLite tests, OpenSearch adapter, Redis/Celery, httpx, pytest.

**Spec:** `docs/superpowers/specs/2026-09-14-p0-rag-optimization-design.md`

## Global Constraints

- Public and patient-private evidence use separate index and authorization paths.
- ACL filters run before reranking and prompt assembly.
- External model calls remain behind `ModelGateway` and consent/audit checks.
- Deleted, inactive, and review-required evidence must not be retrievable.
- Existing medical safety behavior must remain unchanged.
- No task may claim quality improvement without a repeatable evaluation result.

---

### Task 1: Add deterministic evaluation fixtures and diagnostics

**Files:**
- Create: `tests/eval/cases.jsonl`
- Create: `tests/eval/test_rag_quality.py`
- Create: `src/medical_rag/observability/metrics.py`
- Modify: `src/medical_rag/retrieval/contracts.py`

**Interfaces:**
- `EvaluationCase.from_json(data) -> EvaluationCase`
- `evaluate_retrieval(case, evidence_ids) -> RetrievalMetrics`
- `RetrievalResult.diagnostics: dict[str, object]`

- [ ] Add synthetic cases for public, private, no-evidence, deleted, cross-patient, and bilingual retrieval.
- [ ] Add deterministic tests for hit-rate, Recall@k, MRR, permission violations, and deletion residuals.
- [ ] Extend `RetrievalResult` with diagnostics while preserving existing callers.
- [ ] Run `pytest tests/eval -q` and record the baseline before changing retrieval behavior.

### Task 2: Add durable private index records and index jobs

**Files:**
- Modify: `src/medical_rag/retrieval/contracts.py`
- Modify: `src/medical_rag/domain/enums.py`
- Modify: `src/medical_rag/domain/models.py`
- Create: `alembic/versions/0003_add_index_jobs.py`
- Create: `src/medical_rag/retrieval/indexing.py`
- Test: `tests/integration/test_private_indexing.py`

**Interfaces:**
- `PrivateIndexRecord(...)`
- `PrivateIndexWriter.upsert(records) -> None`
- `PrivateIndexWriter.delete_document(document_id) -> None`
- `IndexJob(status, document_version_id, index_version, attempts, error_code)`

- [ ] Write failing tests proving a private record carries tenant, patient, document, version, anchor, confidence, and eligibility fields.
- [ ] Add the `IndexJob` persistence model and migration with retry/status fields.
- [ ] Add an explicit writer protocol and deterministic in-memory adapter.
- [ ] Add idempotency keyed by document version, evidence unit, parser version, and embedding model.
- [ ] Run the new integration test against the SQLite schema.

### Task 3: Connect parsing output to private indexing and review gating

**Files:**
- Modify: `src/medical_rag/parsing/service.py`
- Modify: `src/medical_rag/parsing/worker.py`
- Modify: `src/medical_rag/domain/enums.py`
- Modify: `src/medical_rag/retrieval/indexing.py`
- Test: `tests/integration/test_private_indexing.py`
- Test: `tests/integration/test_document_lifecycle.py`

**Interfaces:**
- `ParsingService(..., private_index_writer=None, index_job_factory=None)`
- `IndexWorker.run(job_id) -> IndexReceipt`

- [ ] Add a failing test showing parsed evidence creates an index job for the same document version.
- [ ] Add a failing test showing `NEEDS_REVIEW` evidence is stored but `retrieval_eligible=False`.
- [ ] Persist evidence/facts and index job in one database transaction.
- [ ] Make `IndexWorker` load evidence metadata, create index records, and mark the job succeeded only after writer success.
- [ ] Ensure delete flow calls the private writer and records failed layers for retry.
- [ ] Run parsing, indexing, and lifecycle tests.

### Task 4: Wire real embeddings for ingestion and queries

**Files:**
- Modify: `src/medical_rag/core/config.py`
- Modify: `src/medical_rag/knowledge/contracts.py`
- Modify: `src/medical_rag/models/contracts.py`
- Modify: `src/medical_rag/knowledge/ingest_service.py`
- Modify: `src/medical_rag/retrieval/service.py`
- Modify: `src/medical_rag/api/dependencies.py`
- Test: `tests/contract/test_model_provider_contract.py`
- Test: `tests/unit/test_embedding_pipeline.py`

**Interfaces:**
- `EmbeddingPipeline.embed_records(texts, model_version) -> list[tuple[float, ...]]`
- `RetrievalService(..., embedding_model=None, embedding_model_name="...")`

- [ ] Add failing tests for batch-size preservation, vector count mismatch, dimension mismatch, and bounded retry behavior.
- [ ] Add embedding model/version/dimension/batch settings.
- [ ] Batch-embed public and private records before index upsert; persist model/version metadata.
- [ ] Generate one query embedding before vector retrieval and pass it through `SearchQuery.embedding`.
- [ ] Do not make `OpenSearchStore` call the embedding provider.
- [ ] Run contract and embedding unit tests.

### Task 5: Implement separate BM25/KNN retrieval, RRF, and rerank

**Files:**
- Modify: `src/medical_rag/retrieval/contracts.py`
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Modify: `src/medical_rag/retrieval/service.py`
- Create: `src/medical_rag/retrieval/ranking.py`
- Test: `tests/contract/test_retrieval_store.py`
- Modify: `tests/unit/test_retrieval_service.py`

**Interfaces:**
- `RetrievalFilters(...)`
- `CandidateRetriever.search_lexical(text, filters, limit) -> list[SearchRecord]`
- `CandidateRetriever.search_vector(embedding, filters, limit) -> list[SearchRecord]`
- `HybridRanker.fuse(lexical, vector, limit) -> list[SearchRecord]`

- [ ] Add failing tests for ACL filtering before rerank and deterministic RRF ordering.
- [ ] Split OpenSearch lexical and KNN requests while preserving public/private filters.
- [ ] Implement RRF without comparing raw BM25 and vector score scales.
- [ ] Rerank only the authorized fused candidates and cap candidate counts.
- [ ] Replace the fixed uncalibrated `min_score` behavior with an evaluated fused threshold or rank gate.
- [ ] Add diagnostics for candidate counts, ranks, scores, index version, and elapsed time.
- [ ] Run retrieval unit and contract tests.

### Task 6: Regression, deletion, and rollout verification

**Files:**
- Create: `tests/integration/test_delete_residuals.py`
- Create: `tests/integration/test_p0_rollout.py`
- Modify: `src/medical_rag/ingest/delete_service.py`
- Modify: `src/medical_rag/audit/service.py`

- [ ] Verify same-patient report retrieval after parse and index jobs complete.
- [ ] Verify cross-patient and cross-tenant records never reach rerank or model prompts.
- [ ] Verify deleted, inactive, and review-required records are excluded from both lexical and vector paths.
- [ ] Verify index job retries and failure diagnostics are observable.
- [ ] Run `pytest -q` and `pytest tests/eval -q`.
- [ ] Compare P0 metrics against the saved baseline before enabling the new index alias.

## Execution Order

```text
Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6
```

Each task must pass its focused tests before the next task starts. The implementation stops if any authorization, deletion, or medical-safety regression appears.

