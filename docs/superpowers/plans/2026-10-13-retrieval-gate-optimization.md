# 检索门禁优化与失败归因实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正真实 OpenSearch 分数空间身份，增加向量独有候选的两级门禁，生成完整失败归因，并用 dev 选择新的门禁工件后复评冻结 test。

**Architecture:** 保留 `RetrievalService` 的 ACL、生命周期、引用和 RRF 主链路；在 `OpenSearchStore` 增加映射分数空间身份，在 `CalibrationArtifact` 增加向量独有阈值，在门禁阶段分离词法+向量交集和向量独有候选。评测层新增失败归因和二维阈值扫描，所有选择只来自 dev 集。

**Tech Stack:** Python 3.12、pytest、ruff、OpenSearch 2.18、FastEmbed、JSON/JSONL 评测工件。

**Spec:** `docs/superpowers/specs/2026-10-13-retrieval-gate-optimization-design.md`

## Global Constraints

- 使用 `.venv/Scripts/python.exe` 运行 Python 命令。
- 当前工作树有其他未提交改动；只修改本计划列出的文件，不 reset、stash 或覆盖它们。
- 冻结 `eval/rag_cases.test.v2.1.jsonl` 不得用于阈值选择。
- 不把患者正文、PHI 或 embedding 向量写入诊断汇总；只保存 ID、哈希、分数和元数据。
- mapping、配置和记录分数空间不一致时严格门禁 fail-closed。
- 旧校准工件缺少新字段时保持兼容，但必须记录回退诊断。

---

### Task 1: 分数空间身份契约

**Files:**
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Modify: `src/medical_rag/retrieval/service.py`
- Modify: `tests/test_rejection_gate.py`
- Create: `tests/eval/test_retrieval_score_space.py`

**Interfaces:**
- `OpenSearchStore(..., vector_score_space: str | None = None)` 保存显式或映射发现的分数空间。
- `OpenSearchStore.resolve_vector_score_space() -> str` 读取 `embedding.method.space_type`，缺失时返回 `mapping_unavailable` 错误信息而不是猜测。
- `RetrievalService` 诊断增加 `mapping_score_space` 和 `score_space_match`。

- [ ] **Step 1: Write failing tests** for a `cosinesimil` mapping, a missing mapping method, and a service response when record space differs from gate space.
- [ ] **Step 2: Run focused tests** with `.venv/Scripts/python.exe -m pytest tests/eval/test_retrieval_score_space.py tests/test_rejection_gate.py -q`; confirm the new contract is absent or failing.
- [ ] **Step 3: Implement mapping extraction** without fetching document text; permit only `l2_inv` and `cosinesimil`.
- [ ] **Step 4: Set `OpenSearchStore` record space from the resolved mapping** and propagate it into `SearchRecord.vector_score_space`.
- [ ] **Step 5: Add fail-closed service validation** for gate/mapping/record space mismatches and preserve `vector_path_unavailable` behavior when the vector backend itself is unavailable.
- [ ] **Step 6: Run focused tests and ruff** for the modified files.
- [ ] **Step 7: Commit** the score-space contract and tests.

### Task 2: 两级门禁与校准工件

**Files:**
- Modify: `src/medical_rag/observability/metrics.py`
- Modify: `src/medical_rag/retrieval/service.py`
- Modify: `config/calibration/cosine-gate-2026-10-13.json`
- Modify: `tests/test_rejection_gate.py`
- Create: `tests/eval/test_two_level_gate.py`

**Interfaces:**
- `CalibrationArtifact.vector_only_cosine_threshold: float | None`，缺省时回退到 `cosine_threshold`。
- `RetrievalService.vector_only_cosine_threshold: float | None`。
- Diagnostics include `vector_only_candidates`, `vector_only_gated_out`, `lexical_vector_intersection`, `vector_only_gate_threshold`, and `vector_only_threshold_fallback`.

- [ ] **Step 1: Write failing tests** for a lexical+vector candidate passing the base threshold, a vector-only candidate requiring the stricter threshold, a lexical-only candidate being removed, and legacy artifact fallback.
- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/eval/test_two_level_gate.py -q`; confirm failure.
- [ ] **Step 3: Extend `CalibrationArtifact` validation** so the optional threshold is numeric, bounded, and only valid with a cosine gate.
- [ ] **Step 4: Implement gate partitioning** after ACL/lifecycle/citation filtering: base threshold for all vector candidates, stricter threshold for vector-only candidates, and preserve RRF ordering for survivors.
- [ ] **Step 5: Add diagnostics** without including text or vectors.
- [ ] **Step 6: Set the new calibration artifact field** only after dev scanning identifies its value; keep the checked-in artifact structurally valid during intermediate tests.
- [ ] **Step 7: Run focused gate tests, existing rejection tests, and ruff.**
- [ ] **Step 8: Commit** the two-level gate implementation and tests.

### Task 3: 失败用例归因器

**Files:**
- Create: `scripts/retrieval_failure_attribution.py`
- Create: `tests/eval/test_retrieval_failure_attribution.py`

**Interfaces:**
- `attribute_failures(cases_file: Path, retrieval_result: Path, *, asset_audit: Path | None = None) -> dict[str, object]`.
- Output fields: `run_meta`, `dataset_sha256`, `counts`, `per_case`, `unresolved_manual_review`.
- Each case stores only `case_id`, `category`, `gold_evidence_sha256`, `returned_ids`, `reason`, `diagnostics_summary`, and `labels`.

- [ ] **Step 1: Write failing tests** for retrieval miss, gate false reject, gate false accept, permission/lifecycle failure, infrastructure failure, and unresolved manual review.
- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/eval/test_retrieval_failure_attribution.py -q`; confirm failure.
- [ ] **Step 3: Implement JSON/JSONL loading** and dataset hash validation; never copy question text or evidence text to output.
- [ ] **Step 4: Implement deterministic label precedence**: infrastructure/reason first, then permission/lifecycle, then answerable-empty gate false reject, rejection-nonempty gate false accept, returned-without-gold retrieval miss, otherwise manual review.
- [ ] **Step 5: Add CLI** accepting `--cases-file`, `--retrieval-results`, `--asset-audit`, and `--out`.
- [ ] **Step 6: Run tests and ruff.**
- [ ] **Step 7: Commit** the attribution script and tests.

### Task 4: 二维 dev 阈值扫描

**Files:**
- Modify: `scripts/retrieval_threshold_sweep.py`
- Modify: `tests/eval/test_retrieval_threshold_sweep.py`
- Create: `eval_out/phase3_threshold_sweep_2d.json`
- Create: `config/calibration/cosine-gate-2026-10-14.json` after selection

**Interfaces:**
- `run_threshold_sweep(base_thresholds, vector_only_thresholds, cases_file, public_index, private_index, rounds) -> dict[str, object]`.
- Every row records both thresholds, score-space identity, quality metrics, rejection metrics, safety counts, candidate partition counts, latency, and case IDs.

- [ ] **Step 1: Extend failing tests** for deterministic two-dimensional ordering, invalid `vector_only_threshold < base_threshold`, and preservation of safety denominators.
- [ ] **Step 2: Run the focused sweep tests** and confirm failure.
- [ ] **Step 3: Implement the grid** with base values `0.68..0.82` and vector-only values from base through `0.90`, using the actual `RetrievalService` and current OpenSearch seams.
- [ ] **Step 4: Record mapping score space** and reject the sweep if it is not `cosinesimil` for the current physical indexes.
- [ ] **Step 5: Run the full dev grid** with five measured rounds and save the raw JSON result; do not read the frozen test set in this command.
- [ ] **Step 6: Select the lowest-risk Pareto point** satisfying zero safety violations, then update `config/calibration/cosine-gate-2026-10-14.json` with model, index versions, score space, both thresholds, and dev dataset hash.
- [ ] **Step 7: Commit** the two-dimensional sweep and selected calibration artifact.

### Task 5: 冻结 test 复评与最终报告

**Files:**
- Modify: `scripts/build_evaluation_report.py`
- Create: `eval_out/phase3_failure_attribution.json`
- Create: `eval_out/phase3_final_retrieval/retrieval_eval_results.json`
- Create: `eval_out/phase3_final_evaluation_manifest.json`
- Create: `docs/evaluation/retrieval-gate-optimization-2026-10-14.md`
- Create: `tests/eval/test_retrieval_gate_optimization_report.py`

**Interfaces:**
- Final report preserves pre/post metrics, selected thresholds, mapping/record score-space identity, failure labels and denominators.
- `build_report` rejects artifacts with mismatched test hash, index identity, embedding model, or score space.

- [ ] **Step 1: Write failing report tests** for missing attribution, mismatched selected threshold identity, and valid combined artifacts.
- [ ] **Step 2: Run the focused report tests** and confirm failure.
- [ ] **Step 3: Run the selected calibration artifact on the frozen 120-case test set** with multiple measured rounds; do not change thresholds during this run.
- [ ] **Step 4: Run the failure attribution CLI** against the new raw test result and asset audit.
- [ ] **Step 5: Build the final Markdown and JSON reports** with pre/post Recall@3, Precision@3, MRR, nDCG@3, rejection accuracy, false-reject rate, safety counts, citation metrics, latency, and failure labels.
- [ ] **Step 6: Manually review only unresolved labels** and record them as review items without changing gold data.
- [ ] **Step 7: Run** `.venv/Scripts/python.exe -m pytest tests/eval tests/test_rejection_gate.py -q`, then `.venv/Scripts/python.exe -m pytest -q`, then ruff on all modified files.
- [ ] **Step 8: Commit** the final reports and report tests.

## Self-Review Checklist

- The test set is never passed to the threshold-selection command.
- Existing vector-only retrieval behavior remains available through the stricter threshold rather than being removed outright.
- Mapping, configured gate space, and record space are all checked before a strict gate is applied.
- Diagnostics contain IDs, scores, counts and identities only; no evidence正文, patient正文 or vectors.
- Failure labels are deterministic and unresolved cases remain visible for manual review.
