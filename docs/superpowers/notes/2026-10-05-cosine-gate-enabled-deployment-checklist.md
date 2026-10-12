# 余弦门禁已启用 —— 部署检查项与回滚手册（阶段 3B）

> 日期：2026-10-05
> 变更：`.env` 新增 `CALIBRATION_ARTIFACT_PATH=config/calibration/cosine-gate-2026-09-26.json`
> 效果：RAG 从「**从不拒答**」变为「**无关查询返回 `insufficient_evidence`**」。
> 批准链：spec `2026-09-28-cosine-gate-enablement-design.md` → plan（16 任务）→
> 阶段 3A（A1–A5 全绿）→ **用户指令"帮我接上"= 批准阶段 3B**。

---

## 1. 一行摘要

`.env` 里加了一行 `CALIBRATION_ARTIFACT_PATH`，把检索门禁从 `TOP_K` 切到
`COSINE_ABSOLUTE`（阈值 0.50，分数空间 `l2_inv`）。**回滚 = 注释这一行。**

---

## 2. 启用前证据（干跑，断言 ①–⑦ 全 PASS）

脚本：`scripts/cosine_gate_enable_e2e.py`（读**正式 artifact 文件**，不经 `.env`）
输出：`eval_out/cosine_gate_enable_e2e.json`

| 断言 | 内容 | 结果 |
|---|---|---|
| ① | 拒答用例 2/2 正确拒答，`reason=insufficient_evidence` | PASS |
| ② | 可答用例 Hit/Recall/MRR/nDCG 与无门禁 baseline **逐值相等** | PASS（Δ 全 0） |
| ③ | 零容忍安全项（越权/删除残留/复核泄漏）== 0 | PASS |
| ④ | 端到端 P95 劣化比 ≤ 1.2× | PASS（1.00–1.01×） |
| ⑤ | 混合作用域可答用例零返回数 == 0 | PASS |
| ⑥ | `calibration_artifact_mismatch` 用例数 == 0 | PASS |
| ⑦ | 门禁跳过原因经 `_safe_diagnostics` 后**可读**（D1 修复端到端验证） | PASS |

启用前后指标对照（干跑实测，18 用例）：

| 配置 | 命中 | 召回 | MRR | nDCG | 拒答 | 零容忍 | P95ms |
|---|---|---|---|---|---|---|---|
| baseline 无门禁 | 1.000 | 0.964 | 0.964 | 0.946 | 0/2 | 0 | 55.2 |
| cosine-gate-0.50 | 1.000 | 0.964 | 0.964 | 0.946 | **2/2** | 0 | 55.1 |

---

## 3. 启用后验证（真实 `.env` 装配，全 PASS）

| 检查项 | 结果 |
|---|---|
| `Settings.from_env()` 读到 `calibration_artifact_path` | ✅ `config/calibration/cosine-gate-2026-09-26.json` |
| `RetrievalService.gate_policy` | ✅ `cosine_absolute` |
| `cosine_threshold` / `gate_score_space` | ✅ `0.5` / `l2_inv` |
| 启动自检日志 | ✅ `INFO medical_rag.api.app cosine gate enabled: threshold=0.5 score_space=l2_inv public=public-v1 private=private-v1` |
| `GET /health/ready` | ✅ `200 {"status":"ready"}` |

### 真实检索行为对照（Opensearch 真实索引）

| 查询 | 回滚态（无门禁） | 启用态（门禁） |
|---|---|---|
| 今天天气怎么样适合去钓鱼吗 | 3 条无关 ❌ | **0 条 + 拒答** ✅ |
| 推荐几部好看的科幻电影 | 3 条无关 ❌ | **0 条 + 拒答** ✅ |
| 混凝土抗压强度等级怎么划分 | 3 条无关 ❌ | **0 条 + 拒答** ✅ |
| 家庭自测血压应该怎么测量 | 3 条相关 | 3 条相关（**未受损**）✅ |
| 血红蛋白偏低是什么意思 | 3 条 | **0 条 + 拒答** ✅（正确，见下） |

> **最后一行不是误判**：已核对真实语料——公共索引 5 条全部是高血压主题，
> **不含任何"血红蛋白/白细胞/参考范围"内容**；含血红蛋白的只有私有索引
> （患者检验报告）。该查询走 `include_public=True`，是**"库里确实没有"**，
> 拒答正是本次要修的行为。这正是 P0-2「零真实医学语料」的表现，属已知遗留项。

---

## 4. ⚠️ 三条告警条件（**尚未接线**，属运维配置 D17）

门禁生效后，以下三种状态必须被监控，目前**代码只提供了可读的诊断字段，没有接告警通道**：

| # | 条件 | 含义 | 严重度 |
|---|---|---|---|
| **1** | `gate_skipped_reason == "vector_path_unavailable"` 任意非零 | **静默失去拒答能力**——向量路径不可用时门禁整体跳过，系统退回"永不拒答"。这是最危险的状态：表面正常，实际回到了修之前的行为 | **高** |
| **2** | `gate_policy != "cosine_absolute"` | 生产环境装配退化——配了 artifact 却没生效。**正常情况下启动自检会直接拒绝启动**（fail-closed），此告警是自检之外的运行期兜底 | 中 |
| **3** | `calibration_artifact_mismatch` 为真 | 记录身份与 artifact 不符 → fail-closed → **检索全部返回空**，服务实质不可用 | 高 |

诊断字段均已通过 `_safe_diagnostics` 脱敏且**原样可读**（阶段 3A A1 修复；干跑断言 ⑦ 已验证）。

**接线建议**：从检索诊断（`RetrievalResult.diagnostics`）或 `/metrics` 出口采集上述三键，
接入现有监控栈；在接线完成前，**部署说明须显式告知运维这三条未自动告警**。

---

## 5. 回滚手册

**回滚 = 注释 `.env` 一行 + 重启**，不改任何代码：

```bash
# 编辑 .env，把这一行注掉：
# CALIBRATION_ARTIFACT_PATH=config/calibration/cosine-gate-2026-09-26.json
```

重启后门禁退回 `TOP_K`（即改动前行为），`gate_policy` 诊断变为 `top_k`。

回滚安全性已由测试钉死（`tests/integration/test_scheme_b_rollout.py`）：
- 机制层：路径为空 → `load_calibration_artifact()` 返回 `None` → 口径 `top_k`；
- 行为层：无候选被砍时，两态返回**逐值相同**的证据列表（id 顺序、分数、reason 全等）；
- 边界层：低分候选在回滚态**不再被砍**（恢复"全部返回"）。

**不需要**回滚的例外：artifact **文件**本身不要删。文件在、路径注掉 = 干净回滚；
文件删了再启用会 fail-closed 拒绝启动（这是设计意图，见 §4 第 3 条）。

---

## 6. 遗留与边界（显式记录）

1. **降级期永不拒答**：向量路径不可用时门禁跳过（有意设计，避免"每次查询都拒答"），
   代价是降级期恢复旧行为。↔ 告警条件 1。
2. **阈值 0.50 的语料依赖**：0.50 标定自**当前这份语料**（公共 5 条高血压样例 + 私有 7 条
   检验报告）。换语料后需按 spec §11 阶段 2 重新标定。
3. **P0-2 未修**：公共索引仍只有高血压样例（1 个源文件）；这是"血红蛋白类问题被拒答"
   的根因，属独立任务（换源文件，非重跑解析）。
4. **D17 未接线**：三条告警条件未接监控通道（见 §4）。
5. **评估空转未修（P0-4）**：`tests/eval/` 仍无真实 `retrieve` 调用；本次验收用的是
   `scripts/cosine_gate_enable_e2e.py`（真实链路），非 CI 内。

---

## 7. 本次变更文件清单

| 文件 | 变更 |
|---|---|
| `.env` | **+** `CALIBRATION_ARTIFACT_PATH`（含回滚注释）——唯一启用入口 |
| `config/calibration/cosine-gate-2026-09-26.json` | 新增（阶段 3A A2）：门禁参数的单一事实来源 |
| `src/medical_rag/observability/metrics.py` | `_safe_diagnostics` 放行 gate_policy/score_space/skipped_reason（A1） |
| `src/medical_rag/api/app.py` | lifespan 门禁自检，放在 `try/except` **之外**（A3） |
| `tests/unit/test_safe_diagnostics_gate_keys.py` | 新增 9 例（A1） |
| `tests/contract/test_calibration_artifact_file.py` | 新增 4 例（A2） |
| `tests/integration/test_app_lifespan.py` | 新增 6 例（A3） |
| `scripts/cosine_gate_enable_e2e.py` | 新增：启用前干跑（A4） |
| `tests/integration/test_scheme_b_rollout.py` | +4 例回滚不变量（A5） |
| 若干 `scripts/*.py` | ruff 清理死导入（无行为变化） |

**测试基线**：754 passed → **777 passed / 3 skipped / 0 failed**（+23 例，零回归）。
