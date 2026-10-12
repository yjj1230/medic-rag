# Medical RAG Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the current end-to-end RAG gaps and evolve the medical patient assistant from a passing MVP skeleton into a measurable, permission-safe, citation-grounded 2-step RAG, with controlled Agentic RAG deferred until the baseline is reliable.

**Architecture:** Keep a modular monolith with explicit seams: an indexing pipeline owns parsing-to-index consistency, a query pipeline owns rewrite/routing, a deep hybrid retriever owns ACL filtering/fusion/reranking, and the chat orchestrator owns conversation context, model calls, citation validation, and safety. Start with deterministic 2-step RAG for predictable latency; add a bounded tool loop only after evaluation gates pass.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL, OpenSearch, Redis/Celery, httpx, pytest, OpenTelemetry.

**Spec:** `docs/superpowers/specs/2026-09-09-medical-patient-rag-design.md`

## Global Constraints

- Patient and public knowledge remain in separate indexes and permission domains.
- ACL/tenant/patient filters must be applied before reranking and prompt assembly.
- External model calls remain behind `ModelGateway` and require consent, provider allowlisting, zero retention, and audit.
- The system must not diagnose, prescribe, or recommend medication changes.
- Deleted or revoked data must not be retrievable from any derived index or cache.
- Every answerable medical claim must have a locatable citation or be withheld.

---

## Current Findings

- `ChatOrchestrator.answer()` performs one raw-query retrieval with fixed `top_k=8`; it does not use session history, query rewrite, routing, or multi-step tools.
- `SearchQuery.embedding` exists but is never populated; `KnowledgeIngestService` does not call an embedding model, so OpenSearch normally executes only lexical matching.
- Parsed patient `EvidenceUnit` rows are written to PostgreSQL, but no private-index writer is connected from `ParsingService`; uploaded reports therefore cannot complete the private RAG path.
- `OpenSearchStore` mixes `match` and `knn` in one boolean query and reads `_score` as BM25 while leaving vector score at zero; there is no score normalization or RRF-style fusion.
- `KnowledgeChunker` is character-based, has no overlap or parent context, and preserves an arbitrarily large table as one chunk.
- `ParsingService` marks the document/version `READY` even when the parse job is `NEEDS_REVIEW`, allowing review-required evidence to look production-ready.
- Citation validation checks that at least one cited ID exists, but does not enforce claim-level coverage or output-side medication/urgent-risk checks.
- `ChatSession` history is persisted but never loaded into the next prompt; supplied `session_id` is not ownership-validated before lookup.
- There is no retrieval audit/trace/quality evaluation path despite `AuditAction.RETRIEVAL` and the planned observability/eval modules.

## File Map

- Modify `src/medical_rag/knowledge/contracts.py`: define index records with embedding/model/version and private/public metadata.
- Modify `src/medical_rag/knowledge/chunker.py`: add token-aware/structure-aware chunking, overlap, parent heading context, and bounded table chunks.
- Modify `src/medical_rag/knowledge/ingest_service.py`: batch embedding, idempotent upsert, index version, and outbox/retry semantics.
- Modify `src/medical_rag/parsing/service.py` and `src/medical_rag/parsing/worker.py`: index private evidence after successful parsing and gate `NEEDS_REVIEW` records.
- Modify `src/medical_rag/retrieval/contracts.py`, `retrieval/opensearch_store.py`, and `retrieval/service.py`: separate lexical/vector candidate retrieval, ACL filters, score fusion, reranking, and diagnostics.
- Create `src/medical_rag/chat/query_pipeline.py`: query normalization, standalone rewrite, intent routing, and optional sub-question decomposition.
- Modify `src/medical_rag/chat/prompts.py`, `chat/contracts.py`, and `chat/orchestrator.py`: conversation context, structured claims, bounded context budget, and explicit retrieval trace.
- Modify `src/medical_rag/chat/citations.py` and `src/medical_rag/safety/service.py`: claim-level citation coverage and output-side safety checks.
- Create `src/medical_rag/observability/tracing.py`, `observability/metrics.py`, and `tests/eval/`: spans, latency/cost counters, retrieval/answer evaluation, and regression cases.

## Task 1: Establish a quality baseline before changing ranking

**Files:**
- Create: `tests/eval/cases.jsonl`
- Create: `tests/eval/test_rag_quality.py`
- Create: `src/medical_rag/observability/tracing.py`
- Create: `src/medical_rag/observability/metrics.py`

**Interfaces:**
- `Evaluator.run_case(case: EvalCase) -> EvalResult`
- `Metrics.record_retrieval(...) -> None`
- `Metrics.record_model_call(...) -> None`

- [ ] Add 30–50 deterministic cases covering public knowledge, private report facts, tables, bilingual queries, no-evidence, stale/deleted documents, cross-patient access, diagnosis requests, medication changes, and urgent symptoms.
- [ ] Record retrieved IDs, scores, source visibility, model/provider, latency, token usage, and final status without logging raw patient text.
- [ ] Run the baseline and save Recall@k, MRR/nDCG, citation coverage, faithfulness proxy, P95 latency, and degraded/error rates.
- [ ] Do not tune chunk size or thresholds until this baseline exists.

## Task 2: Complete private report indexing and review gating

**Files:**
- Modify: `src/medical_rag/retrieval/contracts.py`
- Modify: `src/medical_rag/parsing/service.py`
- Modify: `src/medical_rag/parsing/worker.py`
- Modify: `src/medical_rag/domain/models.py` and migration files if index metadata is persisted
- Test: `tests/integration/test_knowledge_ingest.py`
- Test: `tests/integration/test_retrieval_permissions.py`

**Interfaces:**
- `PrivateIndexWriter.index_private_evidence(record: PrivateIndexRecord) -> None`
- `PrivateIndexWriter.delete_document(document_id: UUID) -> None`
- `PrivateIndexRecord(id, text, tenant_id, patient_id, document_id, version_id, source_anchor, confidence, active, deleted, embedding, embedding_model) -> ...`

- [ ] Inject a private index writer into `ParsingService` rather than making the parser know OpenSearch details.
- [ ] After evidence rows are flushed, create private records containing tenant/patient/document/version IDs, page/sheet/cell/anchor, confidence, and deletion flags.
- [ ] Index only evidence that is eligible for retrieval; keep low-confidence or conflicting evidence in PostgreSQL with `needs_review` status and an explicit `retrieval_eligible=False` marker.
- [ ] Change document/version status semantics so `NEEDS_REVIEW` is not presented as `READY` to retrieval; add tests proving review-required evidence is excluded.
- [ ] Make private-index writes idempotent by `(document_version_id, evidence_unit_id, parser_version)` and retryable through the existing job record.

## Task 3: Add real embeddings and versioned index writes

**Files:**
- Modify: `src/medical_rag/knowledge/contracts.py`
- Modify: `src/medical_rag/knowledge/ingest_service.py`
- Modify: `src/medical_rag/models/contracts.py`
- Modify: `src/medical_rag/models/http_adapters.py`
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Test: `tests/contract/test_model_provider_contract.py`
- Test: `tests/unit/test_knowledge_ingest.py`

**Interfaces:**
- `EmbeddingModel.embed(texts: list[str]) -> list[list[float]]`
- `IndexRecord.embedding: tuple[float, ...] | None`
- `IndexWriter.upsert(records: list[IndexRecord], index_version: str) -> None`

- [ ] Batch chunks for embedding with bounded batch size and a configured embedding model/version.
- [ ] Persist embedding model, dimensions, content hash, and index version so re-indexing is reproducible.
- [ ] Replace ad-hoc `append`/`index_public_chunk` branching with one explicit writer interface and an in-memory adapter for tests.
- [ ] Use bulk upsert and an alias/version switch; never expose partially rebuilt indexes.
- [ ] Ensure embedding retries are bounded and do not block the request thread; preserve failed records for retry.

## Task 4: Replace the current pseudo-hybrid search with measurable fusion

**Files:**
- Modify: `src/medical_rag/retrieval/contracts.py`
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Modify: `src/medical_rag/retrieval/service.py`
- Test: `tests/unit/test_retrieval_service.py`
- Test: `tests/contract/test_retrieval_store.py`

**Interfaces:**
- `CandidateRetriever.search_lexical(query, filters, limit) -> list[SearchRecord]`
- `CandidateRetriever.search_vector(embedding, filters, limit) -> list[SearchRecord]`
- `HybridRanker.fuse(lexical, vector, limit) -> list[SearchRecord]`
- `RetrievalResult(items, reason, diagnostics)`

- [ ] Execute lexical and KNN retrieval as separate candidate searches with identical ACL/metadata filters.
- [ ] Fuse by Reciprocal Rank Fusion first; only introduce learned weighting after evaluation demonstrates a benefit.
- [ ] Normalize or avoid comparing raw BM25 and vector scores; make `min_score` apply to a calibrated fused score or remove it in favor of a threshold learned from eval data.
- [ ] Preserve per-source scores, rank, index version, and filter decisions in diagnostics.
- [ ] Apply document freshness, source activity, language, and requested `document_ids` filters before reranking.
- [ ] Run reranking only on authorized candidates and cap candidate count for latency.

## Task 5: Improve chunk quality without breaking citations

**Files:**
- Modify: `src/medical_rag/knowledge/chunker.py`
- Modify: `src/medical_rag/knowledge/contracts.py`
- Test: `tests/unit/test_knowledge_chunker.py`

- [ ] Keep headings and table headers in each child chunk as contextual metadata or a prefixed parent path.
- [ ] Use token-aware limits with configurable overlap; do not split in the middle of a sentence where a sentence boundary is available.
- [ ] Split large tables by row groups while repeating headers and preserving sheet/page/cell ranges.
- [ ] Preserve exact source anchors; do not replace a precise section/page location with one document-level `source.location` for every chunk.
- [ ] Add tests for Chinese text, English text, mixed-language text, long paragraphs, long tables, and stable re-ingestion hashes.

## Task 6: Add query understanding and conversation context

**Files:**
- Create: `src/medical_rag/chat/query_pipeline.py`
- Modify: `src/medical_rag/chat/contracts.py`
- Modify: `src/medical_rag/chat/prompts.py`
- Modify: `src/medical_rag/chat/orchestrator.py`
- Test: `tests/unit/test_query_pipeline.py`
- Test: `tests/integration/test_chat_orchestration.py`

**Interfaces:**
- `QueryPipeline.prepare(question, history, context) -> PreparedQuery`
- `PreparedQuery(standalone_text, intent, filters, subqueries, retrieval_mode)`
- `ConversationStore.load_owned(session_id, context, limit) -> list[ChatMessage]`

- [ ] Validate `session_id` ownership against tenant and patient before loading messages.
- [ ] Load a bounded recent history or summary; never send the entire session blindly.
- [ ] Add deterministic normalization first, then an optional governed LLM rewrite for follow-up questions.
- [ ] Route ordinary education, report explanation, structured fact lookup, and unsupported/high-risk intents explicitly.
- [ ] Use multi-query decomposition only for questions that need it; run subqueries in parallel and deduplicate evidence.
- [ ] Keep the first production version 2-step and measurable; do not add an unconstrained autonomous Agent loop here.

## Task 7: Strengthen answer contracts, citation coverage, and output safety

**Files:**
- Modify: `src/medical_rag/chat/contracts.py`
- Modify: `src/medical_rag/chat/prompts.py`
- Modify: `src/medical_rag/chat/citations.py`
- Modify: `src/medical_rag/safety/service.py`
- Test: `tests/unit/test_citations.py`
- Test: `tests/unit/test_safety_service.py`

- [ ] Prefer structured model output: `claims: [{text, evidence_ids, claim_type}]` plus `answer_sections`, instead of extracting arbitrary bracket tokens from free text.
- [ ] Require every factual/medical claim to cite one or more retrieved evidence IDs; reject unsupported claims, not merely answers with zero citations.
- [ ] Add output-side checks for diagnosis, medication change instructions, emergency under-triage, and prompt-injection attempts in retrieved text.
- [ ] Clearly separate patient-reported facts, general medical interpretation, uncertainty, and next steps in the response contract.
- [ ] Preserve source title/version/location in the returned citation and make duplicate citations deterministic.

## Task 8: Add reliability, audit, and rollout gates

**Files:**
- Modify: `src/medical_rag/chat/orchestrator.py`
- Modify: `src/medical_rag/models/http_adapters.py`
- Modify: `src/medical_rag/audit/service.py`
- Create: `tests/integration/test_external_failure.py`
- Create: `tests/integration/test_delete_residuals.py`

- [ ] Add retrieval audit events with query hash, evidence IDs, visibility, index version, and reason; never store unnecessary raw patient text.
- [ ] Use per-stage time budgets and a single overall deadline; retry only idempotent embedding/search operations.
- [ ] Add circuit-breaker/degraded behavior for model, embedding, reranker, and OpenSearch failures.
- [ ] Verify deletion and consent revocation remove data from DB, object store, private/public indexes, caches, and summaries.
- [ ] Canary new index/model versions and compare eval and production metrics before switching the alias.

## Task 9: Introduce controlled Agentic RAG only after the 2-step gate

**Files:**
- Create: `src/medical_rag/agent/contracts.py`
- Create: `src/medical_rag/agent/runner.py`
- Create: `tests/unit/test_agent_runner.py`

- [ ] Allow only an explicit tool registry: retriever, structured-fact lookup, approved calculator, and approved knowledge/API tools.
- [ ] Enforce max steps, max tool latency, max token/cost budget, and cancellation.
- [ ] Re-apply tenant/patient/consent checks inside every tool adapter; the Agent must never receive direct DB or object-store access.
- [ ] Require an evidence sufficiency decision before final synthesis and run the same citation/output-safety validator as 2-step RAG.
- [ ] Keep an auditable tool trace and fall back to a safe insufficient-evidence response when the loop cannot converge.

## Recommended Execution Order and Exit Criteria

```text
1 baseline/eval
  -> 2 private indexing + review gate
  -> 3 embeddings/index versioning
  -> 4 hybrid fusion/rerank
  -> 5 chunk quality
  -> 6 query/history
  -> 7 answer/citation/safety
  -> 8 reliability/delete/rollout
  -> 9 bounded Agentic RAG
```

Do not proceed to the next gate unless:

- Private cross-patient retrieval remains zero in adversarial tests.
- Deleted and revoked evidence has zero residual retrieval.
- Retrieval metrics improve against the baseline without increasing unauthorized hits.
- Citation coverage and citation-source consistency meet the project target (95%+ for key medical claims).
- High-risk output tests remain zero-tolerance failures.
- P95 latency and model cost remain within the pilot budget.

