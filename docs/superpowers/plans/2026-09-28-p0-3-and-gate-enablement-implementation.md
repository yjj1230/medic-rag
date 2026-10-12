# P0-3（扫描件复核出口）+ 阶段 3（余弦门禁启用）合并实施计划

> 关联 spec（两份，均**待用户批准**）：
> - `docs/superpowers/specs/2026-09-28-p0-3-ocr-review-exit-design.md`
> - `docs/superpowers/specs/2026-09-28-cosine-gate-enablement-design.md`
>
> 前置已验收：`docs/superpowers/plans/2026-09-28-per-index-calibration-matching-implementation.md`
> （Task 1–7 全完成；754 passed / 3 skipped；真实双索引 E2E 断言 ①–⑥ 全 PASS）
>
> 日期：2026-09-28
> 范围：两份 spec 的**阶段 1 / 阶段 1a+1b / 阶段 3A** 全部可立即执行的部分。
> **`.env` 的修改（阶段 3B）不在本计划的可执行范围内**，需用户单独批准。

## 合并理由与边界

用户要求"P0-3 与阶段 3 启用两个一起解决"。两者**没有代码依赖**，但共享同一个
交付目标——"检索层可信"：P0-3 让**该进来的内容进来**（扫描件不再被无出口地拦下），
阶段 3A 让**不该进来的内容被拒**（无关查询不再硬性返回 top_k）。

因此本计划把它们编入同一次实施，但**保持物理可分离**：

| 分组 | 内容 | 前置 | 可否独立验收 |
|---|---|---|---|
| **阶段 1a** | P0-3 门禁替代信号 + 复核出口（Task 1–7） | 无 | 是 |
| **阶段 1b** | P0-3 公共路径 fail-closed（Task 8–9） | 无（与 1a 无依赖） | 是 |
| **阶段 3A** | 门禁可观测性 + 正式 artifact + 启动自检 + 干跑（Task A1–A5） | P0-1 已验收 | 是 |
| **阶段 3B** | 改 `.env` 启用（Task B1–B3） | 阶段 3A 验收 | **需用户单独批准** |

> 阶段 1b 与 1a 无依赖，**若评审认为合并提交过大，可只做 1a + 3A，1b 另开一轮**。

## Global Constraints

- **执行环境**：`C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`。
  bash 残缺（`dirname`/`ls`/`cat`/`head` 缺失，退出码 127），命令编排一律用 **PowerShell 工具**；
  PowerShell stdout 常为空 → 输出 `| Out-File <tmp> -Encoding utf8` 再 `Read`。
  打印中文前先 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`。
- **不要用 managed python**（`C:\Users\lyx\.workbuddy\binaries\python\...`）：它**没有项目依赖**。
- **不可破坏的既有契约（逐条有测试守护）**：
  - **P1（最重要）**：`confidence_unavailable_ratio == 0.0` 时门禁行为**逐字节不变**。
    `tests/unit/test_quality_gate.py` 的 15 个用例**一行都不许改**（含
    `test_missing_confidence_is_undecidable_review_not_silent_pass`——它会需要**改语义**，
    见 Task 3 Step 3 的显式说明）。
  - **`QualityDecision.__post_init__` 的 `PASS ↔ retrieval_eligible` 不变量不动**。
  - **`cosine_threshold` / `score_space` / `rrf_k` / `top_k` / `space_type` 全部不动**。
  - `tests/integration/test_scheme_b_rollout.py` 9 个用例、`tests/unit/test_retrieval_metrics.py`
    27 个用例**不改动**仍通过。
- **依赖声明**：本次不新增任何第三方依赖（`pyproject.toml` 不动）。
- **测试自洽**：测试不得读开发者 `.env`；构造 `Settings` 用 `Settings(_env_file=None, ...)`。
  ⚠️ **该约定只适用于测试**。用 `Settings(_env_file=None, ...)` 去判断"运行配置是否已设置"
  会**得出假结论**——本项目已真实踩到过：一次探针据此报"`OCR_SERVICE_URL` 未配置"，
  而 `.env` 第 41 行实际配着（`Settings()` → `len=27`，`Settings(_env_file=None)` → `len=0`）。
  读真实运行配置请用 `Settings.from_env()` / 默认 `Settings()`。
- **编辑后必须复核**：本项目 `Edit` 工具出现过"回执成功但未落盘"。每次编辑后
  `Grep`/`Read` 复核目标内容确实存在，再进入下一步。
- **探针清理**：临时脚本只放 `.workbuddy/`，用 Python `os.remove` 删除（本机 trash 失效且
  静默），删后用 `os.listdir` 复核。**已验证事实**：单轮删除约 50 个目录会触发
  `SAFE_DELETE_BULK_CONFIRM_REQUIRED`（表现为 pytest teardown error，**不是断言失败**）。
- **验收基线**：全量测试 **754 passed / 3 skipped / 0 failed**（2026-09-28 实测）。
- **代理坑**：本机 `HTTP_PROXY=127.0.0.1:64036`。探针访问 `localhost:9200` 要
  `trust_env=False`，否则报 `no handler found`。项目代码（opensearchpy）不受影响。
- **已核实的事实（不要重复验证）**：
  - `ChatOrchestrator` 已暴露 `self.retriever`（`chat/orchestrator.py:111`）。
  - `RetrievalService` 已暴露 `self.calibration_artifact` / `self.gate_policy`
    （`retrieval/service.py:112,138-165`）。
  - `PublicIndexRecord` **已有** `quality_flags` 字段（`retrieval/contracts.py:87`）。
  - `U+25A0` 的 `unicodedata.category` 是 `So`，**不在** `gate.py:41` 的
    `_SUSPICIOUS_CATEGORIES = {"Cc","Cf"}` 中 → 既有 `abnormal_character_ratio` 对它恒为 0。
  - `QualityFailureCode` 是 **`StrEnum`**，用 `QualityFailureCode.X` 作 dict 键等价于用
    `X.value`（`quality/codes.py:56-75` 既有用法即如此）。
  - alembic 最新 revision 是 `0008`（`alembic/versions/0008_add_chat_message_indexes.py`）。
  - `config/` 目录**尚不存在**；`.gitignore` **不排除** `config/`（G7 契约可满足）。

---

## 阶段 1a（P0-3：门禁替代信号 + 复核出口；Task 1–7）

### Task 1: 医学词表 + 三个替代质量纯函数 + `QualityPolicy` 5 个新字段

> spec §4.3。**本任务不改变任何判决**——只新增可测量的指标，`evaluate` 暂不读取它们。

- [ ] **Step 1: Write the failing tests**
  新建 `tests/unit/test_quality_substitute_signals.py`：

  ```python
  """OCR 替代质量信号：纯函数 + 标定阈值（spec 2026-09-28-p0-3 §4.3/§4.5）。

  标定数据来自只读探针的**实测**结果（4 样本），不是估值。见 §4.5 表。
  """
  from __future__ import annotations

  import pytest

  from medical_rag.parsing.contracts import CleanedBlock, CleanedDocument, EvidenceUnitDraft
  from medical_rag.quality.gate import (
      QualityPolicy,
      _medical_term_hit_rate,
      _meaningful_character_ratio,
      _ocr_line_length_plausibility,
  )
  from medical_rag.quality.lexicon import MEDICAL_TERMS


  def _doc(text: str) -> CleanedDocument:
      return CleanedDocument(
          raw_blocks=(CleanedBlock(raw_text=text, block_type="paragraph"),),
          cleaned_blocks=(CleanedBlock(raw_text=text, block_type="paragraph"),),
          source_offsets=(),
          cleaning_flags=(),
          cleaner_version="test",
      )


  # --- §4.5 实测样本（逐值断言，防止指标被无意改动）---

  def test_meaningful_ratio_of_real_chinese_medical_text_is_one() -> None:
      text = "血红蛋白 105 g/L，参考范围 115-150 g/L。建议复查血常规。"
      assert _meaningful_character_ratio(_doc(text), []) == pytest.approx(1.0)


  def test_meaningful_ratio_of_u25a0_block_garbage_is_zero() -> None:
      """无中文字形 PDF 解析出的方块串：S1 实测 0.0000（spec §4.5 第 2 行）。"""
      text = "\n".join("\u25a0" * 12 for _ in range(10))
      assert _meaningful_character_ratio(_doc(text), []) == pytest.approx(0.0)


  def test_meaningful_ratio_penalises_replacement_characters() -> None:
      """U+FFFD 污染：实测 0.7692（12 个替换符 / 52 个非空白字符）。"""
      text = "\n".join("血红蛋白 \ufffd\ufffd\ufffd 105 g/L" for _ in range(4))
      assert _meaningful_character_ratio(_doc(text), []) == pytest.approx(12 / 52, abs=1e-4)


  def test_meaningful_ratio_ignores_whitespace_differences() -> None:
      plain = _meaningful_character_ratio(_doc("血红蛋白105"), [])
      spaced = _meaningful_character_ratio(_doc("血 红 蛋 白\n\t105"), [])
      assert plain == spaced == pytest.approx(1.0)


  def test_empty_text_is_zero_not_one() -> None:
      """无内容 => 0.0（由 S1 拦下）；"无内容"不得算合格。"""
      assert _meaningful_character_ratio(_doc("   \n\t "), []) == pytest.approx(0.0)


  def test_so_category_is_not_meaningful_but_is_not_caught_by_existing_metric() -> None:
      """独立发现：U+25A0 属 So，既有 _SUSPICIOUS_CATEGORIES 不含它。"""
      from medical_rag.quality.gate import _abnormal_character_ratio

      garbage = _doc("\u25a0" * 50)
      # 旧指标对它完全无感（0.0 异常率）——这正是需要新指标的原因
      assert _abnormal_character_ratio(garbage) == pytest.approx(0.0)
      # 新指标看得见它
      assert _meaningful_character_ratio(garbage, []) == pytest.approx(0.0)


  # --- S2 医学词表命中率 ---

  def test_medical_term_hit_rate_counts_lines_not_characters() -> None:
      text = "血红蛋白 105\n天气不错\n参考范围 115-150"
      assert _medical_term_hit_rate(_doc(text), []) == pytest.approx(2 / 3)


  def test_medical_term_hit_rate_is_one_when_no_content_lines() -> None:
      """fail-open 到"不判定"（与既有 page_coverage 口径一致），由 S1 兜底。"""
      assert _medical_term_hit_rate(_doc("   \n  "), []) == pytest.approx(1.0)


  def test_medical_lexicon_is_non_empty_unique_and_has_a_declared_source() -> None:
      """词表契约：非空、去重、且在模块 docstring 中声明来源。"""
      import medical_rag.quality.lexicon as lexicon

      assert len(MEDICAL_TERMS) >= 20
      assert len(set(MEDICAL_TERMS)) == len(MEDICAL_TERMS)
      assert all(term.strip() for term in MEDICAL_TERMS)
      assert lexicon.__doc__ and "来源" in lexicon.__doc__


  # --- S3 行长合理性 ---

  def test_line_length_plausibility_rejects_extreme_lines() -> None:
      text = "a\n" + ("b" * 900) + "\n" + "短"
      # 3 行：1 行过短(1)、1 行过长(900)、1 行过短(1) => 0 行落在 [2,500]
      assert _ocr_line_length_plausibility(_doc(text), [], QualityPolicy()) == pytest.approx(0.0)


  def test_line_length_plausibility_accepts_normal_report_lines() -> None:
      text = "\n".join(
          ["血红蛋白 105 g/L 参考范围 115-150", "白细胞 6.2 10^9/L 参考范围 4-10"] * 3
      )
      assert _ocr_line_length_plausibility(_doc(text), [], QualityPolicy()) == pytest.approx(1.0)


  def test_line_length_plausibility_is_one_when_no_content_lines() -> None:
      assert _ocr_line_length_plausibility(_doc(""), [], QualityPolicy()) == pytest.approx(1.0)


  # --- QualityPolicy 新字段默认值（来自实测标定，spec §4.5）---

  def test_policy_carries_substitute_thresholds_with_calibrated_defaults() -> None:
      policy = QualityPolicy()
      assert policy.min_meaningful_character_ratio == pytest.approx(0.60)
      assert policy.min_medical_term_hit_rate == pytest.approx(0.02)
      assert policy.min_ocr_line_length == 2
      assert policy.max_ocr_line_length == 500
      assert policy.min_ocr_line_length_pass_ratio == pytest.approx(0.80)


  def test_policy_documented_defaults_are_unchanged_by_this_task() -> None:
      """回归线：本任务只加字段，既有阈值一个都不许变。"""
      policy = QualityPolicy()
      assert policy.min_text_coverage == pytest.approx(0.85)
      assert policy.min_anchor_coverage == pytest.approx(1.0)
      assert policy.max_abnormal_character_ratio == pytest.approx(0.02)
      assert policy.min_ocr_confidence == pytest.approx(0.80)
      assert policy.min_page_coverage == pytest.approx(0.5)
  ```

- [ ] **Step 2: Run the tests and verify they fail**

  ```powershell
  $py = "C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
  & $py -m pytest tests/unit/test_quality_substitute_signals.py -q
  ```

  预期：`ModuleNotFoundError: No module named 'medical_rag.quality.lexicon'`（收集期即失败）。
  **必须亲眼确认是"缺少新模块"导致，而不是测试自身语法错误。**

- [ ] **Step 3: Implement**

  1. 新建 `src/medical_rag/quality/lexicon.py`：

     ```python
     """`MedicalTermLexicon`：OCR 替代判据 S2 的单一词典来源。

     来源：本仓库 `knowledge/samples/hypertension-sample.md` 与
     `tests/fixtures/` 下检验报告样本中实际出现的**通用检验项与报告用语**，
     人工摘录、去重。刻意保持小规模并保守：S2 只用于拦"整篇零命中"的极端情形
     （spec §4.5 已声明其辅助地位），不承担主要判别力。
     词表**不含**任何患者信息，可安全纳入版本管理。

     扩表流程：新增条目必须同时补一条 `tests/unit/test_quality_substitute_signals.py`
     的词表契约测试覆盖（非空/去重），并按 spec §11 阶段 2 在真实语料上复核 S2 阈值。
     """

     #: 通用检验项名称与报告用语。仅用于"行是否像医学报告"的存在性判断。
     MEDICAL_TERMS: frozenset[str] = frozenset({
         # 血常规
         "血红蛋白", "白细胞", "红细胞", "血小板", "红细胞压积", "中性粒细胞",
         "淋巴细胞", "单核细胞", "嗜酸", "嗜碱", "平均红细胞",
         # 生化
         "血糖", "总胆固醇", "甘油三酯", "高密度脂蛋白", "低密度脂蛋白",
         "肌酐", "尿素", "尿酸", "谷丙转氨酶", "谷草转氨酶", "总胆红素",
         "白蛋白", "球蛋白", "碱性磷酸酶", "乳酸脱氢酶",
         # 尿常规 / 其他
         "尿蛋白", "尿糖", "尿潜血", "酮体", "亚硝酸盐", "比重", "酸碱度",
         # 血压与体征
         "血压", "收缩压", "舒张压", "心率", "体温", "体重", "身高",
         # 报告用语
         "检验", "检查", "检测", "参考范围", "参考值", "正常值", "结果",
         "增高", "降低", "偏高", "偏低", "阴性", "阳性", "未见异常",
         "病史", "诊断", "建议", "复查", "随访", "考虑", "提示",
     })

     __all__ = ["MEDICAL_TERMS"]
     ```

  2. 在 `src/medical_rag/quality/gate.py` 的 `QualityPolicy` **末尾追加** 5 个字段
     （追加在既有 `min_page_coverage` 之后，不改既有字段的位置与默认值）：

     ```python
         # --- OCR 无置信度时的替代质量判据（仅当 confidence 不可得时启用；spec §4.3）---
         #: 有意义字符占非空白字符的最低比例。标定：好文本 0.985–1.0，U+25A0 垃圾 0.0
         #: （实测，spec §4.5）。0.60 落在两者之间且两端都有裕度。
         min_meaningful_character_ratio: float = 0.60
         #: 命中医学词典的**行**占比下限。刻意极低：只用于拦"整篇零命中"的极端情形，
         #: 不作为主要判别力（词表小，且口语化非医学文本本就可能零命中）。
         min_medical_term_hit_rate: float = 0.02
         #: OCR 内容行的合理长度区间；垃圾 OCR 常产出极短（单字符/方块）或极长（未断行）的行。
         min_ocr_line_length: int = 2
         max_ocr_line_length: int = 500
         #: 行长落在上述区间的行占比下限。
         min_ocr_line_length_pass_ratio: float = 0.80
     ```

  3. 在 `gate.py` 模块级常量区新增原因码与有意义字符集合（放在
     `_SUSPICIOUS_CATEGORIES` 附近）：

     ```python
     REASON_OCR_SUBSTITUTE_QUALITY_INSUFFICIENT = "ocr_substitute_quality_insufficient"

     #: 视为"有意义"的 Unicode 类别：字母、数字（各类）。
     _MEANINGFUL_CATEGORIES = frozenset({"Lo", "Lu", "Ll", "Lt", "Lm", "Nd", "Nl", "No"})

     #: 视为"有意义"的常用标点（中英文）。刻意用显式集合而非类别判断：
     #: 标点类别（Po/Ps/Pe/Pd/Pc）太宽——会把装饰性符号也算作内容。
     _MEANINGFUL_PUNCTUATION = frozenset(
         "，。、；：？！（）【】《》〈〉「」『』—…·％﹪‰“”‘’"
         "()[]{}<>:;,./\\|-_+=*&^%$#@!?~`'\""
     )
     ```

  4. 在 `gate.py` 的私有纯函数区新增三个函数（`_hard_split_ratio` 之后）：

     ```python
     def _content_lines(document: CleanedDocument, evidence: list[EvidenceUnitDraft]) -> list[str]:
         """内容行视图：优先证据文本，退回归块视图（与 `_text_coverage` 同口径）。

         只保留去空白后非空的行；空白行的多寡不构成质量信号。
         """
         if evidence:
             texts = [item.text or item.clean_text or "" for item in evidence]
         else:
             texts = [block.raw_text for block in document.cleaned_blocks]
         return [line.strip() for text in texts for line in text.splitlines() if line.strip()]


     def _meaningful_character_ratio(
         document: CleanedDocument, evidence: list[EvidenceUnitDraft]
     ) -> float:
         """有意义字符占非空白字符的比例（替代信号 S1）。

         有意义 = 空白以外且（Unicode 类别 ∈ `_MEANINGFUL_CATEGORIES`
         或 ∈ `_MEANINGFUL_PUNCTUATION`）。

         刻意覆盖 `U+25A0`（`So`）与 `U+FFFD`（`So`）：两者都**不是**有意义字符，
         因此方块垃圾/替换符污染会被直接扣分。既有 `_abnormal_character_ratio`
         只认 `{Cc,Cf}`，对这两者完全无感（spec §2.3(c) 记录的独立发现）。

         无任何非空白字符时返回 0.0——"没有内容"是**不合格**，不是"不判定"。
         """
         chars = [
             char
             for block in document.cleaned_blocks
             for char in block.raw_text
             if not char.isspace()
         ]
         if not chars:
             return 0.0
         meaningful = sum(
             1
             for char in chars
             if char in _MEANINGFUL_PUNCTUATION
             or unicodedata.category(char) in _MEANINGFUL_CATEGORIES
         )
         return meaningful / len(chars)


     def _medical_term_hit_rate(
         document: CleanedDocument, evidence: list[EvidenceUnitDraft]
     ) -> float:
         """命中内置医学词典的**行**占比（替代信号 S2）。

         无内容行时返回 1.0（fail-open 到"不判定"，与 `_page_coverage` 的既有口径一致）：
         S2 是辅助信号，判定"整篇零命中"的极端情形由它负责，
         "整篇没有内容"则由 S1 负责——两个指标各司其职，不互相越界。
         """
         lines = _content_lines(document, evidence)
         if not lines:
             return 1.0
         hits = sum(1 for line in lines if any(term in line for term in MEDICAL_TERMS))
         return hits / len(lines)


     def _ocr_line_length_plausibility(
         document: CleanedDocument, evidence: list[EvidenceUnitDraft], policy: QualityPolicy
     ) -> float:
         """行长落在 `[policy.min_ocr_line_length, policy.max_ocr_line_length]` 的行占比（S3）。

         垃圾 OCR 的典型形态是极短行（单字符/单个方块）或极长行（未断行的一整页）。
         无内容行时返回 1.0（同样 fail-open 到"不判定"）。
         """
         lines = _content_lines(document, evidence)
         if not lines:
             return 1.0
         plausible = sum(
             1
             for line in lines
             if policy.min_ocr_line_length <= len(line) <= policy.max_ocr_line_length
         )
         return plausible / len(lines)
     ```

  5. `gate.py` 顶部加 `from medical_rag.quality.lexicon import MEDICAL_TERMS`。

- [ ] **Step 4: Run the tests and verify they pass**

  ```powershell
  & $py -m pytest tests/unit/test_quality_substitute_signals.py -q
  & $py -m pytest tests/unit/test_quality_gate.py -q
  ```

  第一条：全绿。第二条：**必须仍然 15 passed**（本任务不碰 `evaluate`）。
  若第二条变红，说明改动越界了。

---

### Task 2: `QualityDecision.advisory_reasons`（仅观测，不参与判定）

> spec §4.3 末段 + §6.1/§6.4。

- [ ] **Step 1: Write the failing tests**
  在 `tests/unit/test_quality_substitute_signals.py` 追加：

  ```python
  # ---------------------------------------------------------------------------
  # QualityDecision.advisory_reasons：仅观测，绝不参与 status/eligible（spec §4.3）
  # ---------------------------------------------------------------------------


  def test_advisory_reasons_defaults_to_empty_tuple() -> None:
      from medical_rag.quality.contracts import QualityDecision

      decision = QualityDecision(status="PASS", metrics={})
      assert decision.advisory_reasons == ()


  def test_advisory_reasons_is_normalised_to_tuple() -> None:
      from medical_rag.quality.contracts import QualityDecision

      decision = QualityDecision(
          status="PASS", metrics={}, advisory_reasons=["confidence_unavailable"]
      )
      assert decision.advisory_reasons == ("confidence_unavailable",)


  def test_advisory_reasons_does_not_participate_in_the_status_invariant() -> None:
      """P3：advisory 非空时，PASS 仍可 eligible=True。"""
      from medical_rag.quality.contracts import QualityDecision

      passed = QualityDecision(
          status="PASS", metrics={}, advisory_reasons=("confidence_unavailable",)
      )
      assert passed.retrieval_eligible is True

      review = QualityDecision(
          status="NEEDS_REVIEW",
          reasons=("low_text_coverage",),
          metrics={},
          retrieval_eligible=False,
          advisory_reasons=("confidence_unavailable",),
      )
      assert review.retrieval_eligible is False
      assert review.reasons == ("low_text_coverage",)


  def test_passing_with_advisory_reasons_still_requires_eligible_true() -> None:
      """不变量未被削弱：PASS + eligible=False 仍必须拒绝构造。"""
      from medical_rag.quality.contracts import QualityDecision

      with pytest.raises(ValueError, match="retrieval_eligible"):
          QualityDecision(
              status="PASS", metrics={}, retrieval_eligible=False,
              advisory_reasons=("confidence_unavailable",),
          )
  ```

- [ ] **Step 2: Run and verify failure**

  ```powershell
  & $py -m pytest tests/unit/test_quality_substitute_signals.py -q -k advisory
  ```

  预期：`TypeError: QualityDecision.__init__() got an unexpected keyword argument 'advisory_reasons'`。

- [ ] **Step 3: Implement**
  在 `src/medical_rag/quality/contracts.py` 的 `QualityDecision` 末尾新增字段：

  ```python
      #: 仅供观测/追溯的附加原因，**绝不参与** status 与 retrieval_eligible 判定。
      #: 典型用途：OCR 无置信度但替代信号合格而被放行时，把 `confidence_unavailable`
      #: 记在这里，使"这份扫描件是在无置信度下放行的"在诊断与审计里可见
      #: （spec 2026-09-28-p0-3 §4.3）。
      advisory_reasons: tuple[str, ...] = ()
  ```

  并在 `__post_init__` 的归一化区（`reasons` 归一化之后）追加：

  ```python
          # advisory 只归一化，不参与任何校验——它是观测面，不是判据。
          object.__setattr__(self, "advisory_reasons", tuple(self.advisory_reasons or ()))
  ```

- [ ] **Step 4: Verify pass**

  ```powershell
  & $py -m pytest tests/unit/test_quality_substitute_signals.py tests/unit/test_quality_gate.py tests/unit/test_quality_documents.py -q
  ```

- [ ] **Step 5: 复核对齐**
  确认 spec §5 契约表中 `advisory_reasons` 的语义与实现一致（"**不**参与既有不变量校验"）。
  实现完成后 `Grep "advisory_reasons" src/medical_rag/quality/contracts.py` 复核已落盘。

---

### Task 3: `QualityGate.evaluate` 判决改造（核心修复）+ 新原因码注册

> spec §4.2 判决矩阵、§4.3。**本任务是整份 P0-3 的承重点。**

- [ ] **Step 1: Write the failing tests**
  在 `tests/unit/test_quality_substitute_signals.py` 追加。**必须显式构造
  "无置信度"**（`confidence=None`），才能触发替代判据分支：

  ```python
  # ---------------------------------------------------------------------------
  # 门禁判决改造（spec §4.2）：替代信号只能**减少** reasons，绝不新增跳过分支
  # ---------------------------------------------------------------------------


  def _ocr_doc(text: str, *, page_total: int | None = None) -> CleanedDocument:
      """模拟 OCR 产出：有内容、有锚点，但**没有置信度**（远程服务不返回）。"""
      blocks = tuple(
          CleanedBlock(raw_text=line, block_type="paragraph", source_anchor="p1", page_number=1)
          for line in text.splitlines()
          if line.strip()
      )
      return CleanedDocument(
          raw_blocks=blocks,
          cleaned_blocks=blocks,
          source_offsets=tuple(range(len(blocks))),
          cleaning_flags=(),
          cleaner_version="test",
          page_total=page_total,
      )


  def _ocr_evidence(text: str) -> list[EvidenceUnitDraft]:
      return [
          EvidenceUnitDraft(
              kind="report_text",
              text=line,
              clean_text=line,
              page_number=1,
              source_anchor="p1",
              confidence=None,  # <-- OCR：诚实置 None
          )
          for line in text.splitlines()
          if line.strip()
      ]


  _GOOD_OCR_TEXT = "\n".join(
      [
          "血常规检验报告 参考范围",
          "血红蛋白 105 g/L 参考范围 115-150 g/L 结果 降低",
          "白细胞 6.2 10^9/L 参考范围 4-10 结果 正常",
          "血小板 210 10^9/L 参考范围 100-300 结果 正常",
          "建议复查血常规 随访",
      ]
  )


  def test_scan_without_confidence_and_good_signals_passes() -> None:
      """核心修复：扫描件（无置信度）在替代信号合格时进入可检索语料。"""
      from medical_rag.quality.gate import QualityGate

      document = _ocr_doc(_GOOD_OCR_TEXT)
      decision = QualityGate().evaluate(document, _ocr_evidence(_GOOD_OCR_TEXT), [])

      assert str(decision.status) == "PASS"
      assert decision.retrieval_eligible is True
      assert "confidence_unavailable" not in decision.reasons
      assert "confidence_unavailable" in decision.advisory_reasons
      assert decision.metrics["confidence_unavailable_ratio"] == pytest.approx(1.0)


  def test_u25a0_garbage_ocr_is_held_for_review_with_the_new_reason() -> None:
      """垃圾扫描件被拦下，且用的是**新**原因码（旧指标看不见它）。"""
      from medical_rag.quality.gate import QualityGate

      garbage = "\n".join("\u25a0" * 12 for _ in range(6))
      decision = QualityGate().evaluate(_ocr_doc(garbage), _ocr_evidence(garbage), [])

      assert str(decision.status) == "NEEDS_REVIEW"
      assert decision.retrieval_eligible is False
      assert "ocr_substitute_quality_insufficient" in decision.reasons
      # 旧判据不再出现：我们不是"因为没有置信度"而拦它，而是"因为文本是垃圾"
      assert "confidence_unavailable" not in decision.reasons


  def test_replacement_character_pollution_still_passes() -> None:
      """U+FFFD 污染但文本真实：S1 实测 0.7692 > 0.60 → 放行（spec §4.5）。"""
      from medical_rag.quality.gate import QualityGate

      text = "\n".join("血红蛋白 \ufffd\ufffd\ufffd 105 g/L 参考范围 115-150" for _ in range(4))
      decision = QualityGate().evaluate(_ocr_doc(text), _ocr_evidence(text), [])

      assert str(decision.status) == "PASS", decision.reasons
      assert decision.retrieval_eligible is True


  def test_real_confidence_path_is_byte_for_byte_unchanged_low() -> None:
      """P1：有真实置信度且偏低 → 仍是 low_ocr_confidence，替代判据完全不参与。"""
      from medical_rag.quality.gate import QualityGate

      document = _ocr_doc(_GOOD_OCR_TEXT)
      evidence = [
          replace(item, confidence=0.62) for item in _ocr_evidence(_GOOD_OCR_TEXT)
      ]
      decision = QualityGate().evaluate(document, evidence, [])

      assert "low_ocr_confidence" in decision.reasons
      assert "confidence_unavailable" not in decision.reasons
      assert "ocr_substitute_quality_insufficient" not in decision.reasons
      assert decision.advisory_reasons == ()


  def test_real_confidence_path_is_byte_for_byte_unchanged_high() -> None:
      """P1：有真实置信度且合格 → PASS，且**不使用**替代判据。

      注意：替代信号键在 `metrics` 中**始终存在**（无条件计算，供观测），
      因此这里断言的是"判决未被替代判据影响"，而不是"键不存在"。
      """
      from medical_rag.quality.gate import QualityGate

      document = _ocr_doc(_GOOD_OCR_TEXT)
      evidence = [
          replace(item, confidence=0.95) for item in _ocr_evidence(_GOOD_OCR_TEXT)
      ]
      decision = QualityGate().evaluate(document, evidence, [])

      assert str(decision.status) == "PASS"
      assert decision.reasons == ()
      assert decision.advisory_reasons == ()
      assert "meaningful_character_ratio" in decision.metrics  # 仅观测，不参与判决
      assert "ocr_substitute_quality_insufficient" not in decision.reasons


  def test_substitute_pass_cannot_mask_an_unrelated_review_reason() -> None:
      """P2：替代合格只减 confidence_unavailable，绝不清除其他原因。"""
      from medical_rag.quality.gate import QualityGate

      garbage_free_but_short = "血红蛋白 105 g/L"
      document = _ocr_doc(garbage_free_but_short)
      document = replace(document, page_total=9)  # 声明 9 页，只覆盖 1 页
      decision = QualityGate().evaluate(document, _ocr_evidence(garbage_free_but_short), [])

      assert str(decision.status) == "FAIL"  # missing_pages 是结构性失败
      assert "missing_pages" in decision.reasons


  def test_substitute_insufficient_cannot_mask_missing_pages_fail() -> None:
      """S1/S3 空的输入不得把 FAIL 路径掩盖成 NEEDS_REVIEW。"""
      from medical_rag.quality.gate import QualityGate

      garbage = "\u25a0" * 12
      document = replace(_ocr_doc(garbage), page_total=3)
      decision = QualityGate().evaluate(document, _ocr_evidence(garbage), [])

      assert str(decision.status) == "FAIL"
      assert "missing_pages" in decision.reasons
  ```

  在文件顶部补 `from dataclasses import replace`。

- [ ] **Step 2: Run and verify failure**

  ```powershell
  & $py -m pytest tests/unit/test_quality_substitute_signals.py -q -k "scan_without or u25a0_garbage or replacement_character or cannot_mask"
  ```

  预期：4 个新用例失败——`test_scan_without_confidence_and_good_signals_passes`
  会因 `status` 是 `NEEDS_REVIEW`（且 reasons 含 `confidence_unavailable`）而失败；
  `test_u25a0_garbage_ocr_is_held_for_review_with_the_new_reason` 会因 reasons 含
  `confidence_unavailable` 而失败。**必须亲眼确认这两个失败信息**，
  它们正是 P0-3 缺陷的现场。

- [ ] **Step 3: Implement**

  在 `gate.py`：
  1. `evaluate` 的 `metrics` dict **末尾追加** 3 个键（追加而非插入，保持既有键顺序）：

     ```python
             # --- OCR 无置信度时的替代质量信号（仅观测；判决见下方分支）---
             "meaningful_character_ratio": _meaningful_character_ratio(document, evidence),
             "medical_term_hit_rate": _medical_term_hit_rate(document, evidence),
             "ocr_line_length_plausibility": _ocr_line_length_plausibility(
                 document, evidence, policy
             ),
         ```

     > 这 3 个键**无条件计算**（纯函数、成本可忽略），从而"替代信号"始终可观测。
     > 但**只有** `confidence_unavailable_ratio > 0.0` 时它们才参与判决——
     > 因此有真实置信度的路径**判决结果逐字节不变**（P1）。
     > 注意：Task 3 Step 1 中 `test_real_confidence_path_is_byte_for_byte_unchanged_high`
     > 的断言必须写成"存在这些键"而非"不存在"（Step 1 已按此给出）。

  2. 把 `confidence_unavailable` 分支替换为 **置信度统一处理块**：

     ```python
         # 置信度判据（唯一分支点）。三种情形，互斥：
         #   1. 有真实置信度且低于阈值 -> low_ocr_confidence
         #   2. 有真实置信度且合格     -> 不产生 reason
         #   3. 没有置信度（不可判定） -> 由**替代信号**裁决：
         #        替代合格 -> 不产生 reason（并写 advisory 以保留可追溯性）
         #        替代不合格 -> ocr_substitute_quality_insufficient
         #
         # 为什么不再把"不可判定"直接判为复核：原判据用一个在本环境恒不可取得的
         # 观测量做判决，对扫描件退化为"一律不合格"，且**不区分**识别良好的扫描件
         # 与识别成一团方块的扫描件（spec §2.4）。替代信号是可测量、确定性的，
         # 在标定集上把两者分开（spec §4.5）。
         #
         # 不变量：本块只能**减少** reasons。替代信号合格时绝不新增 reason，
         # 也绝不跳过下方任何其他判据（spec §4.2）。
         advisory: tuple[str, ...] = ()
         if metrics["confidence_unavailable_ratio"] > 0.0:
             substitute_ok = (
                 metrics["meaningful_character_ratio"] >= policy.min_meaningful_character_ratio
                 and metrics["medical_term_hit_rate"] >= policy.min_medical_term_hit_rate
                 and metrics["ocr_line_length_plausibility"]
                 >= policy.min_ocr_line_length_pass_ratio
             )
             if substitute_ok:
                 # 放行，但把"这是无置信度放行"留在观测面上（不影响 status/eligible）。
                 advisory = (REASON_CONFIDENCE_UNAVAILABLE,)
             else:
                 reasons.append(REASON_OCR_SUBSTITUTE_QUALITY_INSUFFICIENT)
         elif metrics["ocr_confidence"] < policy.min_ocr_confidence:
             reasons.append(REASON_LOW_OCR_CONFIDENCE)
     ```

  3. 末尾三个返回点带上 `advisory`：

     ```python
         if reasons:
             return QualityDecision(
                 status=QualityStatus.NEEDS_REVIEW,
                 reasons=reasons,
                 metrics=metrics,
                 retrieval_eligible=False,
                 advisory_reasons=advisory,
             )
         return QualityDecision(
             status=QualityStatus.PASS, reasons=reasons, metrics=metrics, advisory_reasons=advisory
         )
     ```

     > `missing_pages` 的 FAIL 提前返回**不变**（它不携带 advisory；
     > advisory 的用途是"无置信度却放行"的追溯，FAIL 场景本就未放行）。

  4. 补一条**P1 硬护栏**（有置信度时判决四元组与 spec 冻结期望逐字段相等）：

     ```python
  def test_confidence_path_decision_equals_frozen_expectation() -> None:
      """P1 硬护栏：有置信度时，判决四元组与 spec 冻结的期望逐字段相等。

      这是"有置信度路径行为不变"的可执行形式：任何让替代判据介入
      有置信度分支的实现都会让这条失败。
      """
      from medical_rag.quality.gate import QualityGate

      document = _ocr_doc(_GOOD_OCR_TEXT)
      good = QualityGate().evaluate(
          document, [replace(i, confidence=0.95) for i in _ocr_evidence(_GOOD_OCR_TEXT)], []
      )
      assert (str(good.status), good.reasons, good.retrieval_eligible, good.advisory_reasons) == (
          "PASS", (), True, (),
      )

      low = QualityGate().evaluate(
          document, [replace(i, confidence=0.62) for i in _ocr_evidence(_GOOD_OCR_TEXT)], []
      )
      assert (
          str(low.status), low.reasons, low.retrieval_eligible, low.advisory_reasons
      ) == ("NEEDS_REVIEW", ("low_ocr_confidence",), False, ())
     ```

  5. **`tests/unit/test_quality_gate.py` 的处置（规则 4 偏差上报）**：
     `test_missing_confidence_is_undecidable_review_not_silent_pass` 断言"缺置信度 →
     NEEDS_REVIEW"，**本任务使该断言在好文本上不再成立**——这正是修复目的。
     处置方式：
     - **不删除**该测试，**改写为**语义准确的版本（保留原意图"不得静默 PASS"，
       但把"不得"限定在**替代信号不合格**的输入上），并在 docstring 中注明
       "2026-09-28 P0-3 修复：不可判定改为由替代信号裁决，见 spec §2.4/§4.2"；
     - **必须**同时确认该文件其余 14 个用例**逐字未改**且通过。
     - 这条偏差属于"spec 明确要求的语义变更"，但改变了既有测试的期望值，
       因此在交付说明与本节显式记录。

  6. `codes.py` 注册新原因码（**唯一错误码站点**）：
     - `QualityFailureCode` 末尾追加：

       ```python
           # OCR 无置信度且替代质量信号不合格：内容仍在，但需人工确认
           # （spec 2026-09-28-p0-3 §4.3）。
           OCR_SUBSTITUTE_QUALITY_INSUFFICIENT = "ocr_substitute_quality_insufficient"
       ```

     - `_QUALITY_FAILURE_LABELS` 追加标签：

       ```python
           QualityFailureCode.OCR_SUBSTITUTE_QUALITY_INSUFFICIENT:
               "OCR 文本质量信号不足（无置信度且替代指标不合格），需人工确认",
       ```

- [ ] **Step 4: Verify pass**

  ```powershell
  & $py -m pytest tests/unit/test_quality_substitute_signals.py tests/unit/test_quality_gate.py tests/unit/test_quality_documents.py -q
  ```

  预期：全绿。`test_quality_documents.py::test_every_known_code_has_a_label`
  会**自动覆盖**新原因码有标签（它遍历枚举）。

- [ ] **Step 5: 承重不变量自查（不得跳步）**

  | 不变量 | 检查方式 |
  |---|---|
  | P1 有置信度路径不变 | `test_real_confidence_path_*` + `test_confidence_path_decision_equals_pre_change_expectation` |
  | P2 只减不增 | `test_substitute_pass_cannot_mask_an_unrelated_review_reason` |
  | 替代不合格不掩盖 FAIL | `test_substitute_insufficient_cannot_mask_missing_pages_fail` |
  | 结构性失败不被 mask | 既有 `test_missing_pages_are_a_reachable_structural_failure` **未改** 通过 |

---

### Task 4: 角色模型（`UserRole` + `AccessContext.role` + JWT claims 解析）

> spec §4.4.1。默认 `PATIENT`（最小权限）；**请求头 role 一律忽略**。

- [ ] **Step 1: Write the failing tests**
  新建 `tests/unit/test_review_auth.py`：

  ```python
  """复核角色模型（spec 2026-09-28-p0-3 §4.4.1、§10.2）。"""
  from __future__ import annotations

  from uuid import uuid4

  import pytest

  from medical_rag.auth.policy import AccessContext, UserRole


  def test_access_context_defaults_to_patient() -> None:
      """P5：默认最小权限——所有既有调用点因此行为不变且无权复核。"""
      context = AccessContext(tenant_id=uuid4(), patient_id=uuid4(), user_id=uuid4())
      assert context.role is UserRole.PATIENT


  def test_user_role_members_are_stable_strings() -> None:
      assert UserRole.PATIENT == "patient"
      assert UserRole.REVIEWER == "reviewer"
      assert UserRole.ADMIN == "admin"


  def test_context_rejects_unknown_role_value() -> None:
      with pytest.raises(ValueError):
          AccessContext(
              tenant_id=uuid4(), patient_id=uuid4(), user_id=uuid4(), role="hacker"
          )
  ```

  在既有 `tests/integration/test_auth_identity.py` **末尾追加**（不修改既有用例）：

  ```python
  # ---------------------------------------------------------------------------
  # P0-3 §4.4.1：role 声明来自 JWT；请求头 role 一律忽略（P4）
  # ---------------------------------------------------------------------------


  def test_jwt_role_claim_is_honoured(...) -> None:
      # 用既有文件的 helper 签发带 role 的 token；断言 context.role is UserRole.REVIEWER
      ...

  def test_jwt_without_role_claim_falls_back_to_patient(...) -> None:
      # 断言 context.role is UserRole.PATIENT，且不抛错
      ...

  def test_jwt_with_unknown_role_claim_is_rejected(...) -> None:
      # 断言 AuthenticationRequired（spec §10.2 的显式决策：拒，不降级）
      ...

  def test_identity_headers_cannot_escalate_role(...) -> None:
      """P4：开发开关 + 请求头 role=admin → 恒为 PATIENT（否则开关变成提权后门）。"""
      ...
  ```

  > **实现期注意**：`tests/integration/test_auth_identity.py` 已有签发 token 与
  > 调用 `current_access_context` 的 fixture。**必须先读该文件**，复用其既有 helper，
  > 不要另起一套。把上面四个 `...` 补成真实可运行的用例后再继续。

- [ ] **Step 2: Run and verify failure**

  ```powershell
  & $py -m pytest tests/unit/test_review_auth.py -q
  ```

  预期：`ImportError: cannot import name 'UserRole'`。

- [ ] **Step 3: Implement**

  `src/medical_rag/auth/policy.py`：

  ```python
  from enum import StrEnum

  class UserRole(StrEnum):
      """调用方角色。默认 `PATIENT` 是最小权限：不声明即无权复核。

      角色**只能**来自签名令牌的 `role` claim。请求头**绝不**提供角色——
      开发环境的身份头开关（`allow_insecure_identity_headers`）若也能带角色，
      就成了一个"开着开发开关即可提权"的后门（spec §4.4.1）。
      """

      PATIENT = "patient"
      REVIEWER = "reviewer"
      ADMIN = "admin"


  @dataclass(frozen=True, slots=True)
  class AccessContext:
      tenant_id: UUID
      patient_id: UUID
      user_id: UUID
      role: UserRole = UserRole.PATIENT
  ```

  > `role` 是**带默认值的新增尾字段**：所有既有位置参数调用
  > （`AccessContext(tenant_id, patient_id, user_id)`）行为不变。

  `src/medical_rag/auth/service.py`：
  1. `_context_from_claims` 在既有三项解析后追加 role 解析：

     ```python
         raw_role = claims.get("role")
         if raw_role is None or raw_role == "":
             # 未声明角色 => 最小权限。不猜测、不继承其他字段。
             role = UserRole.PATIENT
         else:
             try:
                 role = UserRole(raw_role)
             except ValueError:
                 # 令牌内容自相矛盾时，失败即拒绝优于猜测调用方意图
                 # （与既有 `_context_from_claims` 的失败口径一致；spec §10.2）。
                 raise AuthenticationRequired("token 角色声明无效") from None
         ...
         return AccessContext(
             tenant_id=patient.tenant_id, patient_id=patient.id, user_id=user_id, role=role
         )
     ```

  2. `_development_context` 的返回值**固定** `role=UserRole.PATIENT`
     （**不**从任何请求头读取），并补注释：

     ```python
         # 角色绝不从请求头取：开发开关若可携带角色，就是一个提权后门（P4）。
         return AccessContext(
             tenant_id=patient.tenant_id,
             patient_id=patient.id,
             user_id=user_id,
             role=UserRole.PATIENT,
         )
     ```

  3. `current_access_context` 的签名**不新增** role 头参数（有意：无此参数即无此攻击面）。

- [ ] **Step 4: Verify pass**

  ```powershell
  & $py -m pytest tests/unit/test_review_auth.py tests/integration/test_auth_identity.py tests/integration/test_auth_routes.py -q
  ```

  预期：全绿。**既有用例一行未改。**

---

### Task 5: `ReviewService` + `ReviewReceipt`（复核出口核心）

> spec §4.4.2、§6.1–§6.3、§7.1。**必须是原子的**：状态迁移 + 入索引建单在同一事务，
> 失败整体回滚——否则会出现"接口说已放行、检索却返回空"（正是 P0-3 的原始症状）。

- [ ] **Step 1: 先读既有代码（不得跳过）**
  - `src/medical_rag/parsing/service.py:669-926` `process`：**它才是 NeedReview 状态
    与 IndexJob 的写法发生地**。`ReviewService` 必须复用同一批接缝，不得另写一套。
  - `src/medical_rag/parsing/service.py:178-236` `__init__`：确认它的依赖注入形态
    （gate / outbox / index_job_factory / embedding 等），以决定 `ReviewService`
    是"持有 `ParsingService`"还是"复刻其接缝"。
  - `src/medical_rag/retrieval/indexing.py:300-380` `IndexWorker`：确认
    `retrieval_eligible = evidence.retrieval_eligible and ... == READY` 这一既有判定。

  > **决策原则**：优先**调用** `ParsingService` 的既有方法并传入"人工已确认"的
  > `QualityDecision`，而不是复制建单逻辑。若 `process(job_id)` 无法复用
  > （它以 `ParseJob` 为入口、会重跑解析），则在 `ParsingService` 上**抽出**一个
  > `_persist_decision(job, result, quality, dispatch_index_jobs=True)` 之类的
  > 接缝，供两条路径共用。**抽取时不得改变 `process` 的既有行为**。

- [ ] **Step 2: Write the failing tests**
  新建 `tests/unit/test_review_service.py`，覆盖 spec §10.3 的 10 行。
  关键用例（其余按同法展开）：

  ```python
  def test_patient_role_is_denied(...) -> None:
      """P5：PATIENT 复核 → 403（AccessDenied），且无任何状态变更。"""


  def test_reviewer_cannot_review_another_patients_document(...) -> None:
      """P6：跨患者/跨租户 → AccessDenied，且无状态变更。"""


  def test_document_not_in_needs_review_is_a_conflict(...) -> None:
      """状态前置：非 NEEDS_REVIEW → 409（InvalidStateTransition）。"""


  def test_confirmed_reasons_must_cover_every_current_reason(...) -> None:
      """P7：少一条 → 400 review_reasons_incomplete，列出缺失项；无状态变更。"""


  def test_extra_confirmed_reason_is_rejected(...) -> None:
      """P7：多一条 → 400 review_reasons_unknown，列出多余项；无状态变更。"""


  def test_approval_is_atomic_when_indexing_fails(...) -> None:
      """P8：注入失败的索引写作 → 文档仍 NEEDS_REVIEW，eligible 仍 False。"""


  def test_approval_writes_an_audit_event(...) -> None:
      """P9：存在一条 AuditAction.REVIEW_APPROVED，含 document_id 与 user_id。"""


  def test_approval_marks_evidence_eligible_and_path_records_ready(...) -> None:
      """核心：EvidenceUnit.retrieval_eligible 翻 True；版本/文档/ParseJob 转 READY；
      重新创建的 IndexJob 以 eligible=True 写入（用假索引断言）。"""
  ```

- [ ] **Step 3: Run and verify failure**

  ```powershell
  & $py -m pytest tests/unit/test_review_service.py -q
  ```

  预期：`ModuleNotFoundError: No module named 'medical_rag.review'`。

- [ ] **Step 4: Implement**

  1. `src/medical_rag/domain/enums.py` 的 `AuditAction` 末尾追加：

     ```python
         REVIEW_APPROVED = "review_approved"
     ```

  2. `src/medical_rag/core/errors.py` 追加两个策略异常：

     ```python
     class InvalidStateTransition(MedicalRagError):
         """当前状态不允许该操作（例如对非 NEEDS_REVIEW 文档发起复核）。"""


     class ReviewReasonsMismatch(MedicalRagError):
         """复核确认原因与文档当前原因集合不精确一致。

         ``missing``/``unknown`` 供 API 层组装 400 错误体使用。
         """

         def __init__(self, message: str, *, missing: tuple[str, ...] = (),
                      unknown: tuple[str, ...] = ()) -> None:
             super().__init__(message)
             self.missing = missing
             self.unknown = unknown
     ```

  3. 新建 `src/medical_rag/review/contracts.py`：

     ```python
     @dataclass(frozen=True, slots=True)
     class ReviewReceipt:
         document_id: UUID
         status: str
         index_job_ids: tuple[UUID, ...]
         approved_reasons: tuple[str, ...]
     ```

  4. 新建 `src/medical_rag/review/service.py`（`__init__.py` 一并建）：

     ```python
     class ReviewService:
         """把 NEEDS_REVIEW 文档人工放行为 READY（spec 2026-09-28-p0-3 §4.4.2）。

         语义是**重新评估**而非"打标放行"：复核人必须逐条确认当前 reasons，
         覆盖后重新生成一个 PASS 判决。这样 `QualityDecision` 的
         "PASS ↔ retrieval_eligible" 唯一不变量**完全不被削弱**（spec §6.1）。
         """

         def approve_document(
             self, context: AccessContext, document_id: UUID, confirmed_reasons: Sequence[str]
         ) -> ReviewReceipt: ...
     ```

     实现要点（逐条对应 spec，不得省略）：

     | # | 行为 | 失败模式 |
     |---|---|---|
     | 1 | `context.role not in {REVIEWER, ADMIN}` | `AccessDenied`（→403） |
     | 2 | 文档须属 `(tenant_id, patient_id)`（复用 `can_read_document` 口径） | `AccessDenied`（→403） |
     | 3 | 当前版本/文档须为 `NEEDS_REVIEW` | `InvalidStateTransition`（→409） |
     | 4 | 读取该版本的 `NEEDS_REVIEW` reasons（来自 `EvidenceUnit.quality_flags` 并集，或最近一次 `ParseJob.error_code` 归一化后的原因集合——**以实测为准，二选一并在代码注释里写清为什么**），与 `confirmed_reasons` **精确比对（忽略顺序）** | 少→`ReviewReasonsMismatch(missing=...)`；多→`(unknown=...)`（→400） |
     | 5 | 构造 `QualityDecision(status=PASS, reasons=(), metrics=<原 metrics 或 {}>, advisory_reasons=原 reasons)` | — |
     | 6 | 同一事务内：`EvidenceUnit.retrieval_eligible=True`（逐条）；`document.status`/`version.status`→`READY`；`ParseJob.status`→`SUCCEEDED`、`error_code=None`；**追加** `quality_flags`（不覆盖原值）；重新创建 IndexJob（复用 Step 1 抽出的接缝） | 任一步失败 → 整体回滚，状态**不变** |
     | 7 | `AuditService.record(context, AuditAction.REVIEW_APPROVED, resource_type="document", resource_id=document_id)`，`payload_metadata` 含 `approved_reasons` 与（可选）`note` | — |
     | 8 | 返回 `ReviewReceipt` | — |

     > **`note` 的处置**：仅写审计 `payload_metadata`。**绝不**进索引、不进诊断
     > （spec §4.4.3；自由文本进指标会破坏 `_safe_diagnostics` 的"不放行任意文本"口径）。

- [ ] **Step 5: Verify pass**

  ```powershell
  & $py -m pytest tests/unit/test_review_service.py -q
  ```

---

### Task 6: `POST /v1/documents/{document_id}/review` 路由

> spec §4.4.3、§10.3 最后一行。

- [ ] **Step 1: Write the failing tests**
  新建 `tests/integration/test_review_routes.py`，用 `TestClient` 覆盖五种状态码
  （200 / 400 / 403 / 404 / 409）。**必须读既有
  `tests/integration/test_documents_quality_status.py` 复用其 app/fixture 装配方式。**

  ```python
  def test_reviewer_approves_and_gets_200(...) -> None: ...
  def test_missing_reason_yields_400_with_missing_list(...) -> None: ...
  def test_unknown_reason_yields_400_with_unknown_list(...) -> None: ...
  def test_patient_role_yields_403(...) -> None: ...
  def test_invisible_document_yields_404(...) -> None: ...
  def test_wrong_state_yields_409(...) -> None: ...
  ```

- [ ] **Step 2: Run and verify failure** — 预期 404（路由不存在）。

- [ ] **Step 3: Implement**
  `src/medical_rag/api/routes/documents.py`：

  ```python
  class ReviewRequest(BaseModel):
      confirmed_reasons: list[str] = Field(min_length=1)
      note: str | None = Field(default=None, max_length=500)


  class ReviewResponse(BaseModel):
      document_id: UUID
      status: str
      index_job_count: int
      approved_reasons: list[str]


  @router.post("/{document_id}/review", response_model=ReviewResponse)
  def review_document(
      document_id: UUID,
      payload: ReviewRequest,
      context: AccessContext = Depends(current_access_context),
      session: Session = Depends(get_session),
      # ... 复用既有 review 所需依赖（按 Step 5 的实际依赖注入形态确定）
  ) -> ReviewResponse:
      """人工放行一份 NEEDS_REVIEW 文档（spec 2026-09-28-p0-3 §4.4.3）。"""
  ```

  错误映射（在 `api/app.py` 注册异常处理器，与既有 `AccessDenied`→403 同风格）：

  | 异常 | HTTP |
  |---|---|
  | `AccessDenied` | 403（**已注册**，无需新增） |
  | `InvalidStateTransition` | 409 |
  | `ReviewReasonsMismatch` | 400，`content={"detail": ..., "missing": [...], "unknown": [...]}` |

  `api/app.py` 追加：

  ```python
      @app.exception_handler(InvalidStateTransition)
      async def invalid_state_handler(
          request: Request, exc: InvalidStateTransition
      ) -> JSONResponse:
          return JSONResponse(status_code=409, content={"detail": str(exc)})

      @app.exception_handler(ReviewReasonsMismatch)
      async def review_reasons_handler(
          request: Request, exc: ReviewReasonsMismatch
      ) -> JSONResponse:
          return JSONResponse(
              status_code=400,
              content={"detail": str(exc), "missing": list(exc.missing), "unknown": list(exc.unknown)},
          )
  ```

  > **404 怎么来**：`require_document_access` 抛 `AccessDenied`（403）会让攻击者区分
  > "文档不存在"与"文档存在但不可见"。**按既有路由的既有口径处理**
  > （`documents.py:177-178` 用的是 `AccessDenied`）——本路由**与既有路由保持一致**，
  > 不引入新的信息泄漏差异。spec §4.4.3 写的 404 需按实现期实测口径修正为 403 并记录
  > （规则 4 偏差上报）。**实现前先跑既有 `test_documents_quality_status.py` 确认口径。**

- [ ] **Step 4: Verify pass**

  ```powershell
  & $py -m pytest tests/integration/test_review_routes.py tests/integration/test_document_routes.py tests/integration/test_documents_quality_status.py -q
  ```

---

### Task 7: 真实链路端到端脚本 `scripts/p0_3_ocr_review_e2e.py`

> spec §10.5、§12。**这是 P0-3 的验收证据**，不是装饰。

- [ ] **Step 1: 先读 `scripts/retrieval_cosine_gate_eval.py`（515 行，已验收）**
  复用它的：环境自检（OpenSearch 可达性 / `trust_env=False`）、`eval_out/` 输出约定、
  结论断言风格（`raise RuntimeError` 而非裸 `assert`，见 Stage-3 plan 的规则 4 记录）。

- [ ] **Step 2: Implement**
  实现 spec §10.5 的六条断言：

  | # | 断言 | 期望 |
  |---|---|---|
  | ① | 真实扫描件形态（无置信度、内容正常）→ 门禁 PASS | `retrieval_eligible=True`，advisory 含 `confidence_unavailable` |
  | ② | U+25A0 垃圾 OCR → 门禁 NEEDS_REVIEW | reason 恰为 `{ocr_substitute_quality_insufficient}` |
  | ③ | 复核放行后可被检索 | 真实 `RetrievalService.retrieve` 命中该 id（**显式传 `k=top_k`**） |
  | ④ | 复核前不可检索（负样本） | 同一查询返回 0 条 |
  | ⑤ | 零容忍三项 == 0 | 越权 / 删除残留 / 复核泄漏 均为 0 |
  | ⑥ | 幂等 | 第二次复核 → 409；索引该 id 计数不变 |

  输出 `eval_out/p0_3_e2e.json`。

  > **已知坑**：`RetrievalService.retrieve` 必须**显式传 `k=top_k`**，
  > 否则用默认值会让断言与口径不符（本项目评估红线，见 MEMORY）。

- [ ] **Step 3: Run**

  ```powershell
  & $py scripts/p0_3_ocr_review_e2e.py
  ```

  预期：六条断言全 PASS，`eval_out/p0_3_e2e.json` 落盘。
  若 OpenSearch 不可达，脚本应**显式失败并说明**，不得静默跳过。

- [ ] **Step 4: 清理**
  脚本内的临时数据必须自清理（用后删）。仓库根目录不得残留探针脚本。
  交付前把 `.workbuddy/` 清理到只剩 `memory/`（Python `os.remove` + `os.listdir` 复核）。

---

## 阶段 1b（P0-3：公共路径 fail-closed；Task 8–9）

> 与阶段 1a **无依赖**。若评审希望缩小提交面，可整段推迟，不影响 1a 验收。

### Task 8: `indexing/orchestrator._public_record` 改 fail-closed

> spec §2.3(a)、§6.6、§10.4。**实测为不可达死代码，因此对现网行为零变化**——
> 这是"修隐患不改行为"的理想形态。但必须用测试把新语义**钉死**。

- [ ] **Step 1: Write the failing tests**
  在 `tests/unit/test_index_orchestrator.py` 末尾追加（**不修改既有用例**）：

  ```python
  # ---------------------------------------------------------------------------
  # P0-3 §6.6：公共记录资格改读 evidence.retrieval_eligible（fail-closed）
  # ---------------------------------------------------------------------------


  def test_public_record_respects_persisted_eligibility(...) -> None:
      """P11：evidence.retrieval_eligible=False → 不产出可检索记录。"""
      # 构造 eligible 列为 False 的 EvidenceUnit（其余条件都满足）
      # 断言：抛 RuntimeError（既有 _process_public 会把它转成 retry）
      ...


  def test_public_record_refuses_needs_review_version(...) -> None:
      """收窄：version.status == NEEDS_REVIEW 不再放行（与私有路径 IndexWorker 对齐）。"""


  def test_public_record_still_accepts_ready_and_eligible(...) -> None:
      """volume 不变：READY + eligible=True → 记录 retrieval_eligible=True。"""
  ```

  > **实现期必须读 `tests/unit/test_index_orchestrator.py`**，复用其既有的
  > `_public_record` 调用方式与 fixture（它已能构造 job/evidence/version/document 四元组）。
  > 把上面的 `...` 补成真实可运行用例。**不要新起 fixture 体系。**

- [ ] **Step 2: Run and verify failure**

  ```powershell
  & $py -m pytest tests/unit/test_index_orchestrator.py -q -k "public_record_respects or public_record_refuses"
  ```

  预期：两条新用例失败（`RuntimeError` 未抛出 / 记录仍为 `eligible=True`）。

- [ ] **Step 3: Implement**
  `src/medical_rag/indexing/orchestrator.py:245-274`：

  ```python
      def _public_record(self, job: IndexJob, evidence: EvidenceUnit, document: Document | None, version: DocumentVersion) -> IndexRecord:
          # 资格判定必须**读持久化的 evidence.retrieval_eligible**，不得硬编码 True：
          # 门禁已标记为不可检索的证据，不允许在公共路径被重新放行（spec §6.6）。
          # 状态要求收窄为 == READY（原为 {READY, NEEDS_REVIEW}），与私有路径的
          # IndexWorker 判定对齐——"写入但标记"的正确含义是该记录存在但不可检索，
          # 而不是"NEEDS_REVIEW 也算合格"。
          eligible = (
              evidence.retrieval_eligible
              and version.status == DocumentStatus.READY
              and (document is None or document.status == DocumentStatus.READY)
              and evidence.deleted_at is None
              and version.deleted_at is None
              and (document is None or document.deleted_at is None)
          )
          if not eligible:
              raise RuntimeError("public source is not active and retrieval-eligible")
          return IndexRecord(
              ...
              retrieval_eligible=eligible,   # 用判定值，不再硬编码 True
              ...
          )
  ```

- [ ] **Step 4: Verify pass**

  ```powershell
  & $py -m pytest tests/unit/test_index_orchestrator.py -q
  ```

  预期：全绿。**既有用例一行未改**（`_public_record` 的调用方在 `_process_public`
  里把异常转成 retry，因此"抛错"这一行为契约未变）。

- [ ] **Step 5: 记录可达性（规则 4）**
  在实现完成后于交付说明中重述：该分支实测不可达（公共知识走 `KnowledgeIngestService`），
  本次修改**对现网行为零变化**，价值在于关闭 fail-open 隐患。
  **不要**为了"让它可达"去改 `_classify_visibility`——那是行为变更，不在本次范围。

---

### Task 9: 公共知识入库接入 `QualityGate` + `KnowledgeChunk.quality_flags`

> spec §2.3(b)、§6.5、§10.4、§11 阶段 1。**本次影响面最大的一处**。

- [ ] **Step 1: 明知影响面（不得跳过）**
  spec §6.5 已明确：接入门禁后，现公共库 5 条全部来自自述"未经医学审核"的样例文件，
  会在下次 reindex 时被判复核 → **公共检索返回空**。这是**正确行为**，但必须在
  交付说明中显式提示。**不做**自动豁免、不改判据去迁就现语料。

- [ ] **Step 2: Write the failing tests**
  在 `tests/unit/test_knowledge_ingest.py` 末尾追加：

  ```python
  # ---------------------------------------------------------------------------
  # P0-3 §6.5：公共入库接入 QualityGate（P12）
  # ---------------------------------------------------------------------------


  def test_garbage_chunk_is_persisted_but_not_retrievable(...) -> None:
      """垃圾 chunk → retrieval_eligible=False + quality_flags 落原因码 + 不进公共检索。"""


  def test_good_chunk_is_retrievable(...) -> None:
      """现状保持：好文本 chunk 仍 retrieval_eligible=True。"""


  def test_quality_flags_column_is_populated_for_review_chunks(...) -> None:
      """新增列的持久化契约。"""
  ```

- [ ] **Step 3: Run and verify failure** — 预期新用例失败（无 quality_flags 列、恒 eligible）。

- [ ] **Step 4: Implement**

  1. `src/medical_rag/domain/models.py` 的 `KnowledgeChunk` 追加列（与
     `EvidenceUnit.quality_flags` 对称）：

     ```python
         # 门禁标记（P0-3 §6.5）：与 EvidenceUnit.quality_flags 对称，
         # 使"公共分块为何不可检索"可在 DB 侧回答，而不必重跑门禁。
         quality_flags: Mapped[list | None] = mapped_column(JSON)
     ```

  2. alembic 迁移：新建 `alembic/versions/0009_add_knowledge_chunk_quality_flags.py`，
     `down_revision = "0008"`（**实现前先 `Read` `0008_add_chat_message_indexes.py`
     确认其 `revision` 标识符的确切字面量**），`upgrade()` 加列、`downgrade()` 删列。

  3. `src/medical_rag/knowledge/ingest_service.py`：
     - 在 `ingest` 构造 `PublicIndexRecord` 前，对每个 draft 调用门禁：
       ```python
       decision = self.quality_gate.evaluate(
           _single_block_document(draft.text),   # 单块 CleanedDocument
           [_single_evidence(draft.text)],       # 单条 EvidenceUnitDraft，confidence=1.0
           [],
       )
       ```
       （`confidence=1.0` 的理由：公共知识来自结构化文本，**不存在 OCR 置信度问题**；
       若这里也置 None，就会走进替代判据分支——那是给扫描件的判据，用在这里是误用。）
     - `retrieval_eligible=decision.retrieval_eligible`，`quality_flags=list(decision.reasons) or None`。
     - `reindex_source`（`:340-366`）**同样处理**——它是"停用后重新启用的写回路径"，
       漏掉它会造成"全新入库被拦、reindex 却被放行"的不对称（本项目已因这类不对称
       返工过一次，见 `reindex_source` 的既有 docstring）。
     - `QualityGate` 通过构造参数注入（可空：为 `None` 时**保持现状**
       `retrieval_eligible=True`），以便既有测试与调用点行为可预测。
       注入点按 Step 5 实测确定，**不得**用模块级单例。

- [ ] **Step 5: 接线（关键，容易漏）**
  `Grep "KnowledgeIngestService\(" src/ scripts/` 找到所有构造点，
  逐一传入 `QualityGate()`。**先跑既有测试**看哪些构造点需要更新。

- [ ] **Step 6: Verify pass**

  ```powershell
  & $py -m pytest tests/unit/test_knowledge_ingest.py -q
  & $py -m pytest tests/ -q
  ```

  第二条预期：**≥754 passed / 3 skipped / 0 failed**（基线 754；新增约 35 例）。
  若新增数远低于预期，说明有测试没写。

---

## 阶段 3A（门禁启用：可观测性 + artifact + 自检 + 干跑；Task A1–A5）

> **不改 `.env`**。可立即执行。

### Task A1: `_safe_diagnostics` 白名单扩展（D1 修复）

> spec §5.1、§10.1。**只放行有界枚举值与内部标识符，绝不放行任意自由文本。**

- [ ] **Step 1: Write the failing tests**
  在 `tests/unit/test_chat_route_and_metric_reasons.py`（已 `import` `_SAFE_REASONS`）
  **或**新建 `tests/unit/test_safe_diagnostics_gate_keys.py` 追加：

  ```python
  def test_gate_policy_is_readable_not_hashed() -> None:
      """G3：监控必须能读到"当前门禁口径"。"""
      from medical_rag.observability.metrics import _safe_diagnostics

      assert _safe_diagnostics({"gate_policy": "cosine_absolute"})["gate_policy"] == "cosine_absolute"


  def test_gate_skipped_reason_is_readable_not_hashed() -> None:
      """G4：门禁被跳过是最危险的情形，必须可读（否则无法告警）。"""
      from medical_rag.observability.metrics import _safe_diagnostics

      safe = _safe_diagnostics({"gate_skipped_reason": "vector_path_unavailable"})
      assert safe["gate_skipped_reason"] == "vector_path_unavailable"
      assert _safe_diagnostics({"gate_skipped_reason": None})["gate_skipped_reason"] is None


  def test_gate_score_space_is_readable_not_hashed() -> None:
      from medical_rag.observability.metrics import _safe_diagnostics

      assert _safe_diagnostics({"gate_score_space": "l2_inv"})["gate_score_space"] == "l2_inv"


  def test_unknown_gate_policy_value_is_still_hashed() -> None:
      """G5：白名单绝不扩展自由文本面。"""
      from medical_rag.observability.metrics import _safe_diagnostics

      value = _safe_diagnostics({"gate_policy": "hacked"})["gate_policy"]
      assert isinstance(value, str) and value.startswith("sha256:")


  def test_gate_whitelists_match_the_enums_byte_for_byte() -> None:
      """G6：白名单用**字面量**集合，必须与枚举实际取值逐字对齐。

      用字面量（而非 {p.value for p in GatePolicy}）是为了让"新增枚举值但忘了
      加白名单"变成**测试失败**，而不是静默扩展指标面（spec §5.1）。
      """
      from medical_rag.observability.metrics import (
          _SAFE_GATE_POLICIES, _SAFE_GATE_SKIP_REASONS, _SAFE_SCORE_SPACES, SCORE_SPACES,
      )
      from medical_rag.retrieval.contracts import GatePolicy

      assert _SAFE_GATE_POLICIES == {p.value for p in GatePolicy}
      assert _SAFE_SCORE_SPACES == set(SCORE_SPACES)
      assert _SAFE_GATE_SKIP_REASONS == {"vector_path_unavailable"}


  def test_free_text_diagnostic_keys_are_still_hashed() -> None:
      """G13 负例：任意自由文本（如正文/患者信息）绝不被放行。"""
      from medical_rag.observability.metrics import _safe_diagnostics

      assert _safe_diagnostics({"note": "患者姓名张三"})["note"].startswith("sha256:")
      assert _safe_diagnostics({"gate_policy_reason": "任意文本"})["gate_policy_reason"].startswith("sha256:")


  def test_gate_skip_reason_values_are_locked_to_service_emission() -> None:
      """取值来源锁定：service.py 只发出这一个 skip reason（新增值必须同步白名单）。"""
      import inspect

      import medical_rag.retrieval.service as service

      source = inspect.getsource(service)
      emitted = set(re.findall(r'gate_skipped_reason"\]\s*=\s*"([a-z_]+)"', source))
      assert emitted <= _SAFE_GATE_SKIP_REASONS, f"未登记: {emitted - _SAFE_GATE_SKIP_REASONS}"
  ```

  文件顶部补 `import re`。

- [ ] **Step 2: Run and verify failure**

  ```powershell
  & $py -m pytest tests/unit/test_safe_diagnostics_gate_keys.py -q
  ```

  预期：`gate_policy` 相关用例失败（返回值是 `sha256:...` 而非枚举值）。

- [ ] **Step 3: Implement**
  `src/medical_rag/observability/metrics.py`：

  1. 在 `_SAFE_STATUSES` 附近新增三个字面量集合（与 `_SAFE_REASONS` 同级）：

     ```python
     # 有界枚举值，与 retrieval/contracts.GatePolicy 的成员逐字对齐。
     # 用字面量而非推导式：新增枚举值却忘了登记时，测试会失败，
     # 而不是静默扩大指标放行面（tests/unit/test_safe_diagnostics_gate_keys.py）。
     _SAFE_GATE_POLICIES = frozenset(
         {"top_k", "calibrated", "explicit_threshold", "rank_gate", "cosine_absolute"}
     )
     # 向量分数空间（与 metrics.SCORE_SPACES 一致）——阈值语义随它变，必须可读。
     _SAFE_SCORE_SPACES = frozenset({"l2_inv", "cosinesimil"})
     # 门禁被跳过的原因。这是"静默失去拒答能力"的唯一信号，必须可读且可告警。
     _SAFE_GATE_SKIP_REASONS = frozenset({"vector_path_unavailable"})
     ```

  2. `_safe_diagnostics` 的 `isinstance(value, str)` 分支**追加**三个 elif
     （放在既有 `index_version`/`embedding_model` 分支之后、`else: _hash` 之前）：

     ```python
             elif key == "gate_policy":
                 safe[key] = value if value in _SAFE_GATE_POLICIES else _hash(value)
             elif key == "gate_score_space":
                 safe[key] = value if value in _SAFE_SCORE_SPACES else _hash(value)
             elif key == "gate_skipped_reason":
                 safe[key] = value if value in _SAFE_GATE_SKIP_REASONS else _hash(value)
     ```

     > **`gate_skipped_reason` 的 `None`** 由上方 `value is None` 分支直通，无需特殊处理
     > （测试已覆盖）。

- [ ] **Step 4: Verify pass**

  ```powershell
  & $py -m pytest tests/unit/test_safe_diagnostics_gate_keys.py tests/unit/test_chat_route_and_metric_reasons.py tests/unit/test_retrieval_metrics.py -q
  ```

- [ ] **Step 5: 不修的部分（显式）**
  `calibration_mismatch_records`（tuple-of-dict）**保持 `_hash`**：无有界枚举形态，
  放行需要新的逐字段脱敏逻辑，属独立任务（spec §9 D12）。**不要顺手放行它。**

---

### Task A2: 正式 artifact 文件 `config/calibration/cosine-gate-2026-09-26.json`

> spec §4.2、§10.3。

- [ ] **Step 1: Write the failing test**
  新建 `tests/contract/test_calibration_artifact_file.py`：

  ```python
  """仓库内正式标定产物文件的契约（spec 2026-09-28-cosine-gate-enablement §4.2/§10.3）。"""
  from __future__ import annotations

  import json
  from pathlib import Path

  import pytest

  from medical_rag.observability.metrics import CalibrationArtifact

  REPO = Path(__file__).resolve().parents[2]
  ARTIFACT = REPO / "config" / "calibration" / "cosine-gate-2026-09-26.json"


  def test_artifact_file_exists_and_is_version_controlled() -> None:
      """G7：文件必须存在且不被 .gitignore 排除（否则"启用"依赖一个未入库的文件）。"""
      assert ARTIFACT.is_file(), f"缺失: {ARTIFACT}"
      ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
      assert not any(line.strip() == "config/" for line in ignored)


  def test_artifact_file_constructs_a_valid_calibration_artifact() -> None:
      artifact = CalibrationArtifact(**json.loads(ARTIFACT.read_text(encoding="utf-8")))
      assert artifact.rrf_k == 60
      assert artifact.cosine_threshold == pytest.approx(0.5)
      assert artifact.score_space == "l2_inv"
      assert artifact.public_index_version == "public-v1"
      assert artifact.private_index_version == "private-v1"


  def test_artifact_embedding_model_matches_the_local_model_identity() -> None:
      from medical_rag.core.config import Settings
      from medical_rag.models.factory import resolve_embedding

      artifact = CalibrationArtifact(**json.loads(ARTIFACT.read_text(encoding="utf-8")))
      settings = Settings(_env_file=None, embedding_backend="local")
      assert artifact.embedding_model == resolve_embedding(settings).model_name
  ```

  > **已核实（无需再猜）**：`resolve_embedding` 返回 `EmbeddingComposition`，
  > 其模型身份属性是 **`.model_name`**（`models/factory.py:44-54`）。
  > spec §4.2 曾写的 `.model.name` 已在 spec 内更正（规则 4）。

- [ ] **Step 2: Run and verify failure** — 预期 `FileNotFoundError`/断言失败。

- [ ] **Step 3: Implement**
  新建 `config/calibration/cosine-gate-2026-09-26.json`（spec §4.2 的九个字段，值已定论）：

  ```json
  {
    "index_version": "artifact-2026-09-26",
    "embedding_model": "BAAI/bge-small-zh-v1.5",
    "rrf_k": 60,
    "threshold": 0.42,
    "eval_dataset_version": "cosine-gate-2026-09-26",
    "cosine_threshold": 0.50,
    "score_space": "l2_inv",
    "public_index_version": "public-v1",
    "private_index_version": "private-v1"
  }
  ```

  > **不新增 `.gitignore` 规则**：已核实 `.gitignore` 不排除 `config/`，无需改动。
  > **不得**把该文件放 `.env`/密钥目录。

- [ ] **Step 4: Verify pass**

  ```powershell
  & $py -m pytest tests/contract/test_calibration_artifact_file.py -q
  ```

---

### Task A3: 启动期门禁口径自检（`api/app.py`）

> spec §4.3（含"必须放在 `try/except` 之外"与"条件写路径非空"两条要点）、§10.2、G14。

- [ ] **Step 1: Write the failing tests**
  新建 `tests/integration/test_app_lifespan.py`（若已存在则**追加**）：

  ```python
  def test_no_artifact_path_starts_with_top_k_gate(...) -> None:
      """回归线：未配置路径 → 启动成功且门禁口径仍是 TOP_K（行为与改动前一致）。"""


  def test_correct_artifact_path_starts_with_cosine_gate_and_logs(...) -> None:
      """正确 artifact → 启动成功；caplog 含 "cosine gate enabled"。"""


  def test_artifact_without_cosine_threshold_refuses_to_start(...) -> None:
      """G2：配了 artifact 却选不出余弦口径 → 启动失败（不得静默退回 TOP_K）。"""


  def test_missing_artifact_file_refuses_to_start(...) -> None:
      """artifact 文件不存在 → FileNotFoundError（既有行为，本次不改）。"""


  def test_self_check_is_not_swallowed_in_non_production(...) -> None:
      """G14：非生产环境 + 配了路径 + 编排器未装配 → **仍**抛错。

      回归线：若自检被放进 lifespan 的 try/except 内，非生产环境的
      log-and-continue 分支会把它吞掉，这条测试就会失败。
      """
      # 构造：settings.allow_insecure_identity_headers=False、is_production=False、
      #       calibration_artifact_path 指向 Task A2 的正式文件，
      #       并让 build_chat_orchestrator 失败（monkeypatch 抛错）。
      # 断言：pytest.raises(RuntimeError, match="未装配")
  ```

  > **实现期注意**：既有 `tests/integration/test_startup_validation.py` 已有
  > "开发环境容忍装配失败"与"生产环境拒绝启动"的用例。**复用其 Settings 构造方式**
  > （`Settings(_env_file=None, ...)`），并把**它是否因本任务变红**作为重点检查项：
  > 若那些用例配了 `calibration_artifact_path`，本自检会让它们在非生产环境也抛错——
  > 而"开发环境容忍装配失败"是**既有正确行为，必须保留**。
  > 解决方式即 spec §4.3 的条件设计：**未配路径时不检查**。先跑再看。

- [ ] **Step 2: Run and verify failure** — 预期"配置 artifact 却退回 TOP_K 仍启动成功"。

- [ ] **Step 3: Implement**
  1. `api/app.py` 新增模块级函数 `_assert_configured_gate_is_active(app, settings)`
     （spec §4.3 给出的完整实现，逐字采用）。
  2. 在 `lifespan` 的 `except` 块之后、`yield` 之前调用它。
  3. 确认不新增任何 import 循环（`GatePolicy` 在函数内 import）。

- [ ] **Step 4: Verify pass**

  ```powershell
  & $py -m pytest tests/integration/test_app_lifespan.py tests/integration/test_startup_validation.py tests/unit/test_health.py -q
  ```

- [ ] **Step 5: 全量**

  ```powershell
  & $py -m pytest tests/ -q
  ```

  预期 ≥754 passed / 3 skipped / 0 failed。**若 `test_startup_validation.py` 变红，
  说明自检条件写错了（多半漏了"路径非空才检查"），必须修条件而不是改那些测试。**

---

### Task A4: `scripts/cosine_gate_enable_e2e.py`（干跑 + 断言 ⑦）

> spec §6.2、§10.4、§12。**这是申请用户批准改 `.env` 的依据。**

- [ ] **Step 1: 复用既有验收脚本**
  读 `scripts/retrieval_cosine_gate_eval.py`（515 行，已验收）。新脚本与它的差别只有两点：
  1. **装配来源**：直接读 `config/calibration/cosine-gate-2026-09-26.json` 经
     `CalibrationArtifact(**payload)`（**不经过 `.env`**）；
  2. **额外断言 ⑦**（规范§6.2 第 3 条）：构造"向量路径不可用" →
     断言 `gate_skipped_reason == "vector_path_unavailable"`，且
     `_safe_diagnostics` 之后该值**可读**（D1 修复的端到端验证）。

- [ ] **Step 2: Implement**
  断言 ①–⑥ 与 Task 6 口径**完全一致**（拒答 2/2、排序指标逐值相等、P95 ≤1.2×、
  零容忍 == 0、混合作用域零返回 == 0、mismatch == 0）。输出 `eval_out/cosine_gate_enable_e2e.json`。

  > 断言形式上使用 `raise RuntimeError` 而非裸 `assert`（既有的正确做法：
  > 裸 `assert` 会被 `python -O` 剥离，使关键自检静默失效）。

- [ ] **Step 3: Run**

  ```powershell
  & $py scripts/cosine_gate_enable_e2e.py
  ```

  预期：断言 ①–⑦ 全 PASS。OpenSearch 不可达时**显式失败**，不静默跳过。

---

### Task A5: 回滚不变量测试（G1）

> spec §6.1、§10.5。

- [ ] **Step 1: Write the failing test**
  在 `tests/integration/test_scheme_b_rollout.py`（已覆盖 artifact 装配）**追加**
  （不改既有 9 个用例）：

  ```python
  def test_rollback_is_byte_for_byte_identical(...) -> None:
      """G1：同一 artifact 文件"存在/不存在"两态对比。

      无门禁态（不配路径）的全部指标必须逐值相等；有门禁态才出现
      拒答与 gate_policy == cosine_absolute。
      """
  ```

- [ ] **Step 2–4: 失败 → 实现（按需） → 通过**
  若实现期发现**无需任何生产代码改动即可通过**（因为回滚语义本就由
  `load_calibration_artifact` 返回 None 承担），则本任务退化为**纯护栏测试**——
  这本身是有价值的，且必须在报告中说明"未改动生产代码"。

---

## 阶段 3B（启用；**需用户单独批准**）

> ⚠️ **不得在未获单独批准时执行**。spec 的批准 ≠ 批准修改 `.env`。
> 用户需明确说"批准启用"或等价表述。

### Task B1: 写入 `.env`

- [ ] 在 `.env` 增加一行（并保留一行注释掉的回滚说明）：

  ```bash
  CALIBRATION_ARTIFACT_PATH=config/calibration/cosine-gate-2026-09-26.json
  ```

- [ ] **不得**通过其他方式启用（不改代码默认值、不加环境变量到部署脚本）。
  理由：单一启用入口才能保证回滚是"注释一行"。spec §6.1。

### Task B2: 启用后验证（spec §6.3）

- [ ] 启动日志出现 `cosine gate enabled: ...` 且不抛错
- [ ] `curl -s localhost:8000/health/ready` → 200
- [ ] `python scripts/cosine_gate_enable_e2e.py` → 断言 ①–⑦ 全 PASS
- [ ] 端到端行为：无关查询 → 0 条 + `insufficient_evidence`；可答查询 → 正常且排序与 baseline 一致

### Task B3: 部署检查项交付

- [ ] 交付说明列出三条告警条件（spec §6.4），并明确它们**未接线**（属运维配置，D17）：
  1. `gate_skipped_reason == "vector_path_unavailable"` 任意非零 → 告警（**静默失去拒答能力**）
  2. `gate_policy != "cosine_absolute"` → 生产环境告警（装配退化）
  3. `calibration_artifact_mismatch` 为真 → 告警（fail-closed，服务不可用）
- [ ] 交付说明列出回滚步骤：注释 `.env` 那一行并重启。

---

## 验收标准

```powershell
$py = "C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"

# 1. 全量测试（基线 754 passed / 3 skipped / 0 failed）
& $py -m pytest tests/ -q

# 2. 静态检查（本次改动文件必须无新增告警）
& $py -m ruff check src/medical_rag tests scripts

# 3. P0-3 真实链路（断言 ①–⑥）
& $py scripts/p0_3_ocr_review_e2e.py

# 4. 阶段 3A 干跑（断言 ①–⑦；不改 .env）
& $py scripts/cosine_gate_enable_e2e.py

# 5. 确认 .env 未被修改（阶段 3B 之前）
Get-Content .env | Select-String 'CALIBRATION_ARTIFACT_PATH'
# 预期：无输出（或仅有注释行）
```

**完成判定**（对齐 `FEATURE-WORKFLOW` §5）：

- [ ] 两份 spec 已批准，全部目标实现，非目标确实没做
- [ ] 每个任务独立验收
- [ ] 全量测试通过（记录通过/跳过数，并说明相对基线 754 的增减）
- [ ] 两条真实链路脚本跑通，有输出证据（`eval_out/p0_3_e2e.json`、`eval_out/cosine_gate_enable_e2e.json`）
- [ ] P0-3 §8 P1–P13 逐条断言通过（**尤其 P1：有置信度路径行为不变**）
- [ ] 阶段 3A §8 G1–G14 逐条断言通过（**尤其 G1 回滚一致、G3/G4 可观测、G14 自检不可绕过**）
- [ ] 降级路径验证过（替代不合格 / 角色不足 / 状态冲突 / 入索引失败回滚 / 门禁跳过 / fail-closed）
- [ ] 偏差与遗留项显式记录（P0-3 §9 D6–D11；阶段 3 §9 D12–D17）
- [ ] **`.env` 未被修改**（阶段 3B 前）
- [ ] 临时探针文件已清理（`.workbuddy/` 仅剩 `memory/`）

---

## Plan Self-Review

**任务粒度**：16 个任务（P0-3 阶段 1a：Task 1–7；1b：Task 8–9；

阶段 3A：Task A1–A5；阶段 3B：Task B1–B3），每个都可独立验收。

**依赖顺序**：

| 分组 | 内部依赖 | 与外部的关系 |
|---|---|---|
| 1a | Task 1 → 2 → 3；Task 4 独立；Task 3+4 → 5 → 6；Task 7 依赖 3+6 | 与 1b、3A **无依赖** |
| 1b | Task 8 独立；Task 9 独立 | 与 1a 无依赖 |
| 3A | A1、A2 独立；A3 依赖 A2（自检需要真 artifact 做正例）；A4 依赖 A1+A2；A5 依赖 A2 | 依赖 P0-1（已验收） |
| 3B | B1 → B2 → B3 | 依赖 3A 全部 |

**建议执行顺序**：`1a(1→2→3)` → `1a(4→5→6→7)` → `3A(A1→A2→A3→A4→A5)` → `1b(8→9)`。
把 1b 放最后，因为它对现公共库有**可见影响**（公共检索返回空），
与 3A 的"不改行为"性质相反，宜在 1a+3A 验收通过后单独收尾。

**TDD 完整性**：Task 1/2/3/4/5/6/8/9/A1/A2/A3/A5 均写明"确认失败"步骤与
**预期看到的具体失败信息**。Task 7 与 A4 是脚本任务（无 pytest 阶段），
已写明显式自检与失败模式。Task A5 显式说明"可能退化为纯护栏测试"。

**风险点与对应防线**：

1. **Task 3 是承重点**：`test_quality_gate.py` 里
   `test_missing_confidence_is_undecidable_review_not_silent_pass` 的期望值**必然改变**
   （这正是修复目的）。处置方式已写死：**改写而非删除**，并在 spec/交付说明中上报偏差。
   其余 14 例**逐字未改**是硬性要求。
2. **Task 5 的原子性是 P0-3 的核心**：若允许"状态已 READY 但入索引失败"，
   就复现了原始症状（接口说成功、检索返回空）。Step 2 的
   `test_approval_is_atomic_when_indexing_fails` 是**不可省略**的。
3. **Task 9 会改变现公共库的可见行为**（5 条样例全部转复核 → 公共检索返回空）。
   这是正确行为，但**必须在批准前明确告知用户**，不能作为"实施期惊喜"。
4. **Task A3 的自检位置**：放进 `try/except` 会被非生产环境吞掉（spec §4.3 已修正）。
   条件必须写"路径非空"而非"artifact 非 None"，否则漏掉"配了路径但装配失败"这一组合。
   G14 测试专守此点。
5. **`Edit` 未落盘**：Global Constraints 已要求每次编辑后 `Grep`/`Read` 复核。
   本项目在 `retrieval_gate_prototype.py` 上真实踩到过 dataclass 字段未写入。
6. **`tests/integration/test_startup_validation.py` 是隐形雷区**：本自检若条件写错，
   会让"开发环境容忍装配失败"的既有正确行为变红。**先跑再看，修条件而不是改测试。**

**非目标确认**：本计划不含 `cosine_threshold`/`score_space`/`rrf_k`/`top_k`/`space_type`
的任何数值改动；不含 `embedding_model` 校验方式变更；不含全仓 format；
不含存量 `NEEDS_REVIEW` 回填（P0-3 §11 阶段 2）；不含公共语料替换（阶段 3）；
不含 D12（mismatch 明细放行）、D13（死配置）、D14（版本命名）、D15（假绿零容忍）、
D16（双份指标体系）、D17（告警通道接线）。

**⚠️ 批准边界（必须向用户明示）**：

- 批准本计划 = 批准执行 **阶段 1a + 1b + 3A**。
- **不等于**批准修改 `.env`。阶段 3B 需用户**单独**明确说"批准启用"。
- 批准本计划也**不等于**批准 P0-3 §11 阶段 2/3（阈值收紧、存量回填、公共语料替换）。

