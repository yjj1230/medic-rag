# Document Lifecycle Dedup and Incremental Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为患者上传文档增加文件级去重、逻辑版本管理和 Chunk 级增量索引，同时保证旧版本可追溯、更新失败可回滚、检索索引最终一致。

**Architecture:** 保留现有 `Document -> DocumentVersion -> ParseJob -> EvidenceUnit -> IndexJob` 链路，把 `Document` 明确定义为逻辑文档、`DocumentVersion` 定义为文件快照。上传阶段用文件 SHA-256 做幂等；解析完成后用 Chunk 指纹做差异；新版本只有在全部索引任务成功后才原子切换为当前版本。Embedding 通过独立缓存复用，索引更新继续使用现有 Outbox、租约、重试和对账机制。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Alembic, SQLite/PostgreSQL, Celery/Redis, OpenSearch, pytest。

**Spec:** `docs/superpowers/specs/2026-10-13-document-lifecycle-dedup-design.md`

## Global Constraints

- 不改变公共知识库现有 `source_id + content_hash` 增量更新语义。
- 患者数据去重键必须包含 `tenant_id` 和 `patient_id`，禁止跨租户/跨患者复用。
- 更新失败时旧当前版本继续可检索；不得先切换数据库状态再等待索引。
- 旧版本、审计事件和删除墓碑必须保留；物理清理通过现有分层删除和可重试机制完成。
- 每个任务必须先写失败测试，再实现最小改动，再运行针对性测试和相关回归测试。

---

### Task 1: 扩展数据库模型和迁移

**Files:**
- Modify: `src/medical_rag/domain/models.py` (`Document`, `DocumentVersion`, `EvidenceUnit`)
- Create: `alembic/versions/0011_document_version_identity.py`
- Create: `alembic/versions/0012_embedding_cache.py`
- Test: `tests/contract/test_document_version_identity.py`

**Interfaces:**
- `Document.current_version_id: UUID | None`
- `DocumentVersion.storage_key: str`
- `DocumentVersion.file_hash: str`
- `DocumentVersion.file_size_bytes: int`
- `DocumentVersion.is_current: bool`
- `DocumentVersion.superseded_at: datetime | None`
- `EvidenceUnit.chunk_fingerprint: str | None`
- 新 `EmbeddingCache`：`chunk_fingerprint`, `embedding_model`, `embedding_dimensions`, `vector`, `created_at`，并建立三字段唯一约束。

- [ ] **Step 1: Write the failing test**

```python
def test_same_patient_cannot_have_duplicate_file_hash(session, patient_context):
    first = make_version(session, patient_context, file_hash="abc")
    session.flush()
    second = make_version(session, patient_context, file_hash="abc")
    session.add(second)
    with pytest.raises(IntegrityError):
        session.flush()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/contract/test_document_version_identity.py::test_same_patient_cannot_have_duplicate_file_hash -q`

Expected: FAIL because the new identity columns and constraint do not exist.

- [ ] **Step 3: Implement the minimal schema change**

Add nullable columns first for migration compatibility, backfill existing versions with deterministic hashes where source bytes are available, then make new writes non-null through application validation. Add unique constraints scoped by tenant, patient, and hash; add a partial/filtered current-version uniqueness strategy supported by the deployed database dialect.

- [ ] **Step 4: Run the focused tests**

Run: `pytest tests/contract/test_document_version_identity.py -q`

Expected: PASS, including cross-patient same-hash acceptance and one-current-version enforcement.

- [ ] **Step 5: Commit**

```bash
git add src/medical_rag/domain/models.py alembic/versions/0011_document_version_identity.py alembic/versions/0012_embedding_cache.py tests/contract/test_document_version_identity.py
git commit -m "feat: add document version identity and embedding cache schema"
```

### Task 2: Add exact-file deduplication to upload

**Files:**
- Modify: `src/medical_rag/ingest/upload_service.py`
- Modify: `src/medical_rag/api/routes/documents.py`
- Modify: `src/medical_rag/chat/contracts.py` only if a shared result type is needed
- Test: `tests/unit/test_upload_deduplication.py`
- Test: `tests/contract/test_upload_route_deduplication.py`

**Interfaces:**
- `UploadService.create_document(context, file, *, existing_document_policy="return_duplicate") -> UploadResult | DuplicateUploadResult`
- `DuplicateUploadResult` contains `document_id`, `document_version_id`, `status="duplicate"` and never publishes a parse job.

- [ ] **Step 1: Write failing tests** for repeated identical bytes, same bytes across patients, and a retry with the same client request ID.
- [ ] **Step 2: Run:** `pytest tests/unit/test_upload_deduplication.py -q` and confirm failures.
- [ ] **Step 3: Implement** streaming SHA-256 while reading the already size-limited upload, query by tenant/patient/hash, return the existing ready/processing version for a duplicate, and preserve the existing object-store/database rollback on new uploads.
- [ ] **Step 4: Handle races** by catching the database unique-constraint error, rolling back the losing insert, and re-reading the winning version before returning `duplicate`.
- [ ] **Step 5: Update the route response** to expose `status`, `document_id`, `document_version_id`, and `duplicate_of` without exposing file contents.
- [ ] **Step 6: Run:** `pytest tests/unit/test_upload_deduplication.py tests/contract/test_upload_route_deduplication.py -q`.
- [ ] **Step 7: Commit** with `feat: make document upload idempotent by file hash`.

### Task 3: Add explicit logical-document version creation

**Files:**
- Modify: `src/medical_rag/api/routes/documents.py`
- Modify: `src/medical_rag/ingest/upload_service.py`
- Modify: `src/medical_rag/domain/enums.py` if a `BUILDING`/`SUPERSEDED` status is needed
- Create: `tests/unit/test_document_version_upload.py`
- Create: `tests/contract/test_document_version_route.py`

**Interfaces:**
- `UploadService.create_version(context, document_id, file, *, expected_current_version: int | None) -> UploadResult`
- `POST /v1/documents/{document_id}/versions`
- The endpoint must verify tenant/patient ownership and use a row lock or dialect-safe compare-and-swap on the current version number.

- [ ] **Step 1: Write failing tests** for version 2 creation, stale expected version rejection, unauthorized patient access, and duplicate bytes against an existing version.
- [ ] **Step 2: Run:** `pytest tests/unit/test_document_version_upload.py tests/contract/test_document_version_route.py -q` and confirm failures.
- [ ] **Step 3: Move version-owned storage metadata** to `DocumentVersion.storage_key`; keep a compatibility read path for the existing `Document.storage_key` until migration is complete.
- [ ] **Step 4: Implement** version-number allocation, `is_current=false`, `status=PROCESSING`, version-scoped parse job creation, and a version-specific object-store key.
- [ ] **Step 5: Run focused tests** and verify a new version does not change `Document.current_version_id` before indexing succeeds.
- [ ] **Step 6: Commit** with `feat: add explicit document version upload`.

### Task 4: Compute stable Chunk fingerprints and produce a diff

**Files:**
- Modify: `src/medical_rag/parsing/service.py`
- Modify: `src/medical_rag/parsing/contracts.py`
- Modify: `src/medical_rag/knowledge/semantic_chunker.py` only if the fingerprint needs canonical metadata
- Create: `src/medical_rag/knowledge/chunk_diff.py`
- Test: `tests/unit/test_chunk_diff.py`
- Test: `tests/eval/test_provenance_roundtrip.py` (extend existing provenance assertions)

**Interfaces:**
- `compute_chunk_fingerprint(draft: EvidenceUnitDraft, *, parser_version: str, cleaner_version: str, chunker_version: str) -> str`
- `diff_chunks(previous: Sequence[EvidenceUnit], current: Sequence[EvidenceUnitDraft]) -> ChunkDiff`
- `ChunkDiff`: `unchanged`, `added`, `removed`, `metadata_changed` lists keyed by stable chunk fingerprint and source locator.

- [ ] **Step 1: Write failing tests** for unchanged text, changed text, removed chunks, duplicate text at different anchors, and locator-only changes.
- [ ] **Step 2: Run:** `pytest tests/unit/test_chunk_diff.py -q` and confirm failures.
- [ ] **Step 3: Implement canonicalization** using normalized clean text, section path, parent block identity, source anchor, and pipeline versions; do not hash raw object representations or nondeterministic JSON ordering.
- [ ] **Step 4: Implement multiset-safe matching** so two identical paragraphs at different locations do not collapse into one chunk.
- [ ] **Step 5: Run focused tests** and verify provenance fields remain attached to every emitted draft.
- [ ] **Step 6: Commit** with `feat: add stable chunk fingerprints and diffing`.

### Task 5: Reuse Embeddings and create version-scoped index jobs

**Files:**
- Modify: `src/medical_rag/models/contracts.py`
- Modify: `src/medical_rag/models/factory.py` only for cache composition
- Modify: `src/medical_rag/parsing/service.py`
- Modify: `src/medical_rag/indexing/orchestrator.py`
- Create: `src/medical_rag/models/embedding_cache.py`
- Test: `tests/unit/test_embedding_cache.py`
- Test: `tests/contract/test_index_provenance_contract.py`

**Interfaces:**
- `EmbeddingCache.get(fingerprint, model, dimensions) -> tuple[float, ...] | None`
- `EmbeddingCache.put(fingerprint, model, dimensions, vector) -> None`
- `EmbeddingPipeline.embed_records_cached(records, model_version) -> list[tuple[float, ...]]`
- `IndexJob` idempotency must include `document_version_id`, chunk identity, pipeline versions, embedding model, and index version.

- [ ] **Step 1: Write failing tests** for cache hit, cache miss, dimension mismatch, and changed embedding model invalidation.
- [ ] **Step 2: Run:** `pytest tests/unit/test_embedding_cache.py -q` and confirm failures.
- [ ] **Step 3: Implement** cache validation and batch lookup; reject vectors with the wrong dimensions instead of silently reusing them.
- [ ] **Step 4: Update parsing/index-job creation** so unchanged chunks can reuse vectors while added or changed chunks create new embedding work.
- [ ] **Step 5: Run:** `pytest tests/unit/test_embedding_cache.py tests/contract/test_index_provenance_contract.py -q`.
- [ ] **Step 6: Commit** with `feat: reuse embeddings across document versions`.

### Task 6: Activate versions atomically and remove stale private index records

**Files:**
- Modify: `src/medical_rag/parsing/service.py`
- Modify: `src/medical_rag/indexing/orchestrator.py`
- Modify: `src/medical_rag/retrieval/opensearch_writer.py`
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Modify: `src/medical_rag/retrieval/contracts.py`
- Create: `tests/integration/test_document_version_activation.py`

**Interfaces:**
- `VersionActivationService.activate_if_complete(document_version_id: UUID) -> bool`
- `PrivateIndexWriter.delete_version(document_version_id: UUID) -> None`
- OpenSearch records must carry `document_version_id`, `is_current`, `retrieval_eligible`, and `deleted`.

- [ ] **Step 1: Write failing integration tests** for successful activation, partial index failure, removed chunk cleanup, and old-version exclusion from retrieval.
- [ ] **Step 2: Run:** `pytest tests/integration/test_document_version_activation.py -q` and confirm failures.
- [ ] **Step 3: Implement** an activation check that requires parse success, quality eligibility policy, and zero pending/retryable index jobs before flipping `Document.current_version_id`.
- [ ] **Step 4: Implement** version-level stale record deletion through the private writer; retain a retryable Outbox event when deletion fails.
- [ ] **Step 5: Update retrieval filters** to require the current version and active lifecycle flags, with a compatibility path for legacy rows.
- [ ] **Step 6: Run the integration test and the existing deletion/reconciliation tests**.
- [ ] **Step 7: Commit** with `feat: atomically activate indexed document versions`.

### Task 7: Add lifecycle observability, reconciliation, and migration tooling

**Files:**
- Modify: `src/medical_rag/indexing/reconciliation.py`
- Modify: `src/medical_rag/observability/stage_metrics.py`
- Create: `scripts/backfill_document_hashes.py`
- Create: `scripts/rebuild_document_versions.py`
- Test: `tests/eval/test_document_lifecycle_reconciliation.py`
- Modify: `docs/e2e_test_plan_rag.md`

**Interfaces:**
- Reconciliation reasons: `duplicate_version`, `stale_current_version`, `missing_private_record`, `orphan_private_record`, `chunk_fingerprint_mismatch`.
- Metrics: `document_duplicate_upload`, `document_version_created`, `document_version_activated`, `document_version_activation_failed`, `chunk_reused`, `chunk_reindexed`, `chunk_deactivated`.

- [ ] **Step 1: Write failing reconciliation tests** for an old active version, a missing index record, and an orphan private record.
- [ ] **Step 2: Run:** `pytest tests/eval/test_document_lifecycle_reconciliation.py -q` and confirm failures.
- [ ] **Step 3: Implement** read-only scan output first, then repair actions that reuse the existing leased/indexed writer seams and never include patient text in lifecycle payloads.
- [ ] **Step 4: Implement** the hash backfill script with dry-run and deterministic progress output; do not change current-version selection during backfill.
- [ ] **Step 5: Document** rollback, retry, and operational metrics in `docs/e2e_test_plan_rag.md`.
- [ ] **Step 6: Run focused tests and the existing reconciliation/provenance suites**.
- [ ] **Step 7: Commit** with `feat: add document lifecycle reconciliation and metrics`.

### Task 8: End-to-end validation and rollout gates

**Files:**
- Modify: `tests/eval/test_provenance_roundtrip.py`
- Modify: `tests/contract/test_index_provenance_contract.py`
- Create: `tests/e2e/test_document_update_flow.py`
- Modify: `docs/e2e_test_plan_rag.md`

**Interfaces:**
- E2E scenarios: exact duplicate upload, new version upload, changed/unchanged chunk mix, failed update rollback, deletion retry, cross-patient same-file acceptance, and concurrent duplicate uploads.

- [ ] **Step 1: Add E2E tests** that assert database state, object-store keys, index records, current-version selection, citations, and diagnostics.
- [ ] **Step 2: Run:** `pytest tests/e2e/test_document_update_flow.py -q`.
- [ ] **Step 3: Run the full relevant suite:** `pytest tests/unit tests/contract tests/eval tests/integration -q`.
- [ ] **Step 4: Run migration and startup checks** against a fresh database and a copy of the current local database.
- [ ] **Step 5: Define rollout gates:** first enable exact-file deduplication, then version upload, then chunk diff/embedding cache, and finally automatic current-version activation.
- [ ] **Step 6: Commit** with `test: cover document deduplication and incremental update lifecycle`.

## Rollout order

1. Schema and backfill support.
2. Exact-file deduplication; this is low-risk and immediately prevents duplicate work.
3. Explicit version upload; keep old upload behavior as “new logical document” until clients send a version target.
4. Atomic version activation and stale index cleanup.
5. Chunk diff and embedding cache; enable metrics before turning on aggressive reuse.
6. Reconciliation repair and operational rollout gates.

## Acceptance criteria

- Uploading identical bytes for the same patient returns the existing version and creates no new parse/index job.
- The same bytes for two different patients are stored independently.
- A revised report creates version 2 without making version 1 disappear from audit/history.
- Failed version 2 indexing leaves version 1 as the current retrievable version.
- Unchanged chunks are counted as reused; changed and added chunks are reindexed; removed chunks are not retrievable.
- Retrieval never returns a superseded, deleted, ineligible, or cross-patient private chunk.
- Existing public knowledge ingestion tests and deletion/reconciliation tests remain green.
