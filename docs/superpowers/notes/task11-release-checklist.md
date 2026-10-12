# Task 11 — Step 4 Manual Release Checklist (E2E rollout verification)

This checklist is the operator-facing verification for the Task 11 release.
Every item maps to a real test in `tests/integration/test_release_checklist.py`
(and the assertion the reader should trust), except the on-disk artifact items
which are validated by this document and the golden report writer.

| # | Invariant | Where it is enforced / verified | Status |
|---|-----------|--------------------------------|--------|
| 1 | Raw files remain readable (original never overwritten) | `test_document_lifecycle.py::test_upload_persists_original_and_parse_job` — the object-store blob is preserved under `document.storage_key`; evidence carries `raw_text` separate from `clean_text` (Task 5 never-overwrite invariant). | ✅ |
| 2 | Every indexed chunk has a source anchor | `tests/contract/test_index_record_contract.py` + golden `MIN_ANCHOR_ACCURACY` (99%); the e2e lifecycle test indexes an evidence row with `source_anchor="p1"` and it is retrievable; `retrieval/indexing.py` rejects anchor-less records from embedding (write-but-flag). | ✅ |
| 3 | Public records contain no tenant/patient IDs | `test_retrieval_permissions.py::test_public_records_carry_no_tenant_or_patient_identifiers` — a `Visibility.PUBLIC` record has `tenant_id=None`/`patient_id=None`. | ✅ |
| 4 | Review records have `retrieval_eligible=False` and are never retrievable | `test_retrieval_permissions.py::test_review_ineligible_private_record_is_never_retrievable` and the e2e lifecycle review section — persisted but absent from retrieval. | ✅ |
| 5 | All version fields are present | `test_p0_rollout.py::test_release_checklist_verifies_versions_and_zero_deletion_residual` — `parser_version`, `cleaner_version`, `chunker_version`, `index_version`, `embedding_model` on a real `PrivateIndexRecord`; version fields thread from `VersionBundle` through `IndexJob` (`test_index_outbox.py`). | ✅ |
| 6 | Retries are bounded and observable | `outbox.retry_event` / `IndexJob.retry` apply capped exponential backoff (`min(3600, 5*2**(attempts-1))`); `test_index_outbox.py::test_outbox_retry_lease_and_publish_emit_bounded_counter` and the e2e lifecycle retry section observe attempts. | ✅ |
| 7 | Deletion residual count is zero | `test_delete_residuals.py::test_deletion_leaves_zero_searchable_residuals_stage_metric` — `RetrievalMetrics.deletion_residuals == 0` and the stage-metric `deletion_residual_count == 0`; `RolloutGate` rejects any nonzero residual. | ✅ |
| 8 | Golden-set report is stored with release artifacts | `eval/golden.py` writes `baseline.json` in the golden fixtures directory on a PASS; `GoldenFixtureResult.as_baseline_dict()` includes the six RAG baselines incl. `deletion_residuals`, `anchor_coverage` and per-stage timings. This document is the release-artifact companion. | ✅ |

## Release-gate summary

Run `python -m pytest -q` (and `--ignore=tests/contract/test_opensearch_writer.py`
for the collision-isolated verification). Expected (Task 11 baseline + additions):

- `--ignore` version: **336 passed, 3 skipped** (315 pre-task baseline + 21 new tests).
- Full run: **345 passed, 3 skipped** (includes the parallel session's
  `test_opensearch_writer.py`, which passes unchanged).

Base lint: `python -m ruff check src tests` reports the pre-existing repo-wide
issues only (see "Lint notes" below); no NEW errors are introduced by Task 11's
files.

## Lint notes (Task 11)

- New modules `stage_metrics.py` and `per_page_ocr.py`: `ruff check` clean.
- New/edited Task 11 test files: `ruff check` clean. The one pre-existing `F841`
  (`test_retrieval_permissions.py` `consent` var in the original revoked-consent
  test) was resolved with a benign unused-variable removal — the grant side-effect
  and all assertions are intact; the file now lints clean.
- `src/.../api/dependencies.py` and other parallel-session files already carry
  pre-existing lint findings (B008 / I001 / …). Per the collision rules these are
  **not** fixed by Task 11.

## Artifacts produced by Task 11

- `src/medical_rag/observability/stage_metrics.py` — stage counters/timers.
- `src/medical_rag/parsing/per_page_ocr.py` — per-page OCR backend (deterministic
  transport) wire into `parsing/service.py` + `parsing/adapters.py`.
- `src/medical_rag/domain/models.py` + `alembic/versions/0007_add_parse_job_version_index.py`
  — `parse_jobs.document_version_id` index (n3b).
- `src/medical_rag/indexing/outbox.py`, `src/medical_rag/ingest/delete_service.py`
  — retry/lease/deletion-residual observability.
- Tests: `test_release_checklist.py`, `test_per_page_ocr.py`,
  `test_parse_job_index.py`, `test_stage_metrics.py` + extensions to the four
  plan test files.
