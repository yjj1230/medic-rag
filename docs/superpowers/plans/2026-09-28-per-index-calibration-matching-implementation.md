# 按索引分派的标定一致性校验 Implementation Plan

> 关联 spec：`docs/superpowers/specs/2026-09-28-per-index-calibration-matching-design.md`
> 发现现场：`eval_out/门禁启用端到端验证报告.md`
> 日期：2026-09-28
> 范围：仅 spec **阶段 1**（按索引分派校验 + 测试空洞闭合 + 真实链路验证）。
> **不改** `embedding_model` 校验方式、**不改** `space_type`、**不改** `top_k`、**不改** 语料、
> **不改** `CALIBRATION_ARTIFACT_PATH` 的部署状态（spec §3.2）。

## Global Constraints

- **执行环境**：`C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`。
  bash 残缺（`dirname`/`ls`/`cat` 缺失，退出码 127），命令编排用 PowerShell；
  PowerShell stdout 常为空 → 输出写临时文件再读。打印中文前先
  `[Console]::OutputEncoding=[Text.Encoding]::UTF8`。
- **不可破坏的既有契约**：
  - 旧 artifact（无 `public_index_version`/`private_index_version`）→ 全局单一版本比对，
    行为**逐字节不变**（含"双索引下仍 fail-closed"这一既有语义）。
  - `embedding_model` 校验**保持全局**（向量空间身份，不因索引而异）。
  - 一致性检查的**位置不变**：ACL/引用过滤之后、余弦门禁之前（spec §6.1）。
  - 校验**只做减法**，不新增"跳过校验"分支；无可解析期望版本时 fail-closed。
  - 两路候选均失败仍必须是 `retrieval_unavailable`，不得被改成 `insufficient_evidence`。
  - `tests/unit/test_retrieval_service.py::test_calibrated_artifact_version_mismatch_fails_closed`
    与 `tests/integration/test_scheme_b_rollout.py` 全部测试**继续通过且不作任何修改**（I4）。
- **依赖声明**：本次不新增第三方依赖（`pyproject.toml` 不动）。
- **测试自洽**：测试不得读开发者 `.env`；构造 `Settings` 时用 `Settings(_env_file=None, ...)`。
- **编辑后复核**：本项目 `Edit` 工具曾出现"回执成功但文件未落盘"。每次编辑后用
  `Grep`/`Read` 复核目标内容确实存在，再进入下一步。
- **可见性取值口径**：`Visibility` 是 `StrEnum`，`str(Visibility.PUBLIC) == "public"`。
  实现统一用 **`str(record.visibility)`** 而非 `record.visibility.value`——后者在
  "非枚举可见性"（自定义 `SearchIndex` 返回裸字符串）的防御路径上会抛
  `AttributeError`，把一个 fail-closed 场景变成 500。spec §5.3 的伪码用 `str(...)`，
  本计划与该口径一致。
- **既有测试的空洞（务必记住）**：`_DisjointCandidates` 两路同源、`_artifact()` 默认
  `index_version="public-v7"`，因此"公共与私有版本不同"从未被构造过。本计划所有新增
  服务测试**必须显式设置 `visibility` 与 `index_version`**。
- **验收基线**：全量测试 730 passed / 3 skipped；真实链路 `scripts/retrieval_cosine_gate_eval.py`
  上的"每查询返回 0 条 + `calibration_artifact_mismatch`"复现次数为 **16**。
  > ⚠️ **实施期更正（2026-09-28）**：`16` 是写计划时的**未验证估值**，实施后实测为
  > **6 个用例 / 42 条记录 / 5 个混合作用域零返回**（见 `eval_out/cosine_gate_eval.json`
  > 的 `legacy_demo` 块）。验收脚本断言文本已改用实测值，`16` 不再出现于任何产物中。
  > 教训：计划里的量化预期要么先测，要么标注为估值——不要写成看起来已核实的样子。

---

### Task 1: `CalibrationArtifact` 新增 `public_index_version` / `private_index_version` + 成对校验

- [x] **Step 1: Write the failing tests**
  在 `tests/unit/test_retrieval_metrics.py` 末尾追加（沿用文件内既有的 `_artifact()` helper）：

  ```python
  # ---------------------------------------------------------------------------
  # CalibrationArtifact：按索引分派的 index_version
  # spec 2026-09-28-per-index-calibration-matching-design.md §5.1
  # ---------------------------------------------------------------------------


  def test_artifact_accepts_paired_index_versions() -> None:
      art = _artifact(
          public_index_version="public-v1", private_index_version="private-v1"
      )
      assert art.public_index_version == "public-v1"
      assert art.private_index_version == "private-v1"
      # 既有 index_version 字段被保留（退化为 artifact 标识元数据）
      assert art.index_version == "v1"


  def test_artifact_rejects_half_configured_index_versions() -> None:
      """半配置是不可能正确的状态：另一侧期望版本无从得知 → 构造期拒绝。"""
      with pytest.raises(ValueError, match="pair"):
          _artifact(public_index_version="public-v1")
      with pytest.raises(ValueError, match="pair"):
          _artifact(private_index_version="private-v1")


  @pytest.mark.parametrize("blank", ["", "   "])
  def test_artifact_rejects_blank_index_versions(blank: str) -> None:
      with pytest.raises(ValueError):
          _artifact(public_index_version=blank, private_index_version="private-v1")
      with pytest.raises(ValueError):
          _artifact(public_index_version="public-v1", private_index_version=blank)


  def test_legacy_artifact_without_index_version_pair_is_unchanged() -> None:
      """旧 artifact（两字段均缺省）行为逐字节不变：全局 index_version 语义保留。"""
      art = _artifact()
      assert art.public_index_version is None
      assert art.private_index_version is None
      assert art.index_version == "v1"
  ```

- [x] **Step 2: Run the tests and verify they fail**

  ```powershell
  $py = "C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
  & $py -m pytest tests/unit/test_retrieval_metrics.py -q -k "index_versions or half_configured or blank_index"
  ```

  预期：`TypeError: CalibrationArtifact.__init__() got an unexpected keyword argument 'public_index_version'`
  对 `test_artifact_accepts_paired_index_versions` 与 `test_legacy_artifact_without_index_version_pair_is_unchanged`
  报错；两个 `pytest.raises(ValueError, match="pair")` 因为拿到的是 `TypeError` 而**失败**。
  **必须亲眼确认这些是"因缺少新契约而失败"，而不是语法错误。**

- [x] **Step 3: Implement**
  在 `src/medical_rag/observability/metrics.py` 的 `CalibrationArtifact`：

  1. 新增两个字段（放在 `score_space` 之后，均为关键字默认 `None`，不动既有字段位置）：

     ```python
     # --- 按索引分派的 index_version（可选；二者必须成对出现）---
     #: 公共索引记录的期望 ``index_version``。提供后，公共记录与它比对、
     #: 私有记录与 ``private_index_version`` 比对，**替代**全局 ``index_version`` 比对。
     public_index_version: str | None = None
     #: 私有索引记录的期望 ``index_version``。与 ``public_index_version`` 成对必填。
     private_index_version: str | None = None
     ```

  2. `__post_init__` **末尾追加** `self._validate_index_versions()`（追加而非插入，
     保证既有非法输入的报错顺序不变）：

     ```python
     def _validate_index_versions(self) -> None:
         """两索引期望版本必须成对提供，且各自为非空字符串。

         「只给公共侧」或「只给私有侧」是不可能正确的状态——另一侧索引的期望版本
         无从得知，任何命中该侧的查询都必然 fail-closed。与
         ``cosine_threshold``/``score_space`` 的成对约束同构：拒绝，而不是猜测调用方意图。
         """
         public, private = self.public_index_version, self.private_index_version
         if public is None and private is None:
             return
         if public is None or private is None:
             raise ValueError(
                 "public_index_version and private_index_version must be provided as a pair"
             )
         for name in ("public_index_version", "private_index_version"):
             value = getattr(self, name)
             if not isinstance(value, str) or not value.strip():
                 raise ValueError(f"{name} must be a non-empty string")
     ```

  3. 更新类 docstring：显式写明 **`index_version` 的语义收窄**——
     "当 `public_index_version`/`private_index_version` 均未提供时，`index_version` 是
     全局期望版本（旧行为）；提供后它退化为 artifact 标识元数据，不再参与逐记录比对。"

- [x] **Step 4: Run the tests and existing suite**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_metrics.py -q
  ```

  预期：全绿（含既有 6 个 cosine/legacy artifact 测试）。

---

### Task 2: `CalibrationArtifact.expected_index_version_for(visibility)`

- [x] **Step 1: Write the failing tests**
  在 `tests/unit/test_retrieval_metrics.py` 继续追加：

  ```python
  def test_expected_index_version_dispatches_per_visibility() -> None:
      art = _artifact(
          public_index_version="public-v1", private_index_version="private-v1"
      )
      assert art.expected_index_version_for("public") == "public-v1"
      assert art.expected_index_version_for("private") == "private-v1"


  def test_expected_index_version_is_global_for_legacy_artifact() -> None:
      """旧 artifact：任何可见性都取全局 index_version（行为不变）。"""
      art = _artifact()   # index_version="v1"
      assert art.expected_index_version_for("public") == "v1"
      assert art.expected_index_version_for("private") == "v1"


  @pytest.mark.parametrize("unknown", ["", "legacy", "PUBLIC", "public "])
  def test_expected_index_version_returns_none_for_unknown_visibility(unknown: str) -> None:
      """无法判定归属 → 返回 None，调用方据此 fail-closed（不是跳过校验）。"""
      art = _artifact(
          public_index_version="public-v1", private_index_version="private-v1"
      )
      assert art.expected_index_version_for(unknown) is None


  def test_expected_index_version_returns_none_for_half_config_defensively() -> None:
      """半配置已由 __post_init__ 拒绝；此处用 object.__setattr__ 构造出该
      不可能状态，验证运行期防御分支确实返回 None（fail-closed），而不是抛异常。"""
      art = _artifact(
          public_index_version="public-v1", private_index_version="private-v1"
      )
      object.__setattr__(art, "private_index_version", None)
      assert art.expected_index_version_for("public") is None
      assert art.expected_index_version_for("private") is None
  ```

- [x] **Step 2: Run the tests and verify they fail**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_metrics.py -q -k "expected_index_version"
  ```

  预期：`AttributeError: 'CalibrationArtifact' object has no attribute 'expected_index_version_for'`。

- [x] **Step 3: Implement**
  在 `CalibrationArtifact` 上新增方法（放在 `_validate_index_versions` 之后）：

  ```python
  def expected_index_version_for(self, visibility: str) -> str | None:
      """返回某条记录（按可见性判定索引归属）应当匹配的 ``index_version``。

      返回 ``None`` 表示「无法解析期望值」——调用方**必须 fail-closed**，
      而不是跳过校验。``None`` 只出现在两种情形：
        * 半配置（已由 ``__post_init__`` 拒绝构造，运行期仅为防御）；
        * ``visibility`` 不是已知的 public/private。
      """
      if self.public_index_version is None and self.private_index_version is None:
          return self.index_version          # 旧 artifact：全局单一期望版本
      if self.public_index_version is None or self.private_index_version is None:
          return None                        # 半配置：不可解析（防御分支）
      if visibility == "public":
          return self.public_index_version
      if visibility == "private":
          return self.private_index_version
      return None                            # 归属未知：不可解析
  ```

  > 放在契约上而非服务里：这条规则属于"artifact 如何定义自己的适用范围"，
  > 放服务里会让契约与校验逻辑分居两处，未来第三处调用必然漏抄（spec §5.2）。

- [x] **Step 4: Run the tests**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_metrics.py -q
  ```

  预期：全绿。

---

### Task 3: `RetrievalService` 按可见性分派校验 + mismatch 诊断键（核心修复）

- [x] **Step 1: Write the failing tests**
  在 `tests/unit/test_retrieval_service.py` 末尾追加：

  ```python
  # ---------------------------------------------------------------------------
  # 按索引分派的标定一致性校验
  # spec 2026-09-28-per-index-calibration-matching-design.md §5.3 / §10.2
  # ---------------------------------------------------------------------------


  def _dual_index_artifact(**overrides) -> CalibrationArtifact:
      """带两索引期望版本的 artifact；threshold=0.4 使单路 rank-1（0.5）能过 RRF 门禁。"""
      return _artifact(
          threshold=0.4,
          public_index_version="public-v1",
          private_index_version="private-v1",
          **overrides,
      )


  def _private_candidate(identifier: str, context, **overrides) -> SearchRecord:
      return _candidate(
          identifier,
          visibility=Visibility.PRIVATE,
          tenant_id=context.tenant_id,
          patient_id=context.patient_id,
          **overrides,
      )


  def test_dual_index_versions_with_paired_artifact_serves_mixed_scope() -> None:
      """核心修复：混合作用域 + 两索引版本按设计不同 + 新 artifact → 正常返回。

      修复前：items == [] 且 reason == "calibration_artifact_mismatch"。
      """
      context = make_context()
      candidates = _DisjointCandidates(
          [
              _candidate("pub-0", index_version="public-v1", embedding_model="embed-v1"),
              _private_candidate(
                  "priv-0", context, index_version="private-v1", embedding_model="embed-v1"
              ),
          ],
          [],
      )
      result = RetrievalService(
          candidates,
          embedding_model_name="embed-v1",
          calibration_artifact=_dual_index_artifact(),
      ).retrieve(
          SearchQuery(
              "混合作用域",
              context.patient_id,
              5,
              include_public=True,
              include_private=True,
          ),
          context,
      )

      assert result.reason is None
      assert sorted(item.id for item in result.items) == ["priv-0", "pub-0"]
      assert "calibration_artifact_mismatch" not in result.diagnostics


  def test_paired_artifact_private_version_mismatch_fails_closed() -> None:
      context = make_context()
      candidates = _DisjointCandidates(
          [_private_candidate("priv-0", context, index_version="private-v9",
                              embedding_model="embed-v1")],
          [],
      )
      result = RetrievalService(
          candidates,
          embedding_model_name="embed-v1",
          calibration_artifact=_dual_index_artifact(),
      ).retrieve(
          SearchQuery("私有", context.patient_id, 5, include_public=True,
                      include_private=True),
          context,
      )

      assert result.items == []
      assert result.reason == "calibration_artifact_mismatch"


  def test_paired_artifact_public_version_mismatch_fails_closed() -> None:
      context = make_context()
      candidates = _DisjointCandidates(
          [_candidate("pub-0", index_version="public-v9", embedding_model="embed-v1")],
          [],
      )
      result = RetrievalService(
          candidates,
          embedding_model_name="embed-v1",
          calibration_artifact=_dual_index_artifact(),
      ).retrieve(
          SearchQuery("公共", None, 5, include_public=True, include_private=False),
          context,
      )

      assert result.items == []
      assert result.reason == "calibration_artifact_mismatch"


  def test_paired_artifact_embedding_model_mismatch_still_fails_closed() -> None:
      """I5：embedding_model 仍是全局校验，不因按索引分派而放宽。"""
      context = make_context()
      candidates = _DisjointCandidates(
          [_candidate("pub-0", index_version="public-v1", embedding_model="other-model")],
          [],
      )
      result = RetrievalService(
          candidates,
          embedding_model_name="embed-v1",
          calibration_artifact=_dual_index_artifact(),
      ).retrieve(
          SearchQuery("公共", None, 5, include_public=True, include_private=False),
          context,
      )

      assert result.items == []
      assert result.reason == "calibration_artifact_mismatch"


  def test_paired_artifact_unknown_visibility_fails_closed_with_identity() -> None:
      """I2：归属未知 → fail-closed；且诊断不得因裸字符串可见性而崩溃。"""
      context = make_context()
      candidates = _DisjointCandidates(
          [
              _candidate(
                  "weird",
                  visibility="legacy",          # type: ignore[arg-type]
                  tenant_id=context.tenant_id,
                  patient_id=context.patient_id,
                  index_version="public-v1",
                  embedding_model="embed-v1",
              )
          ],
          [],
      )
      result = RetrievalService(
          candidates,
          embedding_model_name="embed-v1",
          calibration_artifact=_dual_index_artifact(),
      ).retrieve(
          SearchQuery("未知归属", context.patient_id, 5, include_public=True,
                      include_private=True),
          context,
      )

      assert result.items == []
      assert result.reason == "calibration_artifact_mismatch"
      assert result.diagnostics["calibration_mismatch_count"] == 1
      assert result.diagnostics["calibration_mismatch_records"][0]["visibility"] == "legacy"


  def test_mismatch_diagnostics_carry_identity_without_text() -> None:
      """I8：可追溯（谁的、哪条、期望什么版本），但不泄漏正文。"""
      context = make_context()
      candidates = _DisjointCandidates(
          [
              _candidate("pub-bad", index_version="public-v9", embedding_model="embed-v1",
                         text="这是不该出现在诊断里的正文"),
              _candidate("pub-ok", index_version="public-v1", embedding_model="embed-v1"),
          ],
          [],
      )
      result = RetrievalService(
          candidates,
          embedding_model_name="embed-v1",
          calibration_artifact=_dual_index_artifact(),
      ).retrieve(
          SearchQuery("公共", None, 5, include_public=True, include_private=False),
          context,
      )

      assert result.diagnostics["calibration_mismatch_count"] == 1
      records = result.diagnostics["calibration_mismatch_records"]
      assert len(records) == 1
      entry = records[0]
      assert entry["id"] == "pub-bad"
      assert entry["visibility"] == "public"
      assert entry["index_version"] == "public-v9"
      assert entry["embedding_model"] == "embed-v1"
      assert "text" not in entry
      assert "这是不该出现在诊断里的正文" not in str(records)
  ```

- [x] **Step 2: Run the tests and verify they fail**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_service.py -q -k "paired_artifact or dual_index_versions or mismatch_diagnostics_carry"
  ```

  预期：
  - `test_dual_index_versions_with_paired_artifact_serves_mixed_scope` → **断言失败**
    （`result.items == []`，`reason == "calibration_artifact_mismatch"`）。
  - `test_paired_artifact_unknown_visibility_fails_closed_with_identity` →
    `KeyError: 'calibration_mismatch_count'`（诊断键尚未实现）。
  - `test_mismatch_diagnostics_carry_identity_without_text` → `KeyError` 同上。
  - 其余 `paired_artifact_*` 因 `_dual_index_artifact()` 抛
    `TypeError: unexpected keyword argument 'public_index_version'` 而报错——Task 1/2 完成后
    它们应能构造成功但仍断言失败。**必须看到失败/报错，而不是语法错误。**

- [x] **Step 3: Implement**
  在 `src/medical_rag/retrieval/service.py`：

  1. 把 `_retrieve_hybrid` 中的单一版本比对块（当前 `service.py:437-453`）替换为按可见性分派：

     ```python
     if self.calibration_artifact is not None:
         artifact = self.calibration_artifact
         mismatched = [
             record
             for record in (*lexical, *vector)
             if self._record_breaks_calibration(record, artifact)
         ]
         if mismatched:
             diagnostics["calibration_artifact_mismatch"] = True
             diagnostics["status"] = "degraded"
             # 可追溯：只报身份（id/可见性/索引版本/模型名），**不含正文**。
             diagnostics["calibration_mismatch_records"] = tuple(
                 {
                     "id": record.id,
                     "visibility": str(record.visibility),
                     "index_version": record.index_version,
                     "embedding_model": record.embedding_model,
                 }
                 for record in mismatched[:5]
             )
             diagnostics["calibration_mismatch_count"] = len(mismatched)
             diagnostics["stage_elapsed_ms"]["total"] = (perf_counter() - started) * 1000
             return RetrievalResult(
                 items=[],
                 reason=REASON_CALIBRATION_MISMATCH,
                 diagnostics=diagnostics,
             )
     ```

  2. 新增静态方法（放在 `_has_citation` 之后）：

     ```python
     @staticmethod
     def _record_breaks_calibration(
         record: SearchRecord, artifact: CalibrationArtifact
     ) -> bool:
         """记录是否破坏了「它所在索引」的标定身份。

         两个维度**独立**判定：
           * ``embedding_model`` → 全局统一（向量空间身份，不因索引而异）；
           * ``index_version``  → 按索引分派（两索引按设计可有不同版本）。
         期望版本无法解析（``None``）即视为破坏——fail-closed，不跳过校验。
         """
         if record.embedding_model != artifact.embedding_model:
             return True
         expected = artifact.expected_index_version_for(str(record.visibility))
         return expected is None or record.index_version != expected
     ```

  ⚠️ **只替换判据，不移动代码块位置**：块仍位于 ACL/引用过滤之后、余弦门禁之前
  （spec §6.1）。**不要**新增空结果分支，也**不要**改动
  `diagnostics["index_version"]`（`sorted(versions)[0]`，取融合后版本集合的最小值）——
  这是既有行为。

- [x] **Step 4: Run the tests**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_service.py -q
  ```

  预期：全绿，且**既有的** `test_calibrated_artifact_version_mismatch_fails_closed`
  未作任何修改仍通过（I4 护栏）。

---

### Task 4: 安全不变量护栏（I1 / I7 + 旧 artifact 双索引回归）

> 本任务**不含生产代码变更**，因此没有"确认失败"步骤：它把 spec §8 的不变量钉成
> 回归测试，其有效性由 Task 3 的实现保证。若任一护栏失败，说明 Task 3 破坏了不变量，
> 必须回退修复而不是放宽断言。

- [x] **Step 1: Write the guard tests**
  在 `tests/unit/test_retrieval_service.py` 追加：

  ```python
  def test_paired_artifact_passing_calibration_leaves_candidate_set_unchanged() -> None:
      """I1：校验只做减法。令门禁不产生任何削减（threshold=0.0），
      则融合前的候选 id 集合必须与输入逐 id 相等。"""
      context = make_context()
      candidates = _DisjointCandidates(
          [_candidate("pub-0", index_version="public-v1", embedding_model="embed-v1")],
          [_vec_candidate("vec-0", 0.60, index_version="public-v1",
                          embedding_model="embed-v1")],
      )
      result = RetrievalService(
          candidates,
          embedding_model_name="embed-v1",
          calibration_artifact=_dual_index_artifact(threshold=0.0),
      ).retrieve(
          SearchQuery("公共", None, 5, include_public=True, include_private=False,
                      embedding=(0.1,)),
          context,
      )

      assert result.reason is None
      assert sorted(result.diagnostics["candidate_ids"]) == ["pub-0", "vec-0"]
      assert result.diagnostics["fused_candidates"] == 2
      assert result.diagnostics["lexical_filtered"] == 0
      assert result.diagnostics["vector_filtered"] == 0


  def test_legacy_artifact_with_dual_index_versions_still_fails_closed() -> None:
      """I4 / §7.2：旧 artifact（无两字段）在双索引下语义不变——仍然 fail-closed。
      这是本次最重要的回归护栏：若实现里出现"让旧 artifact 也变可用"的副作用，本测试立刻失败。"""
      context = make_context()
      candidates = _DisjointCandidates(
          [
              _candidate("pub-0", index_version="public-v1", embedding_model="embed-v1"),
              _private_candidate(
                  "priv-0", context, index_version="private-v1", embedding_model="embed-v1"
              ),
          ],
          [],
      )
      result = RetrievalService(
          candidates,
          embedding_model_name="embed-v1",
          calibration_artifact=_artifact(threshold=0.4),   # index_version="public-v7"
      ).retrieve(
          SearchQuery("混合", context.patient_id, 5, include_public=True,
                      include_private=True),
          context,
      )

      assert result.items == []
      assert result.reason == "calibration_artifact_mismatch"
      assert result.diagnostics["calibration_mismatch_count"] == 2


  def test_unauthorized_record_absent_from_mismatch_diagnostics() -> None:
      """I7：越权记录在 ACL 过滤（校验之前）就被丢弃，不得出现在 mismatch 诊断中。"""
      context = make_context()
      forbidden = _candidate(
          "forbidden",
          visibility=Visibility.PRIVATE,
          tenant_id=context.tenant_id,
          patient_id=uuid4(),                    # 其他患者
          index_version="wrong",
          embedding_model="embed-v1",
      )
      allowed = _private_candidate("allowed", context, index_version="wrong",
                                   embedding_model="embed-v1")

      class Candidates:
          def search_lexical(self, text, filters, limit):  # noqa: ANN001
              del text, filters, limit
              return [forbidden, allowed]

          def search_vector(self, embedding, filters, limit):  # noqa: ANN001
              del embedding, filters, limit
              return [forbidden, allowed]

      result = RetrievalService(
          Candidates(),
          embedding_model_name="embed-v1",
          calibration_artifact=_dual_index_artifact(),
      ).retrieve(
          SearchQuery("私有", context.patient_id, 5, include_public=False,
                      include_private=True, embedding=(0.1,)),
          context,
      )

      assert result.reason == "calibration_artifact_mismatch"
      assert result.diagnostics["calibration_mismatch_count"] == 1
      assert [e["id"] for e in result.diagnostics["calibration_mismatch_records"]] == ["allowed"]
  ```

- [x] **Step 2: Run the guard tests**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_service.py -q -k "candidate_set_unchanged or legacy_artifact_with_dual or absent_from_mismatch"
  ```

  预期：**3 个全绿**。若 `test_legacy_artifact_with_dual_index_versions_still_fails_closed`
  失败 → Task 3 实现越界（把旧 artifact 也放宽了），必须回退。

- [x] **Step 3: Run the whole service + metrics suite**

  ```powershell
  & $py -m pytest tests/unit/test_retrieval_service.py tests/unit/test_retrieval_metrics.py tests/integration/test_scheme_b_rollout.py -q
  ```

  预期：全绿，`test_scheme_b_rollout.py` 未被修改。

---

### Task 5: 组装期接入（配对字段透传 + 半配置拒绝）

- [x] **Step 1: Write the failing test**
  在 `tests/integration/test_composition_wiring.py` 追加（沿用文件内 `CALIBRATION` / `_settings` / `_build`）：

  ```python
  def test_paired_index_versions_artifact_wires_through(scratch_dir) -> None:
      """双索引部署的正式 artifact 形态必须能被组装根原样接入。"""
      path = scratch_dir / "dual-index.json"
      path.write_text(
          json.dumps(
              {
                  **CALIBRATION,
                  "public_index_version": "public-v1",
                  "private_index_version": "private-v1",
                  "cosine_threshold": 0.50,
                  "score_space": "l2_inv",
              }
          ),
          encoding="utf-8",
      )
      retriever = _build(_settings(calibration_artifact_path=str(path))).retriever
      artifact = retriever.calibration_artifact
      assert artifact is not None
      assert artifact.public_index_version == "public-v1"
      assert artifact.private_index_version == "private-v1"
      # 两个维度：版本按索引分派，门禁口径切到向量分数
      assert artifact.expected_index_version_for("public") == "public-v1"
      assert retriever.gate_policy is GatePolicy.COSINE_ABSOLUTE


  def test_half_configured_index_versions_artifact_fails_fast(scratch_dir) -> None:
      """半配置必须在启动期失败（构造期 ValueError），而不是运行期全量拒答。"""
      path = scratch_dir / "half.json"
      path.write_text(
          json.dumps({**CALIBRATION, "public_index_version": "public-v1"}),
          encoding="utf-8",
      )
      with pytest.raises(ValueError, match="pair"):
          _build(_settings(calibration_artifact_path=str(path)))
  ```

  > **为什么第二个测试用 `match="pair"`**：修复前 `public_index_version` 是未知字段，
  > 组装根会把 `TypeError` 转成 `ValueError`（消息为 "incomplete or has unknown fields"），
  > 只断言 `pytest.raises(ValueError)` 会**在修复前就通过**（假绿）。
  > 加上 `match="pair"` 才能钉住"是成对校验在拒绝"，而不是"字段不认识"。

- [x] **Step 2: Run and verify it fails**

  ```powershell
  & $py -m pytest tests/integration/test_composition_wiring.py -q -k "paired_index_versions or half_configured"
  ```

  预期：
  - `test_paired_index_versions_artifact_wires_through` → `ValueError: ... incomplete or has unknown fields`
    （因为 `CalibrationArtifact` 还不接受新字段；Task 1/2 完成后则应变为断言通过）。
  - `test_half_configured_index_versions_artifact_fails_fast` → **失败**
    （抛出的消息不含 "pair"）。

- [x] **Step 3: Implement**
  `src/medical_rag/api/composition.py` **预期无需改动**：`load_calibration_artifact` 已用
  `CalibrationArtifact(**payload)` 透传 JSON 全部字段，`_build` 也只做透传。
  - 若测试失败，先确认 `CalibrationArtifact(**payload)` 能接受新字段（Task 1 是否落盘）。
  - **仅在确有必要时**才改 composition，且**不得新增配置项**、**不得改变失败语义**
    （已配置但读取/解析失败仍然直接抛错，不回退）。

- [x] **Step 4: Run the tests**

  ```powershell
  & $py -m pytest tests/integration/test_composition_wiring.py -q
  ```

  预期：全绿（含既有 13 个测试）。

---

### Task 6: 验收脚本改走 artifact 装配路径（新增 ⑤⑥ 断言）

> 现状：`scripts/retrieval_cosine_gate_eval.py` 直接传 `cosine_threshold=COSINE_THRESHOLD`，
> **恰好绕过** artifact 一致性检查——这就是本缺陷此前在真实链路上"看不出来"的原因。
> 本任务把它改成经 artifact 装配，并新增两条断言。

- [x] **Step 1: Modify `build_service` to take a mode**

  ```python
  #: 双索引部署的索引版本常量（来源见 spec §2.2）。
  PUBLIC_INDEX_VERSION = "public-v1"
  PRIVATE_INDEX_VERSION = "private-v1"

  ARTIFACT_EVAL_DATASET_VERSION = "cosine-gate-2026-09-26"


  def build_artifact(*, per_index_versions: bool) -> CalibrationArtifact:
      """构造与真实索引匹配的 artifact。

      ``per_index_versions=False`` 复现修复前的旧 artifact 形态（单一 index_version），
      用于在真实链路上展示"双索引必然 fail-closed"。
      """
      values: dict[str, object] = {
          "index_version": PUBLIC_INDEX_VERSION if not per_index_versions else "artifact-2026-09-28",
          "embedding_model": EMBEDDING_MODEL_NAME,
          "rrf_k": 60,
          # RRF 阈值；cosine 门禁生效时不参与判定，仅作为 artifact 元数据保留。
          "threshold": 0.42,
          "eval_dataset_version": ARTIFACT_EVAL_DATASET_VERSION,
          "cosine_threshold": COSINE_THRESHOLD,
          "score_space": "l2_inv",
      }
      if per_index_versions:
          values["public_index_version"] = PUBLIC_INDEX_VERSION
          values["private_index_version"] = PRIVATE_INDEX_VERSION
      return CalibrationArtifact(**values)


  def build_service(store: OpenSearchStore, *, mode: str) -> RetrievalService:
      """生产 ``RetrievalService``：唯一差异是门禁装配方式。

      mode:
        ``baseline``       —— 无 artifact、无门禁（生产现状对照）
        ``artifact``       —— 经 artifact 装配的余弦门禁（**双索引版本按索引分派**）
        ``legacy-artifact``—— 经旧 artifact 装配（复现修复前的 fail-closed）
      """
      if mode == "baseline":
          return RetrievalService(
              store,
              embedding_model=make_embedding_model(use_cache=True, prefix=None),
              embedding_model_name=EMBEDDING_MODEL_NAME,
              embedding_dimensions=512,
              ranker=HybridRanker(rrf_k=60),
              candidate_limit=50,
          )
      return RetrievalService(
          store,
          embedding_model=make_embedding_model(use_cache=True, prefix=None),
          embedding_model_name=EMBEDDING_MODEL_NAME,
          embedding_dimensions=512,
          ranker=HybridRanker(rrf_k=60),
          candidate_limit=50,
          calibration_artifact=build_artifact(
              per_index_versions=(mode == "artifact")
          ),
      )
  ```

  同时新增导入：`from medical_rag.observability.metrics import CalibrationArtifact`
  （与既有 `EvaluationCase, evaluate_retrieval` 同一行合并）。

- [x] **Step 2: Aggregate mismatch evidence in `evaluate`**
  在 `CaseRow` 上新增 `scope: str` 字段（构造时传 `scope=case.scope`），
  在 `Summary` 上新增：

  ```python
  artifact_mismatch_cases: int = 0      # reason == calibration_artifact_mismatch 的用例数
  artifact_mismatch_records: int = 0    # 各用例 diagnostics["calibration_mismatch_count"] 之和
  mixed_scope_zero_return: int = 0      # 混合作用域可答用例中返回 0 条的个数
  ```

  在 `evaluate` 的用例循环里累加：

  ```python
  mismatch_cases = 0
  mismatch_records = 0
  mixed_scope_zero = 0
  # ... 循环内，拿到 result / returned 之后：
  if result.reason == "calibration_artifact_mismatch":
      mismatch_cases += 1
      count = result.diagnostics.get("calibration_mismatch_count")
      mismatch_records += count if isinstance(count, int) else 0
  # 「混合作用域」= include_public 与 include_private 同时开启（生产 LAB_INDICATOR/ORDINARY 路由）
  if case.category == "quality" and case.scope == "private" and not returned:
      mixed_scope_zero += 1
  ```

  并把三个计数写进 `Summary(...)`。

- [x] **Step 3: Add the new verdicts and the legacy demo run in `main`**

  ```python
  parser.add_argument(
      "--legacy-demo", action="store_true",
      help="追加一次旧 artifact（单一 index_version）运行，复现双索引 fail-closed",
  )
  ```

  在 `if not args.baseline:` 分支里把 `build_service(store, gated=True)` 换成
  `build_service(store, mode="artifact")`，并在 `--legacy-demo` 时追加
  `build_service(store, mode="legacy-artifact")` 的一次运行（**仅展示，不进 verdict**）。

  新增两条断言（在既有四项之后）：

  ```python
  # 断言 5（新增）：混合作用域可答用例必须全部有返回
  verdicts.append(
      (
          f"断言5 混合作用域零返回用例数 == 0（修复前 = {gated.mixed_scope_zero_return}）",
          gated.mixed_scope_zero_return == 0,
      )
  )
  # 断言 6（新增）：标定不一致出现次数必须为 0
  verdicts.append(
      (
          f"断言6 calibration_artifact_mismatch 用例数 == 0（修复前 = {gated.artifact_mismatch_cases}）",
          gated.artifact_mismatch_cases == 0,
      )
  )
  ```

  并把 `gated.artifact_mismatch_cases`、`gated.artifact_mismatch_records`、
  `gated.mixed_scope_zero_return`、`scope`（per row）写进 JSON payload。

- [x] **Step 4: Run baseline first, then the artifact path**

  ```powershell
  & $py scripts/retrieval_cosine_gate_eval.py --baseline
  & $py scripts/retrieval_cosine_gate_eval.py --legacy-demo
  ```

  预期（第二条命令，三条运行并列）：
  - `baseline-no-gate`：正常。
  - `cosine-gate-artifact`：**断言 1–6 全部 PASS**；`artifact_mismatch_cases == 0`；
    `mixed_scope_zero_return == 0`；可答用例 Hit/Recall/MRR/nDCG 与 baseline **逐值相等**；
    零容忍三项 == 0；P95 劣化 ≤ 1.2×。
  - `legacy-artifact (修复前口径)`：出现 `calibration_artifact_mismatch` 的用例数 **> 0**，
    作为"该缺陷是既有缺陷、且本次未放宽旧 artifact"的现场证据（**不参与 verdict**）。

- [x] **Step 5: Verify the six assertions**
  - [x] ① 拒答 **2/2**，`reason == "insufficient_evidence"`（其中 `priv-out-of-scope`
        本身即混合作用域，是本次修复的直接覆盖点）
  - [x] ② Hit@K / Recall@K / MRR / nDCG@K **与 baseline 逐值相等**
  - [x] ③ 零容忍三项 == 0
  - [x] ④ 端到端 P95 劣化 ≤ 1.2×（脚本内已预热 2 轮 + 计时 25 轮）
  - [x] ⑤ 混合作用域可答用例返回条数 **全部 > 0**（修复前为 0；实测 legacy 口径 = 5）
  - [x] ⑥ `calibration_artifact_mismatch` 用例数 == 0（修复前**实测** = 6 个用例 / 42 条记录；
        计划初稿写的 16 是未验证估值，已在本文件 Global Constraints 处更正）

---

### Task 7: 全量回归 + 静态检查 + 清理 + 文档更正

- [x] **Step 1: Full suite**

  ```powershell
  & $py -m pytest tests/ -q
  ```

  记录实际通过/跳过/失败数，并与基线对比（基线 **730 passed / 3 skipped**；
  本次新增约 25 例，预期 **≈755 passed / 3 skipped，failed 必须为 0**）。
  > **实测（2026-09-28）**：**754 passed / 3 skipped / 0 failed**（38.51s）。
  > 新增 24 例（4+5+10+2+3），与预估 25 例差 1——因为 Task 6 未新增 pytest 用例（改为脚本内断言）。
  > 无成批 teardown error，未触发 safe-delete 护栏。
  若出现**成批 teardown error**，先单独重跑受影响文件——本沙箱有 safe-delete 护栏
  （累计删除约 50 个临时目录会触发 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`），
  表现为 teardown error 而**非断言失败**。

- [x] **Step 2: Static checks**

  ```powershell
  & $py -m ruff check src/medical_rag tests scripts
  & $py -m ruff format --check src/medical_rag tests scripts
  ```

  预期：本次改动的文件无新增告警（`scripts/_i18n_*.py` 的既有 5 处告警属于历史遗留，
  不在本次范围，但需在交付说明中重述）。
  > **实测（2026-09-28）**：
  > - `ruff check`（6 个本次改动文件）→ **All checks passed!**。修掉了本次引入的 2 处
  >   （`F401` 未用的 `EMBEDDING_MODEL_PATH`、`S101` 裸 `assert` → 改为 `raise RuntimeError`，
  >   后者在 `python -O` 下不会被剥离，严格更优）。其余 8 处告警分散在
  >   `retrieval_eval_report.py` / `retrieval_gate_prototype.py` / `retrieval_raw_score_probe.py` /
  >   `retrieval_rejection_probe.py` / `retrieval_tuning_lab.py`，**均为历史遗留，不在本次范围**。
  > - `ruff format --check`：全仓 121 个文件会被重排——**format 不是本项目 CI 基线门槛**
  >   （改前即如此），本次**未**全仓格式化以免制造无关 diff。本次改动文件的行级 format 偏差
  >   经逐文件核对，`metrics.py` / `service.py` / `test_retrieval_metrics.py` /
  >   `test_retrieval_service.py` / `test_composition_wiring.py` 的偏差**均为既有**，
  >   仅 `retrieval_cosine_gate_eval.py` 有 4 处为本次代码。**决策：保持不动**，理由是
  >   该文件其余部分同样未格式化，单独格式化它反而使风格分裂；留待独立「全仓 format」任务。

- [x] **Step 3: 安全不变量逐条断言（对照 spec §8）**

  | # | 不变量 | 覆盖位置 |
  |---|---|---|
  | I1 | 校验只做减法 | Task 4 `test_paired_artifact_passing_calibration_leaves_candidate_set_unchanged` |
  | I2 | 无可解析期望版本 → fail-closed | Task 3 `test_paired_artifact_unknown_visibility_fails_closed_with_identity` |
  | I3 | 半配置构造期即拒 | Task 1 `test_artifact_rejects_half_configured_index_versions` + Task 5 组装期 |
  | I4 | 旧 artifact 行为逐字节不变 | 既有 `test_calibrated_artifact_version_mismatch_fails_closed`（未改）+ Task 4 `test_legacy_artifact_with_dual_index_versions_still_fails_closed` |
  | I5 | `embedding_model` 仍全局校验 | Task 3 `test_paired_artifact_embedding_model_mismatch_still_fails_closed` |
  | I6 | 越权/删除残留/复核泄漏恒为 0 | Task 6 断言 ③ |
  | I7 | 越权记录不进 mismatch 诊断 | Task 4 `test_unauthorized_record_absent_from_mismatch_diagnostics` |
  | I8 | 诊断不泄漏正文 | Task 3 `test_mismatch_diagnostics_carry_identity_without_text` |
  | I9 | 门禁在混合作用域下确实生效 | Task 6 断言 ①⑤⑥ + ② |

  若某条无对应测试，**必须补测试**，不得只勾选框。

- [x] **Step 4: 降级路径验证（spec §7.1）**

  确认以下三行行为与 spec 一致（前两行已有测试，第三行为现场观察）：
  - 记录版本与其所在索引声明不符 → `items=[]` + `calibration_artifact_mismatch`；
  - 半配置 artifact → 启动即失败；
  - 旧 artifact + 双索引 → 仍 fail-closed（Task 6 `--legacy-demo` 现场证据）。

- [x] **Step 5: 清理临时文件**
  用 Python `os.remove` 删除 `.workbuddy/` 下本次探针产物（本机 trash 机制失效且失败静默），
  删除后用 `os.listdir` **复核为空**（只应剩 `memory/`）。
  仓库根目录不得残留任何探针脚本。

- [x] **Step 6: 更新文档**
  - 在 `eval_out/门禁启用端到端验证报告.md` 顶部追加"**2026-09-28 更新：已定位为按索引
    分派的校验缺陷，修复并验证（本报告结论已被取代）**"，指向本 spec/plan 与
    `eval_out/cosine_gate_eval.json` 的新证据。
  - 在 `docs/检索模块评估与调参报告.md` 中记录：标定一致性校验按索引分派，
    两索引版本为 `public-v1` / `private-v1`。
  - 交付说明中显式重述 spec §9 的遗留项 **D1–D5**（本次均未修，D4 仍是假绿）。

---

## 后续阶段（**不在本计划范围**，各自独立验收）

| 阶段 | 内容 | 前置 |
|---|---|---|
| 阶段 2 | 统一/规范两索引 `index_version` 命名（D3）；处理死配置 `Settings.embedding_index_version`（D2）；索引名归属判定提为协议（D5） | 本计划通过 |
| 阶段 3 | 写正式 artifact JSON + 配置 `CALIBRATION_ARTIFACT_PATH` + 启用后端到端验收 + 监控告警接入（含 D1 白名单） | **需用户单独批准**（spec §3.2 明确本设计不改部署状态） |

> 阶段 3 的"启用后验收"口径与 Task 6 完全一致——Task 6 已经在真实 OpenSearch 双索引上
> 走 artifact 装配路径跑通全部六条断言，因此阶段 3 只是把同一 artifact 落到 `.env`。

---

## Plan Self-Review

**任务粒度**：7 个任务各自可独立验收。依赖顺序 Task 1 → 2 → 3 →（4、5、6、7）；
Task 4/5 只依赖 Task 1–3，Task 6 依赖 1–3，Task 7 依赖全部。

**TDD 完整性**：Task 1/2/3/5/6 均写明"确认失败"步骤与**预期看到的具体失败信息**
（`TypeError` / `AttributeError` / `KeyError` / `AssertionError`），避免测试空转。
Task 4 是纯不变量护栏任务，已显式说明为何没有"确认失败"步骤。

**风险点**：
1. **Task 3 的一致性检查块是既有高风险区**（紧邻 4 个 fail-closed 校验与余弦门禁）。
   只允许**替换判据**，不得移动代码块位置、不得新增空结果分支。Step 4 显式要求跑
   `test_scheme_b_rollout.py` 守住既有 6 个测试。
2. **`str(record.visibility)` 而非 `.value`**：防御路径上 `visibility` 可能是裸字符串，
   用 `.value` 会在构造 mismatch 诊断时抛 `AttributeError`，把 fail-closed 变成 500。
   Task 3 的 `test_paired_artifact_unknown_visibility_fails_closed_with_identity`
   专门守这一点。
3. **Task 4 的 I4 护栏是最重要的回归线**：任何"顺手让旧 artifact 也变可用"的实现
   都会让它立刻失败。spec §7.2 已给出拒绝自动降级的三条理由。
4. **Task 6 的 `--legacy-demo` 不是可选装饰**：它把"旧 artifact 在双索引下必然
   fail-closed"从文档断言变成现场证据，同时防止修复把 I4 悄悄破坏。
5. **`Edit` 未落盘**：Global Constraints 已要求每次编辑后 `Grep`/`Read` 复核。
   本项目此前在 `retrieval_gate_prototype.py` 上真实踩到过（dataclass 字段未写入，
   导致 `TypeError: unexpected keyword argument`）。

**非目标确认**：本计划不含 `space_type` 修改、不含 `top_k` 调整、不含语料清理、
不含 `embedding_model` 校验方式变更、不含 `_safe_diagnostics` 白名单修复（D1）、
不含 `CALIBRATION_ARTIFACT_PATH` 部署变更（阶段 3）。这些在 spec §3.2 与 §9 中显式排除。

---

## 执行完成记录（2026-09-28）

**状态：Task 1–7 全部完成，阶段 1（本次范围）验收通过。**

| 项 | 结果 |
|---|---|
| 全量测试 | **754 passed / 3 skipped / 0 failed**（基线 730；新增 24，`test_scheme_b_rollout.py` 逐字节未改） |
| `ruff check`（本次 6 文件） | All checks passed |
| 真实 OpenSearch 双索引 E2E | 断言 ①–⑥ **全 PASS**（`eval_out/cosine_gate_eval.json`） |
| 拒答 | 2/2，`reason=insufficient_evidence` |
| 排序指标 | 与 baseline **逐值相等**（hit/recall/mrr/ndcg 差 0.0） |
| P95 劣化 | **1.01–1.02×**（≤1.2×） |
| 混合作用域零返回 | 修复前 **5** → 修复后 **0** |
| mismatch 用例 | 修复前 **6**（42 条记录）→ 修复后 **0** |
| 临时文件 | `.workbuddy/` 已清空至仅剩 `memory/`（29 个文件，`os.listdir` 复核） |

### 规则 4 偏差上报（实施期对 spec/plan 的修正）

1. **`calibration_mismatch_count` 去重（实现缺陷修复，非计划内容）**
   Task 4 的 I7 护栏测试断言 `count == 1`，实测得到 `2` → 发现**真实实现缺陷**：
   同一条记录若同时被词法 + 向量两路召回，会被计数两次。
   **修正**：按 `record.id` 去重（`unique.setdefault(record.id, record)`）后再计数，
   使 `count` 语义为"**记录数**"而非"记录×路径数"。
   **已同步更新**：spec §5.3 伪码、§5.4 诊断键表；
   新增回归测试 `test_mismatch_count_deduplicates_records_hit_by_both_routes`。
   → 这是本次唯一超出计划的生产代码改动，属**修正计划未预见的缺陷**，不是范围蔓延。

2. **计划中的量化预期 `16` 是未验证估值** → 实测 legacy 口径为 **6 用例 / 42 记录 / 5 混合零返回**。
   已在 Global Constraints 与 Task 6 Step 5 显式更正；所有产物（脚本断言文本、JSON、报告）
   一律使用实测值，`16` 不再出现。

3. **`assert` → `raise RuntimeError`**（Task 6 自检）：裸 `assert` 会被 `python -O` 剥离，
   使"门禁口径未生效"这类关键自检在优化模式下静默失效。改为显式抛错。

### 遗留（本次未做，需独立批准）

- **阶段 2**：D3 版本命名规范、D2 死配置清理、D5 归属判定提为协议。
- **阶段 3**：写正式 artifact JSON + 配置 `CALIBRATION_ARTIFACT_PATH` + 启用后 E2E + 监控告警（含 D1 白名单）。
  **当前 `.env` 仍未配置该路径——这是有意的，不要提前打开。**
- **D4 仍是假绿**：`deletion_residuals` / `review_required_leakage` 零容忍项因语料无对应负样本而恒为 0。
- 全仓 `ruff format`（121 文件）未执行，非本次范围。
