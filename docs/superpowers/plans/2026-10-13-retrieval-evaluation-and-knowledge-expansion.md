# 检索评测与权威知识补全实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前真实 OpenSearch、SQLite 和冻结评测集上完成可复现的检索、拒答、引用和安全评测；只在确认知识缺口后补充可追溯的公开权威医学资料并复评。

**Architecture:** 保留现有 `retrieval_tuning_lab.py` 作为候选召回和门禁实验入口，新增独立的评测资产审计、引用陈述指标和统一结果汇总层。知识补全通过 `KnowledgeSourceRegistry`/`KnowledgeIngestService` 进入公共索引，所有来源和评测结果使用 manifest、SHA-256 和运行元数据绑定，冻结 test 集只做最终确认。

**Tech Stack:** Python 3.12、pytest、SQLAlchemy、OpenSearch 2.18、FastEmbed、本地 JSONL/JSON 评测工件、Markdown 来源清单。

**Spec:** `docs/superpowers/specs/2026-10-13-retrieval-evaluation-and-knowledge-expansion-design.md`

## Global Constraints

- 使用 `.venv/Scripts/python.exe` 运行 Python 命令；系统 Python 缺少 SQLAlchemy。
- 当前工作树包含大量既有未提交改动；只修改本计划列出的文件，不重置或覆盖其他改动。
- 冻结 `eval/rag_cases.test.v2.1.jsonl` 不得用于阈值调参。
- 最终指标必须来自真实 OpenSearch/SQLite 运行；单元测试夹具只能验证逻辑，不能冒充最终结果。
- 不把 PHI、患者正文或 embedding 向量写进诊断汇总；日志只保存 ID、哈希和元数据。
- 外部医学资料只接受官方机构或正式学会来源；每份资料保留 URL、版本/更新时间、抓取时间、审核人和内容哈希。

---

### Task 1: 评测资产审计与可复现 manifest

**Files:**
- Create: `scripts/audit_eval_assets.py`
- Create: `tests/eval/test_eval_asset_audit.py`
- Modify: `eval/rag_cases.test.v2.1.manifest.json` only when the computed hash or counts differ from the committed file
- Create: `eval_out/phase2_asset_audit.json` (generated artifact, not source code)

**Interfaces:**
- Produces `audit_eval_assets(cases_path: Path, index_metadata: Mapping[str, Mapping[str, object]]) -> dict[str, object]`.
- CLI accepts `--cases-file`, `--public-index`, `--private-index`, `--out` and emits a JSON report with file SHA-256, category counts, duplicate IDs, dev/test overlap, missing gold IDs, missing forbidden IDs, index counts and run timestamp.

- [ ] **Step 1: Write failing tests** for duplicate case IDs, dev/test question overlap, missing evidence IDs and a clean manifest.
- [ ] **Step 2: Run the focused tests** with `.venv/Scripts/python.exe -m pytest tests/eval/test_eval_asset_audit.py -q`; confirm the new audit function is missing or failing.
- [ ] **Step 3: Implement the audit function** using `json`, `hashlib`, `Path` and OpenSearch metadata-only queries; never request private `_source.text` fields.
- [ ] **Step 4: Add CLI output** that records the actual index aliases, physical index names, mapping dimension/space, embedding model and dataset hashes.
- [ ] **Step 5: Run the audit against the real local services:**

  ```powershell
  .venv/Scripts/python.exe scripts/audit_eval_assets.py `
    --cases-file eval/rag_cases.test.v2.1.jsonl `
    --public-index medical-rag-public-local `
    --private-index medical-rag-private-local `
    --out eval_out/phase2_asset_audit.json
  ```

- [ ] **Step 6: Verify** that the report identifies the current 120-case v2.1 split, current index counts, and any missing source/evidence binding before changing labels.
- [ ] **Step 7: Commit** only the audit script, its tests, and the generated audit artifact.

### Task 2: 引用陈述指标契约

**Files:**
- Create: `src/medical_rag/eval/citation_metrics.py`
- Modify: `src/medical_rag/eval/__init__.py`
- Create: `tests/eval/test_citation_metrics.py`

**Interfaces:**
- `CitationClaim(id: str, claim: str, evidence_ids: tuple[str, ...], expected_verdict: Literal["entailed", "contradicted", "insufficient"], claim_kind: str, evidence_sha256: Mapping[str, str])`.
- `CitationEvaluation(grounded_claims: int, valid_citations: int, supported_claims: int, contradicted_claims: int, insufficient_claims: int, citation_validity: float, citation_accuracy: float, citation_coverage: float)`.
- `evaluate_citation_claims(claims: Sequence[CitationClaim], evidence_lookup: Mapping[str, Mapping[str, object]]) -> CitationEvaluation`.

- [ ] **Step 1: Write failing tests** for entailed, contradicted, insufficient, missing evidence ID, hash mismatch, duplicate claim ID and empty claim input.
- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/eval/test_citation_metrics.py -q` and confirm failure.
- [ ] **Step 3: Implement** fail-closed validation: an evidence ID counts as valid only when it exists, is active, is retrieval-eligible, and its stored content hash matches the annotation hash.
- [ ] **Step 4: Implement aggregation** so `citation_accuracy` counts only claims whose expected verdict is `entailed`, while contradictions and insufficient support are exposed separately rather than silently counted as correct.
- [ ] **Step 5: Run the tests** and ensure all edge cases pass.
- [ ] **Step 6: Commit** the metric contract and focused tests.

### Task 3: 引用评测运行器与结果汇总

**Files:**
- Create: `scripts/evaluate_citation_claims.py`
- Create: `tests/eval/test_evaluate_citation_claims.py`
- Use: `eval/faithfulness_calibration.jsonl`
- Use: `eval/faithfulness_questions.jsonl`

**Interfaces:**
- CLI: `--claims-file`, `--public-index`, `--private-index`, `--out`, `--reviewer`.
- Output schema: `run_meta`, `dataset_sha256`, `index_snapshot`, `counts`, `metrics`, `invalid_annotations`, `contradictions`, `per_claim`.

- [ ] **Step 1: Write a failing CLI test** using a tiny deterministic metadata-only index stub; assert that missing IDs and hash mismatches appear in `invalid_annotations`.
- [ ] **Step 2: Implement JSONL loading** for the existing `faithfulness_calibration.jsonl` schema and preserve its `expected_verdict`, `claim_kind`, reviewer and evidence hash fields.
- [ ] **Step 3: Implement public/private metadata lookup** through OpenSearch `_source` fields required for citation validation; do not retrieve private text into the aggregate report.
- [ ] **Step 4: Write the real calibration result** to a new output directory, including exact index mapping identity and dataset hash.
- [ ] **Step 5: Run**:

  ```powershell
  .venv/Scripts/python.exe scripts/evaluate_citation_claims.py `
    --claims-file eval/faithfulness_calibration.jsonl `
    --public-index medical-rag-public-local `
    --private-index medical-rag-private-local `
    --out eval_out/citation_phase2.json `
    --reviewer "俞家俊"
  ```

- [ ] **Step 6: Compare the output to the committed calibration expectations**; any mismatch becomes a review item, not an automatic pass/fail rewrite.
- [ ] **Step 7: Commit** the runner, tests and result schema documentation.

### Task 4: Dev 门禁阈值扫描

**Files:**
- Create: `scripts/retrieval_threshold_sweep.py`
- Create: `tests/eval/test_retrieval_threshold_sweep.py`
- Use: `scripts/retrieval_tuning_lab.py`
- Use: `eval/rag_cases.dev.jsonl`

**Interfaces:**
- `run_threshold_sweep(thresholds: Sequence[float], cases_file: Path, public_index: str, private_index: str, rounds: int) -> dict[str, object]`.
- Each row contains `threshold`, `score_space`, `quality.recall_at_k`, `quality.precision_at_k`, `quality.mrr`, `rejection.accuracy`, `rejection.false_reject_rate`, `safety`, `latency` and per-case IDs.

- [ ] **Step 1: Write failing tests** for threshold ordering, empty-candidate rejection, answerable false rejection and preservation of safety counts.
- [ ] **Step 2: Implement the sweep** by constructing the existing `RetrievalService` with each threshold and the current calibration identity; use the actual index seams, not the selftest fixture.
- [ ] **Step 3: Keep the gate policy explicit** as `COSINE_ABSOLUTE`, record `score_space=cosinesimil`, and reject a run when the threshold artifact identity does not match the current embedding model/index version.
- [ ] **Step 4: Run the dev sweep** with a bounded set such as `0.70,0.72,0.74,0.76,0.78,0.80,0.82` and one warm-up plus 5 measured rounds.
- [ ] **Step 5: Generate** a JSON result and a CSV/Markdown curve table; do not choose the final threshold yet.
- [ ] **Step 6: Review** the point where safety remains all-zero and the false-reject/recall trade-off is acceptable; record the selected point and rationale in a new calibration artifact.
- [ ] **Step 7: Commit** the sweep script, tests and dev result artifact.

### Task 5: 权威来源缺口审计与条件式补库

**Files:**
- Create: `scripts/source_gap_audit.py`
- Create: `tests/eval/test_source_gap_audit.py`
- Create conditionally: `knowledge/sources/<source-slug>/manifest.json`
- Create conditionally: `knowledge/sources/<source-slug>/*.md`
- Use: `src/medical_rag/knowledge/ingest_service.py`

**Interfaces:**
- `find_source_gaps(cases_file: Path, retrieval_results: Path, index_metadata: Mapping[str, Mapping[str, object]]) -> dict[str, object]`.
- `SourceManifest` fields: `institution`, `title`, `source_version`, `publication_date`, `scope`, `language`, `url`, `retrieved_at`, `reviewer`, `content_sha256`, `source_type`, `summary_boundary`.

- [ ] **Step 1: Write failing tests** for a covered question, a missing direct-support question and a source whose index record exists but has a stale hash.
- [ ] **Step 2: Implement the gap audit** using existing gold IDs, grouped alternatives and per-case retrieval results; only classify a gap when the question is in scope and no active eligible direct-support evidence exists.
- [ ] **Step 3: Review the gap report** before any external fetch. If no gap is confirmed, skip this task and record that no new source was needed.
- [ ] **Step 4: For each confirmed gap, fetch only an official source** from WHO, a national health authority, a formal medical society, NIH/NHLBI, CDC, MedlinePlus, NICE or FDA; save the URL and retrieval timestamp in the manifest.
- [ ] **Step 5: Save the source as a clearly bounded Markdown summary** when the official page is the available source; do not represent a summary as a full guideline.
- [ ] **Step 6: Register and ingest** through `KnowledgeSourceRegistry.register()` and `KnowledgeIngestService.ingest()` with a medical reviewer, then capture the `IngestReceipt`, source ID, chunk IDs and content hashes.
- [ ] **Step 7: Verify** that new public chunks are active, eligible, cited with source/version/location metadata and indexed with the current 512-dimensional embedding model.
- [ ] **Step 8: Commit** only the manifest, bounded source text, ingest receipt and audit report; never commit credentials or PHI.

### Task 6: 冻结 test 复评与统一报告

**Files:**
- Modify: `scripts/retrieval_tuning_lab.py` only if grouped evidence or metric fields are missing from the final report
- Create: `scripts/build_evaluation_report.py`
- Create: `tests/eval/test_build_evaluation_report.py`
- Create: `docs/evaluation/retrieval-evaluation-2026-10-13.md`
- Create: `eval_out/final_evaluation_manifest.json`

**Interfaces:**
- `build_report(retrieval_result: Path, citation_result: Path, asset_audit: Path, threshold_result: Path | None) -> dict[str, object]`.
- The report must preserve per-case IDs, returned evidence IDs, reasons, gate metadata, source/index versions and metric denominators.

- [ ] **Step 1: Write a failing report test** for missing artifact, mismatched dataset hash and a valid combined run.
- [ ] **Step 2: Implement hash and identity checks** so retrieval, citation and threshold artifacts cannot be combined when their dataset, index version, embedding model or score space differs.
- [ ] **Step 3: Run the full v2.1 test evaluation** with the selected dev threshold and the actual local indexes; use multiple measured rounds and save the raw result.
- [ ] **Step 4: Run the citation evaluator** against the same source/index snapshot and save the raw citation result.
- [ ] **Step 5: Generate the final Markdown report** with Recall@3, Precision@3, MRR, nDCG@3, correct rejection rate, false rejection rate, citation validity/accuracy/coverage, safety zeros and p50/p95 latency.
- [ ] **Step 6: Inspect all failed cases manually** and classify each as retrieval miss, insufficient corpus, annotation issue, gate false reject, citation mismatch, permission issue or infrastructure failure.
- [ ] **Step 7: Run the focused test suites and then the full suite:**

  ```powershell
  .venv/Scripts/python.exe -m pytest tests/eval tests/test_rejection_gate.py -q
  .venv/Scripts/python.exe -m pytest -q
  ```

- [ ] **Step 8: Commit** the evaluator, report builder, final manifest and report only after verification output is captured.

## Self-Review Checklist

- The frozen test set is never passed to threshold-selection code.
- Citation claims with missing or stale evidence are reported as invalid, never silently dropped.
- Public and private evidence are validated through their respective ACL boundaries.
- External sources are added only after the gap audit produces a concrete missing-support case.
- The final report can be regenerated from the saved raw artifacts without network access.
- The report distinguishes real runs, historical artifacts, unit-test fixtures and unresolved clinical review items.
