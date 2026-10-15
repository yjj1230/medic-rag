# Chunk Provenance Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every medical RAG chunk traceable from an indexed hit to its exact document version and source position across parsing, indexing, retrieval, citation, deletion, and reconciliation.

**Architecture:** Introduce one shared structured locator and provenance contract, then thread it through the existing parser-to-index pipeline. Keep `location` as a derived display field for compatibility, while IDs, hashes, versions, and structured coordinates become the authoritative data used by citations, deletion, and reconciliation.

**Tech Stack:** Python 3.12, dataclasses, SQLAlchemy, Alembic, FastAPI, OpenSearch, pytest, existing Celery/Redis indexing pipeline.

**Spec:** `docs/superpowers/specs/2026-10-12-chunk-provenance-design.md`

**Execution status (2026-10-13):** Core locator contract, parser/chunker propagation, database model/migration, public/private index serialization, citation API fields, reconciliation metadata, backfill/audit scripts, and round-trip tests have been implemented. The migration is verified on SQLite and on a real PostgreSQL 16.15 instance with Python 3.12 through upgrade, downgrade, and re-upgrade. PostgreSQL validation also required shortening the `0009` and `0010` Alembic revision IDs to stay within PostgreSQL's 32-character `alembic_version` limit.

## Global Constraints

- Preserve existing public/private index separation and fail-closed authorization behavior.
- Do not invent page numbers, OCR confidence, or source anchors when the parser cannot provide them.
- Keep existing nullable columns and legacy `location` behavior readable during migration.
- Use the existing version fields and content hashes for rebuild identity; do not introduce a second idempotency scheme.
- Do not put PHI into logs, test fixtures, or new diagnostics.
- Restore the missing test suite before claiming the historical full regression count.

---

### Task 1: Freeze the current baseline and provenance inventory

**Files:**
- Read: `docs/RAG恢复清单-2026-10-12.md`
- Read: `src/medical_rag/parsing/contracts.py`
- Read: `src/medical_rag/knowledge/contracts.py`
- Read: `src/medical_rag/domain/models.py`
- Read: `src/medical_rag/retrieval/contracts.py`
- Test: `tests/contract/test_provenance_baseline.py`

**Interfaces:**
- Produces a field inventory mapping each existing provenance field to its source, persistence column, index field, and response field.

- [ ] **Step 1: Restore or confirm the baseline tests**

Run:

```powershell
pytest -q
```

Expected: record the actual current result. Do not use the historical “639 passed” statement unless the missing tests have been restored and the command reproduces it.

- [ ] **Step 2: Add a baseline contract test**

The test should assert that the current shared record exposes `document_id`, `document_version_id`, `evidence_unit_id`, `location`, `page_number`, `section`, `sheet_name`, `cell_range`, `source_anchor`, `content_hash`, `parser_version`, `cleaner_version`, `chunker_version`, and `index_version`.

- [ ] **Step 3: Document lossy boundaries**

Record every constructor that drops `sheet_name`, `cell_range`, `source_anchor`, `section_path`, `parent_block_id`, or `chunk_index`, especially `KnowledgeChunkDraft`, `PublicIndexRecord`, and the public ingest path.

- [ ] **Step 4: Run the focused baseline test**

Run:

```powershell
pytest tests/contract/test_provenance_baseline.py -q
```

- [ ] **Step 5: Commit the inventory and baseline test**

```powershell
git add tests/contract/test_provenance_baseline.py docs/superpowers/specs/2026-10-12-chunk-provenance-design.md
git commit -m "test: freeze chunk provenance baseline"
```

### Task 2: Add the shared `SourceLocator` contract

**Files:**
- Create: `src/medical_rag/provenance/__init__.py`
- Create: `src/medical_rag/provenance/contracts.py`
- Modify: `src/medical_rag/parsing/contracts.py`
- Test: `tests/unit/test_source_locator.py`

**Interfaces:**
- `SourceLocator` is an immutable dataclass with `locator_type`, `page_number`, `page_total`, `section_path`, `block_id`, `reading_order`, `chunk_index`, `char_start`, `char_end`, `bbox`, `sheet_name`, `cell_range`, and `source_anchor`.
- `SourceLocator.to_dict() -> dict[str, object]` returns JSON-safe data.
- `SourceLocator.display_location() -> str` returns a human-readable location.
- `SourceLocator.build_anchor() -> str` returns a deterministic anchor.
- `SourceLocator.validate() -> None` rejects a locator with no usable position fields.

- [ ] **Step 1: Write failing locator tests**

Cover PDF page/block, DOCX section/block, XLSX sheet/range, image page/bbox, JSON serialization, deterministic anchor generation, and rejection of an empty locator.

- [ ] **Step 2: Run the tests and confirm failure**

```powershell
pytest tests/unit/test_source_locator.py -q
```

- [ ] **Step 3: Implement the immutable contract**

Use tuples for `section_path`, `bbox`, and other sequence fields so the object cannot be mutated after persistence. Keep `source_anchor` derived when callers do not provide one.

- [ ] **Step 4: Add conversion helpers**

Implement `locator_from_block(block: ParsedBlock, chunk_index: int | None) -> SourceLocator` and preserve `None` for unavailable page, OCR confidence, or coordinates.

- [ ] **Step 5: Run focused tests**

```powershell
pytest tests/unit/test_source_locator.py tests/unit/test_text_source_parser.py -q
```

- [ ] **Step 6: Commit**

```powershell
git add src/medical_rag/provenance src/medical_rag/parsing/contracts.py tests/unit/test_source_locator.py
git commit -m "feat: add structured source locator contract"
```

### Task 3: Thread the locator through parser and chunk drafts

**Files:**
- Modify: `src/medical_rag/parsing/contracts.py`
- Modify: `src/medical_rag/parsing/service.py`
- Modify: `src/medical_rag/knowledge/semantic_chunker.py`
- Modify: `src/medical_rag/knowledge/contracts.py`
- Test: `tests/unit/test_semantic_chunker_provenance.py`
- Test: `tests/unit/test_parsing_provenance.py`

**Interfaces:**
- `EvidenceUnitDraft` gains `locator: SourceLocator | None` while keeping its legacy location fields during migration.
- `KnowledgeChunkDraft` gains `locator: SourceLocator | None`, `source_id`, and optional `document_id`/`document_version_id`.
- `KnowledgeChunkDraft.from_evidence(...)` copies the locator without reducing it to `section` or `location`.

- [ ] **Step 1: Write failing propagation tests**

Assert that paragraph, table, and oversized-block chunks preserve page, section path, block ID, reading order, chunk index, Sheet, and cell range.

- [ ] **Step 2: Run focused tests and confirm failure**

```powershell
pytest tests/unit/test_semantic_chunker_provenance.py tests/unit/test_parsing_provenance.py -q
```

- [ ] **Step 3: Populate locators at chunk creation**

When `SemanticChunker` emits a draft, derive the locator from the first source block and attach the chunk index and parent block ID. For table children, use the narrowed cell range generated by `_cell_range_for_rows`.

- [ ] **Step 4: Derive legacy fields from the locator**

Populate `page_number`, `sheet_name`, `cell_range`, `source_anchor`, and `location` from the locator. Do not create a second independent value.

- [ ] **Step 5: Run parser and chunk tests**

```powershell
pytest tests/unit/test_semantic_chunker_provenance.py tests/unit/test_parsing_provenance.py tests/unit/test_knowledge_chunker.py -q
```

- [ ] **Step 6: Commit**

```powershell
git add src/medical_rag/parsing src/medical_rag/knowledge tests/unit/test_semantic_chunker_provenance.py tests/unit/test_parsing_provenance.py
git commit -m "feat: preserve locators through parsing and chunking"
```

### Task 4: Persist complete provenance in database models

**Files:**
- Modify: `src/medical_rag/domain/models.py`
- Create: `alembic/versions/0010_add_chunk_provenance.py`
- Test: `tests/integration/test_chunk_provenance_persistence.py`
- Test: `tests/integration/test_migrations.py`

**Interfaces:**
- `EvidenceUnit` stores a JSON-safe `locator` plus `page_total`, `reading_order`, `char_start`, `char_end`, and `bbox` where available.
- `KnowledgeChunk` stores the same locator fields or a JSON locator column according to the existing public knowledge model.
- The database retains `document_version_id` as the authoritative foreign-key path.

- [ ] **Step 1: Write migration and persistence tests**

Cover upgrade from the current schema, downgrade and re-upgrade, round-trip JSON serialization, nullable legacy rows, and indexes on `document_version_id` plus the most common locator query fields.

- [ ] **Step 2: Run migration tests and confirm failure**

```powershell
pytest tests/integration/test_chunk_provenance_persistence.py tests/integration/test_migrations.py -q
```

- [ ] **Step 3: Add the migration**

Use nullable columns for new fields. Do not backfill unknown positions with fake page numbers or anchors. Add an index for `document_version_id` and retain the existing evidence-unit index.

- [ ] **Step 4: Update persistence constructors**

Modify the parsing service and knowledge ingest service so every newly created evidence/chunk row receives the structured locator and derived legacy fields.

- [ ] **Step 5: Run upgrade/downgrade tests**

```powershell
pytest tests/integration/test_chunk_provenance_persistence.py tests/integration/test_migrations.py -q
```

- [ ] **Step 6: Commit**

```powershell
git add src/medical_rag/domain/models.py alembic/versions/0010_add_chunk_provenance.py tests/integration/test_chunk_provenance_persistence.py tests/integration/test_migrations.py
git commit -m "feat: persist structured chunk provenance"
```

### Task 5: Unify public/private index record contracts

**Files:**
- Modify: `src/medical_rag/knowledge/contracts.py`
- Modify: `src/medical_rag/retrieval/contracts.py`
- Modify: `src/medical_rag/retrieval/opensearch_writer.py`
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Modify: `src/medical_rag/knowledge/ingest_service.py`
- Test: `tests/contract/test_index_provenance_contract.py`
- Test: `tests/contract/test_opensearch_provenance_mapping.py`

**Interfaces:**
- `IndexRecord` is the canonical writer payload.
- `PublicIndexRecord.to_index_record()` and private record construction populate the same locator fields.
- `SearchRecord` exposes the locator needed by the chat citation layer.

- [ ] **Step 1: Write contract tests**

Assert that public records cannot contain tenant/patient ACL fields, private records require them, and both visibility types preserve document identity, version identity, locator, content hash, and pipeline versions.

- [ ] **Step 2: Run contract tests and confirm failure**

```powershell
pytest tests/contract/test_index_provenance_contract.py tests/contract/test_opensearch_provenance_mapping.py -q
```

- [ ] **Step 3: Extend public record conversion**

Pass `sheet_name`, `cell_range`, `source_anchor`, `section_path`, `parent_block_id`, and `chunk_index` from the public ingest path instead of reconstructing them from `location`.

- [ ] **Step 4: Add explicit OpenSearch mappings**

Map identifiers and versions as `keyword`, page/chunk indexes as numeric fields, section paths as keyword arrays, and locator JSON fields with explicit types. Keep the text field separately analyzed for BM25.

- [ ] **Step 5: Validate writer/read round-trip**

Index a fixture, read it back through `OpenSearchStore`, and assert that every locator field is identical to the source record.

- [ ] **Step 6: Commit**

```powershell
git add src/medical_rag/knowledge src/medical_rag/retrieval tests/contract/test_index_provenance_contract.py tests/contract/test_opensearch_provenance_mapping.py
git commit -m "feat: unify provenance across public and private indexes"
```

### Task 6: Make citations use structured provenance

**Files:**
- Modify: `src/medical_rag/chat/citations.py`
- Modify: `src/medical_rag/chat/orchestrator.py`
- Modify: `src/medical_rag/retrieval/contracts.py`
- Test: `tests/unit/test_citations.py`
- Test: `tests/integration/test_chat_provenance.py`

**Interfaces:**
- `Citation` gains `chunk_id`, `document_id`, `document_version_id`, and structured locator fields while preserving `location` for old clients.
- `CitationValidator.validate(...)` validates citation identity against the exact evidence set returned to the model.
- The API response includes `display_location` plus machine-readable locator fields.

- [ ] **Step 1: Write citation tests**

Cover valid page citations, valid Excel cell-range citations, missing chunk IDs, citations to an unreturned evidence record, and numeric claims whose cited evidence does not contain the same value/unit.

- [ ] **Step 2: Run citation tests and confirm failure**

```powershell
pytest tests/unit/test_citations.py tests/integration/test_chat_provenance.py -q
```

- [ ] **Step 3: Extend citation construction**

Construct citations directly from `SearchRecord`/`Evidence`, copying the structured locator. Generate `display_location` only at the response boundary.

- [ ] **Step 4: Reject unsupported evidence references**

If a model cites an ID that was not included in the evidence set, return the existing safe degraded/rejection result and record a non-PHI reason code.

- [ ] **Step 5: Run focused chat tests**

```powershell
pytest tests/unit/test_citations.py tests/integration/test_chat_provenance.py tests/integration/test_chat_orchestration.py -q
```

- [ ] **Step 6: Commit**

```powershell
git add src/medical_rag/chat src/medical_rag/retrieval/contracts.py tests/unit/test_citations.py tests/integration/test_chat_provenance.py
git commit -m "feat: validate citations against structured provenance"
```

### Task 7: Align deletion, audit, and reconciliation with provenance

**Files:**
- Modify: `src/medical_rag/ingest/delete_service.py`
- Modify: `src/medical_rag/indexing/reconciliation.py`
- Modify: `src/medical_rag/audit/service.py`
- Modify: `src/medical_rag/retrieval/indexing.py`
- Test: `tests/integration/test_delete_residuals.py`
- Test: `tests/integration/test_index_reconciliation.py`

**Interfaces:**
- Deletion receives `document_id`/`document_version_id` and removes all related evidence and index records by authoritative IDs.
- Reconciliation compares document version, evidence/chunk ID, content hash, locator, embedding model, and index version.
- Audit events record stable resource IDs and reason codes without recording PHI text.

- [ ] **Step 1: Add residual-cleanup tests**

Create a document with multiple chunks across pages and a table, delete it, and assert zero residuals in database, object storage, private index, and citation lookup.

- [ ] **Step 2: Run deletion tests and confirm failure**

```powershell
pytest tests/integration/test_delete_residuals.py -q
```

- [ ] **Step 3: Update deletion selectors**

Use `document_version_id` and `evidence_unit_id` as primary selectors. Do not delete by text, filename, or display location.

- [ ] **Step 4: Extend reconciliation comparison**

Report locator mismatch separately from content/version mismatch so operators can tell whether the problem is a moved source position or a stale content record.

- [ ] **Step 5: Verify privacy-safe audit output**

Assert that audit payloads contain IDs, counts, versions, and reason codes but not raw/clean text, patient names, or full model prompts.

- [ ] **Step 6: Commit**

```powershell
git add src/medical_rag/ingest src/medical_rag/indexing src/medical_rag/audit src/medical_rag/retrieval/indexing.py tests/integration/test_delete_residuals.py tests/integration/test_index_reconciliation.py
git commit -m "fix: make deletion and reconciliation provenance-aware"
```

### Task 8: Add source round-trip and release gates

**Files:**
- Create: `tests/eval/provenance_cases.jsonl`
- Create: `tests/eval/test_provenance_roundtrip.py`
- Modify: `tests/contract/test_release_checklist.py`
- Modify: `docs/e2e_test_plan_rag.md`
- Create: `docs/provenance-runbook.md`

**Interfaces:**
- `test_provenance_roundtrip.py` runs fixture cases through parse → clean → chunk → persist → index → retrieve and compares the returned locator with the expected source locator.
- Release gate fails if an indexed record has no usable locator, mismatched document version, missing content hash, or a citation that cannot resolve.

- [ ] **Step 1: Create four fixture families**

Add one PDF text fixture, one DOCX section fixture, one XLSX multi-level table fixture, and one image/OCR fixture. Store only fictional or already-approved de-identified content.

- [ ] **Step 2: Write failing round-trip tests**

For every fixture, assert document identity, version identity, page/section or sheet/range, source anchor, chunk index, and content hash.

- [ ] **Step 3: Run the evaluation tests and confirm failure**

```powershell
pytest tests/eval/test_provenance_roundtrip.py -q
```

- [ ] **Step 4: Add the release assertions**

Fail the release when any indexed record cannot resolve to a source locator or when any citation references an evidence ID outside the retrieved evidence set.

- [ ] **Step 5: Document operational recovery**

The runbook must show how to locate a chunk by ID, reconstruct its document/version path, inspect its source locator, reindex one version, and verify deletion residuals.

- [ ] **Step 6: Run the complete focused suite**

```powershell
pytest tests/contract tests/unit tests/integration/test_delete_residuals.py tests/integration/test_index_reconciliation.py tests/eval/test_provenance_roundtrip.py -q
```

- [ ] **Step 7: Commit**

```powershell
git add tests/eval tests/contract docs/e2e_test_plan_rag.md docs/provenance-runbook.md
git commit -m "test: add chunk provenance release gates"
```

### Task 9: Backfill and rollout safely

**Files:**
- Create: `scripts/backfill_chunk_provenance.py`
- Create: `scripts/provenance_audit.py`
- Modify: `docs/provenance-runbook.md`

**Interfaces:**
- `backfill_chunk_provenance.py --scope private|public|all --dry-run` reports reconstructable and unreconstructable records before writing.
- `provenance_audit.py --scope ...` emits counts by missing identity, missing locator, version mismatch, and content-hash mismatch without printing PHI.

- [ ] **Step 1: Implement dry-run inventory**

Read existing database/index records and output only counts, IDs hashed or truncated as configured, and reason codes.

- [ ] **Step 2: Run the dry run on a backup**

Verify that the script does not write to the database or OpenSearch and that unknown positions remain unknown rather than being fabricated.

- [ ] **Step 3: Implement idempotent backfill**

Write only missing structured fields when the source database contains authoritative values. Never overwrite a newer version or change content hashes.

- [ ] **Step 4: Reindex records with incomplete OpenSearch mappings**

Use the existing indexing worker path so embeddings and index versions remain consistent.

- [ ] **Step 5: Run the audit and release gate**

The rollout is accepted only when every newly indexed record has a resolvable identity and locator, and any legacy exceptions are explicitly listed for manual review.

- [ ] **Step 6: Commit**

```powershell
git add scripts/backfill_chunk_provenance.py scripts/provenance_audit.py docs/provenance-runbook.md
git commit -m "ops: add safe chunk provenance backfill"
```

## Final verification

Run the following from a clean checkout after restoring the complete test suite:

```powershell
pytest -q
ruff check src tests
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

Then run the live OpenSearch provenance round-trip with an explicit opt-in environment variable and verify:

- PDF/DOCX/XLSX/image chunks resolve to their original locator;
- citations resolve only to returned evidence IDs;
- public/private records preserve visibility and ACL invariants;
- deletion leaves zero database, object-store, and index residuals;
- reconciliation reports no unexplained locator or version mismatch;
- no test output or audit output contains PHI.
