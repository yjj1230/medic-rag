# 引用校验抖动 + 降级诊断丢弃 —— 缺陷规格（spec）

- **日期**：2026-10-08
- **来源**：E2E 测试 B/D/F 组复跑（`docs/e2e_test_plan_rag.md`），真实网关实例
- **状态**：**待批准**（遵循 `FEATURE-WORKFLOW.md`：spec + plan before code）
- **关联**：
  - `docs/chat链路启动诊断报告-2026-09-29.md`（旧根因②：引用校验不兼容，三个子缺陷 a/b/c）
  - `docs/superpowers/specs/2026-10-05-chat-e2e-availability-design.md`（G1–G6，已实施 ②a/②b/②c）
  - `docs/superpowers/notes/2026-10-05-p0-5-reproduction-and-rootcause.md`

---

## 1. 现象

同一句**库内**问题，在完全相同的患者/会话/配置下反复提问，结果在
`answered` 与 `degraded` 之间**间歇翻转**：

| 探针 | N | answered | degraded | 抖动率 | 延迟中位数 |
|---|---|---|---|---|---|
| `agent_lab/probe_degraded_cause.py` | 5 | 2 | 3 | 60% | ~43s |
| `agent_lab/probe_citation_flake.py` | 8 | 5 | 3 | **37.5%** | **49.5s**（37.9~72.7） |
| **合计** | **13** | **7** | **6** | **≈46%** | — |

- 问题：`高血压患者在生活方式上有哪些注意事项？`
- 失败轮次响应：`status="degraded"`、`citations=[]`、
  `safety_notice="回答未通过医学证据和引用校验，因此未向患者展示。请改写问题或咨询医生。"`
- **库内问题可用性 ≈ 54%**（7/13，Wilson 95% CI **0.291~0.768**；n<30 故只报区间、不判达标），
  点估计远低于「功能验收比例 ≥80%」总则。
- 逐轮 `text_head` 标题层级/列表形态各异（`## 标题` / `**饮食方面：**` / `### 饮食` …），
  即"模型输出形态逐轮变化"的直接证据。

### 1.1 关键判别（已实测，排除会话缺陷）

探针每轮都记录 `diagnostics.answer_path`：

- **失败轮 `answer_path="rag"`** → 检索路径完整跑通，模型**已返回**文本，
  只是**卡在引用校验**。会话/园环境无问题。
- 早期误判为"多轮会话不续用（D-01）/ 会话隔离失效（D-04）/ 历史回放缺条（D-02）"，
  **均系该抖动的下游假象**：失败轮走 `UnsupportedClaim` 分支，**不落库、不返回 session_id**，
  故 D-01 `same_session=false`、D-04 `session_a=null`、D-02 只有 2 条消息。
  → **D 组 4 例中 D-01/D-02/D-04 的 FAIL 全部由本缺陷导致，非独立会话缺陷。**

### 1.2 失败出口定位（源码）

`src/medical_rag/chat/orchestrator.py:830-839`：

```python
try:
    validated = self.citations.validate(
        DraftAnswer(text=model_response.text, cited_evidence=cited_ids),
        evidence_for_model,
    )
except UnsupportedClaim:
    notice = "回答未通过医学证据和引用校验，因此未向患者展示。请改写问题或咨询医生。"
    return self._rag_degraded(
        notice, trace_id=model_response.trace_id, title="无法安全生成回答"
    )
```

`CitationValidator.validate()` 逐句要求"每个医学断言句**句末**必须锚定 `[id]`"，
以及段落覆盖（≥30 字符段落必须含 `[id]`）（`chat/citations.py:333-356, 269-284`）。

---

## 2. 根因

**根因 A（主因，可用性缺陷）：引用校验对真实模型输出形态过度敏感。**

- 校验规则是**确定性**的（同输入必同判），但**模型每次输出的段落结构/句末锚点位置不同**，
  因此"同一问题"触发的校验结果**抖动**。
- 已知此前（2026-09-29）已修 ②a/②b/②c 三类子缺陷；本次实测表明**仍有残留触发面**
  （新样本：带 `## 标题` 的多段式回答、列举引导句、句末标点后接换行等形态变化）。
- 医学安全上，这属于**假阳性**（正确答案被拒），代价是患者拿不到本可回答的问题答案。
- **临床影响**：库内问题的可用性 **< 100%**（实测 3/8 ~ 2/5 抖动），
  与 `2026-10-05` spec 的 G-目标（"产出可用回答，而非稳定 degraded"）**同一命题的残留**。

**根因 B（可观测性缺陷，独立且确定）：`rag` 路径的降级出口集体丢弃检索诊断。**

- `orchestrator.py` 的 `_rag_degraded(...)` **四处**出口中，**只有一处**传了 `diagnostics=`：
  | 行 | 出口 | 是否传 diagnostics |
  |---|---|---|
  | 796 | 检索不可用 | ✅ 传 |
  | 803 | 处理超时（预算不足） | ❌ 漏 |
  | 827 | 模型不可用（ModelError/Timeout） | ❌ 漏 |
  | 837 | **引用校验失败（UnsupportedClaim）** | ❌ 漏 |
- 检索诊断在 `orchestrator.py:756` 早已算出并存在于局部变量 `diagnostics`，
  上述三处却**未传入** → 失败轮 `gate_policy / gate_score_space / stage_elapsed_ms /
  embedding_degraded / vector_error ...` **在响应体里全部为 null**
  （实测：`eval_out/probe_degraded.json` 三个失败轮 `gate_policy=null`）。
- 直接连带：**F-03 FAIL**（断言 `diagnostics.gate_policy` 非空）实为**本缺陷**所致，
  而非门禁本身失效。运维侧无法区分"门禁放行但校验失败"与"门禁未工作"。
- 违背 `_ANSWER_DIAGNOSTIC_KEYS` 的设计意图（`orchestrator.py:45-49` 注释：**两侧必须同时可见**，
  否则告警采不到判据）。

---

## 3. 目标与非目标

### 3.1 目标

| # | 目标 | 可验证判据 |
|---|---|---|
| G1 | 库内问题的引用校验不再"同问异果" | N≥8 轮同问，`degraded` 占比 < 阈值（建议 ≤10%）；具体阈值见 §4 决策 |
| G2 | 失败时**保留检索诊断** | `UnsupportedClaim` 分支的响应 `diagnostics` 含 `gate_policy` 等（F-03 转 PASS） |
| G3 | 失败可**归因**（不再只有统一文案） | 降级响应新增 `diagnostics.citation_fail_reason`（有界枚举，加入白名单） |
| G4 | **不放松任何医学安全约束** | 反例必须仍然失败：幻觉 ID / 数值不在被引证据 / 诊断性结论 / 段落无引用 |

### 3.2 非目标

- ❌ 不引入"语义一致性命中"校验（属 P0-4 评估体系职责）
- ❌ 不放宽 `_check_numeric_consistency`、`_check_high_risk_claims`
- ❌ 不为通过测试而关闭校验（fail-closed 是既定安全姿态）

---

## 4. 修复方向（须决策后定稿，不预判实现）

### 根因 B：**低成本、零安全风险，建议直接实施**

在 `orchestrator.py` 的三处漏传出口（L803 超时 / L827 模型不可用 / L837 引用校验失败）
统一补 `diagnostics=diagnostics`；并为 `UnsupportedClaim` 补一个 `reason_code`
（异常携带原因字符串或新增子类）。`citation_fail_reason` 需加入 `_ANSWER_DIAGNOSTIC_KEYS` 白名单，
**并在 metrics 侧 `_safe_diagnostics` 注册为有界枚举**（否则会被哈希掉，见
`tests/contract/test_diagnostic_whitelist_parity.py` 的契约）。

### 根因 A：三选一（需你拍板）

| 方向 | 做法 | 优点 | 风险 |
|---|---|---|---|
| **A1 规则增强** | 继续按实测失败样本补豁免/归一（列举引导句、标题+正文连写、`；` 并列等） | 直接对标 2026-10-05 路线，风险最低 | 治标；总会有新形态 |
| **A2 校验输出回灌重试** | 校验失败时把 `citation_fail_reason` 回灌模型，要求其**只修引用标记**后重试一次（限 1 次，计入总预算） | 治本；保持句级约束不放宽 | 增加一次模型调用（延迟/费用）；须防重试放大 |
| **A3 降级而非拦截** | 校验失败时返回**已通过校验的部分段落**（若无则 insufficient_evidence），而非整体 degraded | 部分可用性 | 涉及医学安全边界，须 spec 明确"哪些段落可单独放行" |

> **倾向**：B 直接做；A 先做 A1（低风险收敛抖动）+ 加 `citation_fail_reason` 观测，
> 用一周真实流量决定是否需要 A2。**A3 涉及安全边界，须单独评审，暂不启动。**

---

## 5. 验收标准（定稿后）

1. `probe_citation_flake.py`：N=8 同问，`degraded` 占比达 §4 决策阈值。
2. 失败轮响应 `diagnostics.gate_policy` 非空（F-03 PASS）。
3. 设计 §5 负样本（幻觉 ID / 数值不符 / 诊断结论 / 无引用段落）**逐条仍必须 UnsupportedClaim**。
4. 全量 pytest 无新增失败（既有基线 20 failed / 9 errors 例外）。

---

## 6. 与现有测试用例的映射

| 用例 | 原判定 | 修正后判定 | 依据 |
|---|---|---|---|
| D-01 | FAIL | **FAIL（归因于本缺陷，非会话缺陷）** | 失败轮 `answer_path=rag`，会话机制本身正常 |
| D-02 | FAIL | **同上** | 失败轮不落库 → 仅 2 条消息 |
| D-04 | FAIL | **同上**（`session_b=null` 另属 insufficient_evidence 设计预期） | 失败轮不返回 session_id |
| F-03 | FAIL | **FAIL → 根因 B 修复后应 PASS** | 诊断被 `UnsupportedClaim` 分支吞掉 |
| B-01 | PASS | PASS | 校验通过轮次链路正常 |
| F-01/F-02 | PASS | PASS | 拒答路径不经引用校验 |
