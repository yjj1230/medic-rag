# Worker Entry and PostgreSQL Re-ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a reproducible worker/index initialization entry point and make the API/search layer target explicitly named PostgreSQL-era OpenSearch indices without touching legacy indices.

**Architecture:** Add explicit public/private index-name settings accepted from `RAG_PUBLIC_INDEX_NAME` and `RAG_PRIVATE_INDEX_NAME`. The restored `scripts/run_worker.py` will provide a narrow `init-indexes` command that constructs two visibility-scoped `OpenSearchIndexWriter` instances and calls `ensure_mapping`; the script will fail clearly for unsupported worker commands until their complete production composition is restored rather than silently starting a partial worker.

**Tech Stack:** Python 3.12, Pydantic Settings, SQLAlchemy, OpenSearch Python client, pytest, PowerShell.

**Spec:** `docs/superpowers/specs/2026-10-12-chunk-provenance-design.md`

## Global Constraints

- Preserve existing public/private index separation and fail-closed authorization behavior.
- Never delete or mutate the legacy `medical-rag-public-local` and `medical-rag-private-local` indices.
- Do not copy SQLite rows into PostgreSQL as part of this change.
- Index dimensions and embedding identity must come from `resolve_embedding(settings)`.
- Keep all new operational output free of document text or PHI.

---

### Task 1: Add explicit index-name settings

**Files:**
- Modify: `src/medical_rag/core/config.py`
- Modify: `src/medical_rag/api/composition.py`
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Test: `tests/unit/test_index_settings.py`

**Interfaces:**
- `Settings.public_index_name: str` reads `RAG_PUBLIC_INDEX_NAME` and defaults to `medical-rag-public`.
- `Settings.private_index_name: str` reads `RAG_PRIVATE_INDEX_NAME` and defaults to `medical-rag-private`.
- `build_search_index(settings)` passes both names to `OpenSearchStore`.

- [x] **Step 1: Write a failing settings test** asserting RAG-prefixed environment variables override defaults and are passed to `OpenSearchStore`.
- [x] **Step 2: Run the focused test and confirm it fails** because the settings fields and constructor wiring do not exist.
- [x] **Step 3: Implement the two aliased settings and composition wiring.**
- [x] **Step 4: Run the focused test and the existing OpenSearch contract tests.**

### Task 2: Restore the safe `init-indexes` entry point

**Files:**
- Create: `scripts/run_worker.py`
- Test: `tests/contract/test_run_worker_entrypoint.py`

**Interfaces:**
- `build_opensearch_client(settings) -> OpenSearch` creates a client from `settings.opensearch_url`.
- `build_index_writers(settings, client) -> tuple[OpenSearchIndexWriter, OpenSearchIndexWriter]` returns public/private scoped writers using resolved embedding dimensions and configured analyzer names.
- `init_indexes(settings) -> dict[str, str]` calls `ensure_mapping` for both writers and returns the configured physical names.
- CLI command: `python scripts/run_worker.py init-indexes`.
- Unsupported commands fail with a non-zero exit and an explicit message; they must not start a partial Celery worker.

- [x] **Step 1: Write failing tests** using a fake OpenSearch client to assert both writers receive the correct scope, index name, alias, dimension, and analyzer, and that `init_indexes` calls both mappings.
- [x] **Step 2: Run the focused tests and confirm failure** because `scripts/run_worker.py` does not exist.
- [x] **Step 3: Implement the minimal script and CLI parser.**
- [x] **Step 4: Run the focused tests and `python scripts/run_worker.py --help`.**

### Task 3: Verify against the live OpenSearch instance

**Files:**
- Modify: `docs/provenance-runbook.md`

- [x] **Step 1: Set a new index namespace** such as `medical-rag-public-pg-local` and `medical-rag-private-pg-local`.
- [x] **Step 2: Run `init-indexes` with `EMBEDDING_BACKEND=none` or the configured local embedding backend.**
- [x] **Step 3: Query `_cat/indices` and verify only the new physical indices were created.**
- [x] **Step 4: Run PostgreSQL provenance audit and record the expected empty-database result before the first ingest.**

### Task 4: Regression verification

- [x] **Step 1: Run `pytest -q`.**
- [x] **Step 2: Run `ruff check src scripts tests`.**
- [x] **Step 3: Run `git diff --check`.**
- [x] **Step 4: Report that legacy indices were preserved and no SQLite data was copied.**
