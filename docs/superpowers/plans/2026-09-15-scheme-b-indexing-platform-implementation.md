# Scheme B Indexing Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the current RAG indexing skeleton into a recoverable, auditable, versioned indexing platform without rewriting the modular monolith.

**Architecture:** PostgreSQL remains the source of truth. A transactional Outbox drives an IndexOrchestrator and durable IndexJob state machine. Public and private OpenSearch writers share a normalized record contract but use separate physical indexes and aliases; retrieval remains ACL-first BM25/KNN + RRF.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, SQLite/PostgreSQL tests, OpenSearch adapter, pytest.

**Spec:** `docs/superpowers/specs/2026-09-15-scheme-b-indexing-platform-design.md`

## Global Constraints

- PostgreSQL is the business source of truth; OpenSearch is rebuildable derived state.
- Public and patient-private evidence use separate physical indexes, writers, aliases, and authorization paths.
- ACL and lifecycle filters run before RRF, rerank, and prompt assembly.
- Deleted, inactive, and review-required evidence is never retrievable.
- Embedding writes fail closed unless an explicit deterministic test switch is used.
- External model calls remain behind consent and audit gateways.
- No query rewrite, session memory, or autonomous Agent behavior is introduced.
- No production alias is switched automatically by tests.

### Task 1: Normalize index contracts and lifecycle metadata

**Files:**
- Modify: `src/medical_rag/knowledge/contracts.py`
- Modify: `src/medical_rag/knowledge/ingest_service.py`
- Modify: `src/medical_rag/retrieval/contracts.py`
- Modify: `src/medical_rag/retrieval/indexing.py`
- Test: `tests/contract/test_index_record_contract.py`
- Test: `tests/integration/test_knowledge_ingest.py`

**Interfaces:**
- `IndexRecord` carries shared identity, ACL, lifecycle, source, embedding, and index-version metadata.
- `PublicIndexWriter.upsert(records)` and `PrivateIndexWriter.upsert(records)` accept normalized records.
- Public writes always serialize explicit `visibility`, `retrieval_eligible`, `source_active`, and `deleted` fields.

- [ ] Add failing contract tests for required metadata and public defaults.
- [ ] Implement the normalized record fields while preserving existing positional constructors.
- [ ] Update public and private adapters to serialize the fields explicitly.
- [ ] Run the focused contract and knowledge-ingest tests.

### Task 2: Add transactional Outbox and lease-based IndexJob state

**Files:**
- Modify: `src/medical_rag/domain/models.py`
- Modify: `src/medical_rag/domain/enums.py`
- Create: `alembic/versions/0004_add_index_outbox.py`
- Create: `src/medical_rag/indexing/outbox.py`
- Modify: `src/medical_rag/retrieval/indexing.py`
- Test: `tests/integration/test_index_outbox.py`

**Interfaces:**
- `OutboxEvent(event_type, aggregate_id, payload, status, attempts, next_retry_at)`.
- `IndexJob.lease(worker_id, now, lease_seconds) -> bool`.
- `IndexJob.retry(error_code, now) -> None`.

- [ ] Write failing tests for atomic creation, lease exclusivity, retry scheduling, and duplicate delivery.
- [ ] Add the Outbox model/migration and additive IndexJob lease/retry fields.
- [ ] Implement deterministic SQLite repository behavior and worker recovery for expired leases.
- [ ] Run the outbox and private-index integration tests.

### Task 3: Implement production OpenSearch writers and mappings

**Files:**
- Create: `src/medical_rag/retrieval/opensearch_writer.py`
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Modify: `src/medical_rag/api/dependencies.py`
- Test: `tests/contract/test_opensearch_writer.py`

**Interfaces:**
- `OpenSearchIndexWriter(index_name, alias, client).upsert(records) -> WriteReceipt`.
- `OpenSearchIndexWriter.delete_document(document_id) -> None`.
- `OpenSearchIndexWriter.ensure_mapping() -> None`.

- [ ] Add failing tests for mappings, bulk upsert, idempotent IDs, document deletion, and alias-safe index names.
- [ ] Implement separate public/private writer instances with explicit mappings and lifecycle fields.
- [ ] Keep retrieval store responsible only for candidate reads and source-index validation.
- [ ] Add dependency factories that fail closed when production writers are absent.
- [ ] Run writer contract tests without requiring a live cluster.

### Task 4: Build IndexOrchestrator and mandatory production composition

**Files:**
- Create: `src/medical_rag/indexing/orchestrator.py`
- Modify: `src/medical_rag/parsing/service.py`
- Modify: `src/medical_rag/parsing/worker.py`
- Modify: `src/medical_rag/api/app.py`
- Modify: `src/medical_rag/api/dependencies.py`
- Test: `tests/integration/test_index_orchestrator.py`

**Interfaces:**
- `IndexOrchestrator.run_once(worker_id, limit) -> list[IndexReceipt]`.
- `IndexOrchestrator.process(job_id, worker_id) -> IndexReceipt`.
- `ProductionComposition.validate() -> None`.

- [ ] Add failing tests for outbox consumption, public/private writer selection, embedding metadata pinning, and audit emission.
- [ ] Implement the orchestrator around the existing IndexWorker and lease/retry semantics.
- [ ] Pass audit context and fixed embedding metadata through asynchronous execution.
- [ ] Add startup validation for writers, embedding, consent, and audit configuration.
- [ ] Run orchestrator and parsing-worker integration tests.

### Task 5: Complete lifecycle deletion, deactivation, and reconciliation

**Files:**
- Create: `src/medical_rag/indexing/reconciliation.py`
- Modify: `src/medical_rag/ingest/delete_service.py`
- Modify: `src/medical_rag/knowledge/ingest_service.py`
- Modify: `src/medical_rag/audit/service.py`
- Test: `tests/integration/test_index_reconciliation.py`

**Interfaces:**
- `ReconciliationService.scan(scope, limit) -> ReconciliationReport`.
- `ReconciliationService.repair(item) -> RepairReceipt`.
- `DeleteService` never emits completion when any configured layer failed.

- [ ] Add failing tests for source deactivation, deleted-version cleanup, stale-index detection, and safe repair.
- [ ] Emit lifecycle outbox events for delete, version replacement, and source deactivation.
- [ ] Implement DB-vs-index reconciliation using hashes/version metadata, never patient text.
- [ ] Add metrics/audit for residuals and repair outcomes.
- [ ] Run lifecycle and reconciliation integration tests.

### Task 6: Calibrate quality gates and staged rollout verification

**Files:**
- Modify: `src/medical_rag/observability/metrics.py`
- Modify: `src/medical_rag/retrieval/service.py`
- Create: `tests/integration/test_scheme_b_rollout.py`
- Create: `tests/contract/test_live_index_requirements.py`
- Modify: `knowledge/current-rag-indexing-flow.md`

**Interfaces:**
- `CalibrationArtifact(index_version, embedding_model, rrf_k, threshold, eval_dataset_version)`.
- `RolloutGate.compare(baseline, candidate) -> RolloutDecision`.

- [ ] Add failing tests for threshold-artifact matching, zero-tolerance security gates, and alias non-mutation.
- [ ] Implement artifact-bound fused threshold/rank gate and explicit rollback decision output.
- [ ] Add OpenSearch live-contract checks for KNN+ACL filter and index provenance.
- [ ] Run full unit/integration/eval suites and record the baseline comparison.
- [ ] Document deployment prerequisites and rollback steps.

## Execution Order

```text
Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6
```
