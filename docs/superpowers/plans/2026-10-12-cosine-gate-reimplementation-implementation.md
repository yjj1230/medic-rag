# 余弦相关性门禁恢复实施计划（Re-implementation Plan）

- 日期：2026-10-12
- 对应 spec：`docs/superpowers/specs/2026-10-12-cosine-gate-reimplementation-design.md`
- 前置：**spec 批准后才开始编码**（FEATURE-WORKFLOW 审批门禁）
- 验收口径：**行为等价**（复现 09-24/09-28 已验收的六项断言），非逐行一致

---

## 任务总览

| # | 任务 | 交付物 | 独立验收断言 | 阻塞关系 |
|---|---|---|---|---|
| T0 | 配置-代码对齐体检 | 体检报告（`.env` 每个生效项逐条核对） | 已发现 1 项（artifact 字段不认）+ 未知项清单 | 无（先做） |
| T1 | 契约层恢复 | `metrics.py` / `contracts.py` 变更 | 契约测试全绿；旧 artifact 行为不变 | T0 |
| T2 | 门禁逻辑恢复 | `service.py` 候选级门禁 + 诊断键 | 单元测试：拒答/交集/跳过/不变量 | T1 |
| T3 | 标定分派恢复 | `expected_index_version_for` + mismatch 记录 | 双索引用例 mismatch 数 6→0 | T1 |
| T4 | 组装与启动自检 | `composition.py` / `app.py` 自检 | 配 artifact → `COSINE_ABSOLUTE`；配而未装配 → 启动报错 | T2,T3 |
| T5 | 测试重建 | `tests/unit`、`tests/integration` 新增用例 | 新增用例数与原 24 项等价；全量 pytest 全绿 | T1–T4 |
| T6 | 端到端验收脚本 | `scripts/retrieval_cosine_gate_eval.py`、`scripts/cosine_gate_enable_e2e.py` | 拒答 2/2；指标逐值相等；零容忍 0；P95 ≤1.2× | T5 |
| T7 | 遗留项登记 | 更新 `docs/问题修复说明-2026-10-12.md` 与 D1 台账 | 偏差、遗留、D1 显式记录 | T6 |

---

## T0 配置-代码对齐体检（先做，防止只修门禁又踩别的坑）

1. 逐条读取 `.env` 中**未注释**的键，与 `core/config.py` 的 `Settings` 字段比对（`extra="ignore"` 会吞掉不存在的键 —— 这是评审 P0-6）。
2. 重点核对：`CALIBRATION_ARTIFACT_PATH`（已知不匹配）、`EMBEDDING_*`、`RETRIEVAL_*`、`RAG_TEXT_ANALYZER`（config 注释自承靠约定一致）、`ALLOW_PLAINTEXT_PHI_EGRESS`（评审 P0-3，代码中不存在该字段）。
3. 产出：`docs/配置-代码对齐体检-2026-10-12.md`（生效键 × 是否被源码识别 × 影响）。
4. **不做修复**（P0-6 的 `extra="forbid"` 是独立任务），只出清单供排序。

**验收**：清单覆盖 `.env` 全部生效键，每项给出"识别/未识别/未识别但无害"判定。

---

## T1 契约层恢复

文件：`src/medical_rag/observability/metrics.py`

1. `CalibrationArtifact` 新增 4 个可选字段（`cosine_threshold` / `score_space` / `public_index_version` / `private_index_version`）。
2. `__post_init__` 补 4 条校验（成对 + 取值域），错误消息指明具体字段。
3. 加 `expected_index_version_for(visibility)`；两字段皆缺 → 回退全局 `index_version`。
4. `src/medical_rag/retrieval/contracts.py`：`GatePolicy` 新增 `COSINE_ABSOLUTE`。

**验收**：`pytest tests/unit/test_retrieval_metrics.py -q`；含"旧 artifact 行为不变"用例。

---

## T2 门禁逻辑恢复

文件：`src/medical_rag/retrieval/service.py`

1. 构造参数新增 `cosine_threshold` / `score_space`；实现 §4.2 优先级；互斥 fail-fast。
2. 在 `_retrieve_hybrid` 的 **ACL 过滤后、`self.ranker.fuse()` 之前**插入候选级门禁（intersect）。
3. 跳过条件：`not (vector_attempted and not vector_errored)` → 记 `gate_skipped_reason`，退回 top_k。
4. 诊断键：`gate_score_space`、`vector_gated_candidates`、`vector_candidates_gated_out`、`lexical_gated_out`、`gate_skipped_reason`。
5. **不要动 P1-6 已实现的 rerank 顺序**（`fuse → rerank → fused-score 门禁 → top_k`）；两条门禁路径互斥，见 spec §4.4。

**验收**：§6.1 单元测试全绿；I1/I5/I8 断言通过；现有 legacy 单路 `retrieve()` 路径行为不变。

---

## T3 标定分派恢复

文件：`src/medical_rag/retrieval/service.py`（`_retrieve_hybrid` 的 mismatch 检查段）

1. 比对从"单一 `artifact.index_version` 比全部候选"改为**按 `record.visibility` 分派**（T1 的 `expected_index_version_for`）。
2. `embedding_model` 保持全局校验。
3. mismatch 时仍 fail-closed（`calibration_artifact_mismatch`），并把**最多 5 条**不符记录写入 `calibration_mismatch_records`（只含 id/visibility/index_version/embedding_model，**不含正文**）。

**验收**：公共 `public-v1` + 私有 `private-v1` 构造用例 mismatch == 0；旧 artifact（全局比对）用例行为不变；I9 断言（无 PHI）通过。

---

## T4 组装与启动自检

文件：`src/medical_rag/api/composition.py`、`src/medical_rag/api/app.py`

1. 组装根无需新逻辑（既有 `load_calibration_artifact()` 自动生效），但需**测试**。
2. 新增启动自检：配置了 `CALIBRATION_ARTIFACT_PATH` 却未装配出门禁（例如非生产环境吞掉组装异常）→ 启动期报错，杜绝"配了 artifact 却静默退回 TOP_K"。
3. 复用评审 P1-10 的结论：非生产环境不应静默半启动。

**验收**：组装用例两条（带/不带新字段）；启动自检用例（配置未生效 → 抛错）。

---

## T5 测试重建

新增（等价于原 24 项）：`tests/unit/test_retrieval_service.py`（11 项，spec §6.1）、`tests/unit/test_retrieval_metrics.py`（6 项）、`tests/integration/`（组装 + 启动自检 + 分派，7 项）。

**验收**：`python -m pytest tests/ -q` 全绿并记录 passed/skipped 数（不要求复现原 754/3/0 —— 测试集本身也丢了，见 K2）。

---

## T6 端到端验收脚本

1. `scripts/retrieval_cosine_gate_eval.py`：调用**生产 `RetrievalService`**（非原型类），复现四项断言（拒答 2/2、指标逐值相等、零容忍 0、P95 ≤1.2×）。
2. `scripts/cosine_gate_enable_e2e.py`：**经正式 artifact 文件装配**（读 `config/calibration/cosine-gate-2026-09-26.json`），断言 ①–⑥。

**验收**：两脚本在本机可跑（需 OpenSearch + 索引 + 依赖环境）；输出落盘 `eval_out/`。

---

## T7 遗留项登记

- 更新 `docs/问题修复说明-2026-10-12.md`：追加门禁恢复条目、偏差、遗留。
- D1（`_safe_diagnostics` 哈希 `gate_policy`/`gate_score_space`）→ 单列待办，本次不修。
- 阶段 2（`space_type` 显式化 + 索引自省）、阶段 3（`top_k`、语料扩容、重标定）→ 指向原 spec §11，不在本计划内。

---

## 完成后必须回答的三个问题（对齐 FEATURE-WORKFLOW §5）

1. spec 是否批准、目标是否全实现、非目标是否确实没做？
2. 每个任务是否独立验收、全量测试通过数是多少？
3. 端到端真实链路是否有输出证据、九条安全不变量是否逐条断言？
