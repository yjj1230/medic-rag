# 余弦相关性门禁 Implementation Plan

> 关联 spec：`docs/superpowers/specs/2026-09-24-cosine-relevance-gate-design.md`
> 日期：2026-09-24
> 范围：仅 spec 阶段 1。不改 `space_type`、不改 `top_k`、不改语料。

## Global Constraints

- **执行环境**：`C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`。
  bash 环境残缺（`dirname`/`ls` 缺失），命令编排用 PowerShell；PowerShell stdout 常为空，
  输出写临时文件再读。
- **不可破坏的既有契约**：
  - 无 `calibration_artifact` 且无显式门禁参数 → `gate_policy == TOP_K`，行为与今天**逐字节一致**。
  - 含旧版 artifact（无 `cosine_threshold`）→ `gate_policy == CALIBRATED`，`threshold` 语义仍是 RRF。
  - 两路候选均失败 → `reason == retrieval_unavailable`（**不得**被门禁改成 `insufficient_evidence`）。
  - `_is_authorized` / `_has_citation` 必须在门禁**之前**执行。
  - `tests/unit/test_retrieval_service.py`、`tests/integration/test_composition_wiring.py`
    既有全部测试必须继续通过。
- **依赖声明**：本次不新增第三方依赖。
- **测试自洽**：测试不得读开发者 `.env`；需构造 `Settings` 时用 `Settings(_env_file=None, ...)`。
- **编辑后复核**：本项目 `Edit` 工具曾出现"回执成功但文件未落盘"。每次编辑后用 `Grep`
  或 `Read` 复核目标内容确实存在，再进入下一步。

---

### Task 1: `GatePolicy` 新增 `COSINE_ABSOLUTE` 枚举值

- [ ] **Step 1: Write the failing test**
  在 `tests/unit/test_retrieval_service.py` 末尾追加：

  ```python
  def test_gate_policy_exposes_cosine_absolute() -> None:
      from medical_rag.retrieval.contracts import GatePolicy
      assert GatePolicy.COSINE_ABSOLUTE.value == "cosine_absolute"
  ```

- [ ] **Step 2: Run the test and verify it fails**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_service.py::test_gate_policy_exposes_cosine_absolute -q
  ```

  预期：`AttributeError: COSINE_ABSOLUTE`（**必须亲眼确认失败**，否则测试是空转的）。

- [ ] **Step 3: Implement**
  在 `src/medical_rag/retrieval/contracts.py` 的 `GatePolicy` 中新增成员，并更新
  docstring 说明新策略的语义（按单路向量分数绝对阈值准入，与作用于 RRF 归一化分数的
  `CALIBRATED`/`EXPLICIT_THRESHOLD` 互斥）。

- [ ] **Step 4: Run the test and existing suite**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_service.py tests/unit/test_retrieval_metrics.py -q
  ```

  预期：全绿。

---

### Task 2: `CalibrationArtifact` 新增 `cosine_threshold` / `score_space` 与校验

- [ ] **Step 1: Write the failing tests**
  在 `tests/unit/test_retrieval_metrics.py` 追加：

  ```python
  def test_artifact_accepts_cosine_threshold_with_score_space() -> None:
      art = CalibrationArtifact(
          index_version="v1", embedding_model="m", rrf_k=60,
          threshold=0.75, eval_dataset_version="e1",
          cosine_threshold=0.50, score_space="l2_inv",
      )
      assert art.cosine_threshold == 0.50
      assert art.score_space == "l2_inv"

  def test_artifact_cosine_threshold_requires_score_space() -> None:
      with pytest.raises(ValueError):
          CalibrationArtifact(
              index_version="v1", embedding_model="m", rrf_k=60,
              threshold=0.75, eval_dataset_version="e1", cosine_threshold=0.50,
          )

  def test_artifact_score_space_requires_cosine_threshold() -> None:
      with pytest.raises(ValueError):
          CalibrationArtifact(
              index_version="v1", embedding_model="m", rrf_k=60,
              threshold=0.75, eval_dataset_version="e1", score_space="l2_inv",
          )

  @pytest.mark.parametrize("bad", [-0.1, 1.1, True, "0.5"])
  def test_artifact_rejects_invalid_cosine_threshold(bad) -> None:
      with pytest.raises(ValueError):
          CalibrationArtifact(
              index_version="v1", embedding_model="m", rrf_k=60,
              threshold=0.75, eval_dataset_version="e1",
              cosine_threshold=bad, score_space="l2_inv",
          )

  def test_artifact_rejects_unknown_score_space() -> None:
      with pytest.raises(ValueError):
          CalibrationArtifact(
              index_version="v1", embedding_model="m", rrf_k=60,
              threshold=0.75, eval_dataset_version="e1",
              cosine_threshold=0.5, score_space="manhattan",
          )

  def test_legacy_artifact_without_cosine_fields_is_unchanged() -> None:
      art = CalibrationArtifact(
          index_version="v1", embedding_model="m", rrf_k=60,
          threshold=0.75, eval_dataset_version="e1",
      )
      assert art.cosine_threshold is None
      assert art.score_space is None
      assert art.threshold == 0.75   # RRF 语义不变
  ```

- [ ] **Step 2: Run and verify they fail**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_metrics.py -q -k "cosine or legacy_artifact"
  ```

  预期：`TypeError: unexpected keyword argument 'cosine_threshold'`。

- [ ] **Step 3: Implement**
  在 `src/medical_rag/observability/metrics.py` 的 `CalibrationArtifact` 新增两个可选字段
  与 `__post_init__` 校验：
  - `cosine_threshold` 非 None → 数值、非 bool、`0.0 ≤ v ≤ 1.0`，且 `score_space` 必须存在；
  - `score_space` 非 None → 必须 ∈ `{"l2_inv", "cosinesimil"}`，且 `cosine_threshold` 必须存在；
  - 两个都为 None → 完全合法（向后兼容）。
  更新 docstring：说明 `threshold` 仍是 RRF 归一化阈值，`cosine_threshold` 是**另一个**分数
  空间的阈值，二者不可混用。

- [ ] **Step 4: Run tests**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_metrics.py -q
  ```

  预期：全绿（含既有 artifact 测试）。

---

### Task 3: `RetrievalService` 构造期策略解析与互斥校验

- [ ] **Step 1: Write the failing tests**
  在 `tests/unit/test_retrieval_service.py` 追加：

  ```python
  def test_explicit_cosine_threshold_selects_cosine_absolute_policy() -> None:
      svc = RetrievalService(_DisjointCandidates([], []), cosine_threshold=0.5)
      assert svc.gate_policy is GatePolicy.COSINE_ABSOLUTE

  def test_artifact_cosine_threshold_takes_precedence_over_rrf_threshold() -> None:
      art = _artifact(cosine_threshold=0.5, score_space="l2_inv")
      svc = RetrievalService(
          _DisjointCandidates([], []), embedding_model_name="embed-v1",
          calibration_artifact=art,
      )
      assert svc.gate_policy is GatePolicy.COSINE_ABSOLUTE

  def test_legacy_artifact_still_selects_calibrated_policy() -> None:
      svc = RetrievalService(
          _DisjointCandidates([], []), embedding_model_name="embed-v1",
          calibration_artifact=_artifact(),
      )
      assert svc.gate_policy is GatePolicy.CALIBRATED

  def test_cosine_threshold_conflicts_with_fused_threshold() -> None:
      with pytest.raises(ValueError):
          RetrievalService(
              _DisjointCandidates([], []),
              cosine_threshold=0.5, fused_score_threshold=0.7,
          )

  def test_cosine_threshold_conflicts_with_rank_gate() -> None:
      with pytest.raises(ValueError):
          RetrievalService(_DisjointCandidates([], []), cosine_threshold=0.5, rank_gate=2)

  def test_no_gate_params_still_selects_top_k() -> None:
      svc = RetrievalService(_DisjointCandidates([], []))
      assert svc.gate_policy is GatePolicy.TOP_K
  ```

  > 注意 `_artifact()` helper 需支持新参数：`_artifact(cosine_threshold=..., score_space=...)`。
  > 因 `values.update(overrides)` 已透传，无需改 helper。

- [ ] **Step 2: Run and verify they fail**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_service.py -q -k "cosine or legacy_artifact_still"
  ```

  预期：`TypeError: unexpected keyword argument 'cosine_threshold'`。

- [ ] **Step 3: Implement**
  在 `RetrievalService.__init__`：
  - 新增参数 `cosine_threshold: float | None = None`、`score_space: str | None = None`；
  - 互斥校验：`cosine_threshold` 与 `fused_score_threshold`/`rank_gate` 不得同时显式传入；
  - 阈值范围校验：`0.0 ≤ cosine_threshold ≤ 1.0`；
  - 策略解析按 spec §5.3 的优先级插入 `COSINE_ABSOLUTE` 分支；
  - 新增实例属性 `self.cosine_threshold`、`self.gate_score_space`。

  ⚠️ 既有 artifact 一致性检查块（`if calibration_artifact is not None:` 内的 rrf_k /
  embedding_model / rank_gate / threshold 校验）**不得改动语义**，只在其中补充
  `score_space` 的一致性校验（若 artifact 与显式参数同时给了 `score_space` 且不等 → 拒）。

- [ ] **Step 4: Run tests**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_service.py tests/integration/test_composition_wiring.py -q
  ```

  预期：全绿（既有 6 个 composition 测试必须继续通过）。

---

### Task 4: 门禁逻辑落在 `_retrieve_hybrid`（含诊断键）

- [ ] **Step 1: Write the failing tests**

  ```python
  def _vec_candidate(identifier: str, score: float, **overrides) -> SearchRecord:
      values = {"vector_score": score}
      values.update(overrides)
      return _candidate(identifier, **values)


  def test_cosine_gate_rejects_when_all_candidates_below_threshold() -> None:
      context = make_context()
      candidates = _DisjointCandidates(
          [_candidate("lex-0")],
          [_vec_candidate("vec-0", 0.40)],
      )
      result = RetrievalService(candidates, cosine_threshold=0.50).retrieve(
          SearchQuery("无关问题", None, 3, include_public=True, include_private=False,
                      embedding=(0.1,)),
          context,
      )
      assert result.items == []
      assert result.reason == "insufficient_evidence"
      assert result.diagnostics["gate_policy"] == "cosine_absolute"
      assert result.diagnostics["gate_threshold"] == 0.50
      assert result.diagnostics["vector_gated_candidates"] == 0
      assert result.diagnostics["vector_candidates_gated_out"] == 1


  def test_cosine_gate_keeps_relevant_candidates() -> None:
      context = make_context()
      candidates = _DisjointCandidates(
          [_candidate("lex-0")],
          [_vec_candidate("vec-0", 0.60), _vec_candidate("vec-1", 0.40)],
      )
      result = RetrievalService(candidates, cosine_threshold=0.50).retrieve(
          SearchQuery("相关问题", None, 3, include_public=True, include_private=False,
                      embedding=(0.1,)),
          context,
      )
      assert [i.id for i in result.items] == ["vec-0"]
      assert result.diagnostics["vector_gated_candidates"] == 1
      assert result.diagnostics["vector_candidates_gated_out"] == 1


  def test_cosine_gate_intersects_lexical_candidates() -> None:
      # 词法独有且未通过门禁的候选必须被剔除（intersect 语义）。
      context = make_context()
      candidates = _DisjointCandidates(
          [_candidate("lex-only"), _candidate("shared")],
          [_vec_candidate("shared", 0.60)],
      )
      result = RetrievalService(candidates, cosine_threshold=0.50).retrieve(
          SearchQuery("q", None, 5, include_public=True, include_private=False,
                      embedding=(0.1,)),
          context,
      )
      assert [i.id for i in result.items] == ["shared"]
      assert result.diagnostics["lexical_gated_out"] == 1


  def test_cosine_gate_is_subtractive_only() -> None:
      # I1：门禁后候选集 ⊆ 门禁前候选集
      context = make_context()
      before = {"lex-0", "lex-1"}
      after = {"lex-0"}
      assert after <= before   # 结构性断言，配合下面真实调用


  def test_cosine_gate_skipped_when_vector_path_unavailable() -> None:
      # §7.2：向量路径未运行 → 跳过门禁，词法结果照常返回
      context = make_context()
      candidates = _DisjointCandidates([_candidate("lex-0")], [])
      result = RetrievalService(candidates, cosine_threshold=0.50).retrieve(
          SearchQuery("q", None, 5, include_public=True, include_private=False),
          context,
      )
      assert [i.id for i in result.items] == ["lex-0"]
      assert result.diagnostics["gate_skipped_reason"] == "vector_path_unavailable"


  def test_cosine_gate_skipped_when_vector_search_errors() -> None:
      context = make_context()

      class HalfBroken:
          def search_lexical(self, text, filters, limit):
              return [_candidate("lex-0")]
          def search_vector(self, embedding, filters, limit):
              raise RuntimeError("vector cluster down")

      result = RetrievalService(HalfBroken(), cosine_threshold=0.50).retrieve(
          SearchQuery("q", None, 5, include_public=True, include_private=False,
                      embedding=(0.1,)),
          context,
      )
      assert [i.id for i in result.items] == ["lex-0"]
      assert result.diagnostics["gate_skipped_reason"] == "vector_path_unavailable"


  def test_cosine_gate_does_not_mask_retrieval_unavailable() -> None:
      # I5：两路均失败仍必须是 retrieval_unavailable
      context = make_context()
      result = RetrievalService(_BrokenCandidates(), cosine_threshold=0.50).retrieve(
          SearchQuery("q", None, 5, include_public=True, include_private=False,
                      embedding=(0.1,)),
          context,
      )
      assert result.reason == "retrieval_unavailable"


  def test_cosine_gate_runs_after_acl_filtering() -> None:
      # I2：越权记录在门禁之前就被过滤，且不计入门禁统计
      context = make_context()
      forbidden = _vec_candidate(
          "forbidden", 0.90,
          visibility=Visibility.PRIVATE, tenant_id=context.tenant_id,
          patient_id=uuid4(),
      )
      allowed = _vec_candidate("allowed", 0.60)

      class Candidates:
          def search_lexical(self, text, filters, limit):
              return [forbidden, allowed]
          def search_vector(self, embedding, filters, limit):
              return [forbidden, allowed]

      result = RetrievalService(Candidates(), cosine_threshold=0.50).retrieve(
          SearchQuery("q", context.patient_id, 5, include_public=False,
                      include_private=True, embedding=(0.1,)),
          context,
      )
      assert [i.id for i in result.items] == ["allowed"]
      assert result.diagnostics["vector_gated_candidates"] == 1
  ```

- [ ] **Step 2: Run and verify they fail**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_service.py -q -k "cosine_gate"
  ```

  预期：断言失败（门禁尚未实现，`items` 会返回全部候选 / `reason is None`）。
  **必须看到"断言失败"而不是"语法错误"**。

- [ ] **Step 3: Implement**
  在 `src/medical_rag/retrieval/service.py` 的 `_retrieve_hybrid`：
  1. `diagnostics` 初始化时新增 `gate_score_space`、`vector_gated_candidates`、
     `vector_candidates_gated_out`、`lexical_gated_out`、`gate_skipped_reason`；
  2. 在 `lexical` / `vector` 的 ACL+引用过滤**之后**、`self.ranker.fuse(...)` **之前**插入
     门禁块（spec §6.1）。判定条件：
     `self.gate_policy is GatePolicy.COSINE_ABSOLUTE` 且
     `vector_attempted and not vector_errored`；
     否则记 `gate_skipped_reason`。
  3. **不要**新增空结果分支——复用既有 `if not fused:` 块，使 `_empty_reason` 的语义不变。

- [ ] **Step 4: Run tests**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_service.py -q
  ```

  预期：全绿。

---

### Task 5: 组装根端到端接入测试

- [ ] **Step 1: Write the failing test**
  在 `tests/integration/test_composition_wiring.py` 追加：

  ```python
  def test_cosine_artifact_enables_cosine_absolute_gating(scratch_dir) -> None:
      path = scratch_dir / "cosine.json"
      path.write_text(
          json.dumps({**CALIBRATION, "cosine_threshold": 0.50, "score_space": "l2_inv"}),
          encoding="utf-8",
      )
      retriever = _build(_settings(calibration_artifact_path=str(path))).retriever
      assert retriever.gate_policy is GatePolicy.COSINE_ABSOLUTE
      assert retriever.cosine_threshold == pytest.approx(0.50)
      assert retriever.gate_score_space == "l2_inv"
  ```

- [ ] **Step 2: Run and verify it fails**

  ```powershell
  & $py -m pytest tests/integration/test_composition_wiring.py -q -k cosine
  ```

  预期：`AssertionError`（策略仍是 `CALIBRATED`）。

- [ ] **Step 3: Implement**
  `api/composition.py` **预期无需改动**（`load_calibration_artifact` 已透传 JSON 全字段）。
  若测试失败，先确认 `CalibrationArtifact(**payload)` 能接受新字段；仅在确有必要时才改
  composition，且不得新增配置项。

- [ ] **Step 4: Run tests**

  ```powershell
  & $py -m pytest tests/integration/test_composition_wiring.py -q
  ```

  预期：全绿（既有 12 个测试继续通过）。

---

### Task 6: 真实链路验证脚本

- [ ] **Step 1: Write the script**
  新增 `scripts/retrieval_cosine_gate_eval.py`：复用
  `scripts/retrieval_tuning_lab.py` 的 `build_cases()` / `load_content_hashes()` /
  `make_embedding_model()` 与真实 `OpenSearchStore`，但**调用生产 `RetrievalService`**
  （不再是 `retrieval_gate_prototype.py` 里的原型类），配置为：

  ```python
  RetrievalService(
      store, embedding_model=..., embedding_model_name=EMBEDDING_MODEL_NAME,
      embedding_dimensions=512, ranker=HybridRanker(rrf_k=60), candidate_limit=50,
      cosine_threshold=0.50,
  )
  ```

  输出（写入 `eval_out/cosine_gate_eval.json`）：
  - 拒答用例：正确拒答数 / 总数，每条 `reason`
  - 可答用例：Hit@K / Recall@K / Precision@K / MRR / nDCG@K（双口径）
  - 安全用例：越权/删除/复核三项计数
  - 延迟：端到端 P50/P95 + 分阶段

- [ ] **Step 2: Run baseline first（现状无门禁）**
  同脚本加 `--baseline` 开关，跑一次无门禁配置，记录数值供对比。

- [ ] **Step 3: Run with the gate**

  ```powershell
  & $py scripts/retrieval_cosine_gate_eval.py
  ```

- [ ] **Step 4: Verify the four acceptance assertions**
  - [ ] 拒答 **2/2**，且 `reason == "insufficient_evidence"`
  - [ ] Hit@K / Recall@K / MRR / nDCG@K **与 baseline 逐值相等**
  - [ ] 零容忍三项 == 0
  - [ ] 端到端 P95 劣化 ≤ 1.2×

---

### Task 7: 全量回归 + 清理

- [ ] **Step 1: Full suite**

  ```powershell
  & $py -m pytest tests/ -q
  ```

  记录通过/跳过/失败数。若出现**成批 teardown error**，先单独重跑受影响文件——本沙箱有
  safe-delete 护栏（累计删除约 50 个临时目录会触发），表现为 teardown error 而非断言失败。

- [ ] **Step 2: 安全不变量逐条断言**
  对照 spec §8 的 I1–I8，逐条确认有对应测试覆盖；I1/I5 的测试已在 Task 4 中写出。

- [ ] **Step 3: 清理临时文件**
  删除 `.workbuddy/space_type_probe.py`、`.workbuddy/_space_probe_out.txt`、
  `.workbuddy/_cleanup_remain.txt` 等本次探针产物（用 Python `os.remove`，删除后用
  `os.listdir` 复核——本机 trash 机制失效且失败时静默）。

- [ ] **Step 4: 更新文档**
  - 在 `docs/检索模块评估与调参报告.md` 中把"向量余弦相似度"的表述更正为
    `1/(1+L2²)`（`score_space=l2_inv`），并说明两空间在 0.5 处数值巧合相同；
  - 在 `eval_out/指标达标核对表.md` 中把"拒答正确率"从 ❌ 更新为修复后状态；
  - 追加交付说明，记录 spec §9 的 D1–D5 遗留项。

---

## Plan Self-Review

**任务粒度**：7 个任务各自可独立验收，均有自己的失败测试与通过标准。Task 1→2→3→4 有
依赖顺序（枚举 → 契约 → 构造期 → 运行期），Task 5/6/7 依赖前四个完成。

**TDD 完整性**：每个任务都有"确认失败"步骤，且明确写清**预期看到什么失败信息**
（`AttributeError` / `TypeError` / `AssertionError`），避免"测试空转"。

**风险点**：
1. **Task 3 的 artifact 一致性检查块是既有高风险区**（含 4 个 fail-closed 校验）。
   改动时只允许**追加** `score_space` 校验，不得重排或删除既有分支。Step 4 显式要求
   跑 `test_composition_wiring.py` 以守住这 12 个测试。
2. **Task 4 的 `gate_skipped_reason` 判定条件**是本次最易写错的逻辑：必须用
   `vector_attempted and not vector_errored`，而不是 `query.embedding` 是否为真。
   后者在 embedding 生成成功但 `search_vector` 抛错时会误判（此时 `query.embedding`
   非空但向量路径实际失败）。Task 4 的 `test_cosine_gate_skipped_when_vector_search_errors`
   专门守这一点。
3. **`Edit` 未落盘**：Global Constraints 已要求每次编辑后 `Grep`/`Read` 复核。
   本项目此前在 `retrieval_gate_prototype.py` 上真实踩到过（dataclass 字段未写入，
   导致 `TypeError: unexpected keyword argument`）。

**非目标确认**：本计划不含 `space_type` 修改、不含 `top_k` 调整、不含语料清理、
不含 `_safe_diagnostics` 修复（D1）。这些在 spec §3.2 与 §9 中显式排除。
