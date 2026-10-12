# D17 告警信号可采集性修复 —— 规格说明

> 日期：2026-10-05
> 状态：**待批准**（按仓库 FEATURE-WORKFLOW：spec → plan → TDD）
> 关联：`docs/superpowers/specs/2026-09-28-cosine-gate-enablement-design.md` §6.4 / §9 D17
> 前置已验收：阶段 3A（A1–A5）+ 阶段 3B（`.env` 已启用，门禁生效）
> 调研依据：`docs/superpowers/notes/2026-10-05-d17-alert-wiring-findings.md`

---

## 1. 问题陈述

门禁已启用。告警条件 1（`gate_skipped_reason == "vector_path_unavailable"`）
是三者中**最危险**的一条——它意味着系统**静默失去拒答能力**，退回改动前行为。

但该字段**无法从任何生产出口读到**：

```
RetrievalService.diagnostics
  ├─→ MetricsRecorder._safe_diagnostics   ← A1 已修，但**无生产调用方**
  └─→ ChatOrchestrator._answer_diagnostics ← 唯一生产出口，**缺该键**
```

**根因**：仓库存在两道独立白名单，各自维护。A1 只修了无调用方的那道。
新增诊断键时没有任何机制强制两处同步——这是 D1 缺陷的**复发**。

**后果（可复现）**：门禁跳过时，`POST /answers` 响应里只有 `vector_error: true`。
而 `vector_error` 与"门禁被跳过"**不等价**：`embedding` 未配置时
（`vector_attempted=False`）门禁同样跳过，但 `vector_error` 为 `false`
（`service.py:507-530`）。据此告警必然**漏报**。

---

## 2. spec §6.4 的偏差（须显式记录）

| spec 原话（§6.4:337-339） | 实测 |
|---|---|
| "本次只定义告警条件与指标可读性" | 条件已定义；**可读性只完成一半**（metrics 侧 done，HTTP 侧未做） |
| "告警系统的接线…属运维配置，**不在本仓库代码范围内**" | 接线确在仓库外，但**仓库必须先提供可采集出口**；当前无出口 |

**处理**：判据定义不变（三条条件正确、可用）。本次补齐"仓库内可采集性"，
并在原 spec 增补一节记录该边界修正。这是**偏差上报**，非静默改口径。

---

## 3. 设计

### 3.1 HTTP 白名单补齐（唯一功能改动）

`chat/orchestrator.py:37` 的 `_ANSWER_DIAGNOSTIC_KEYS` 增补 4 个键：

| 键 | 类型 | 加入理由 |
|---|---|---|
| `gate_skipped_reason` | `str \| None`，有界枚举 | **告警条件 1 的判据**；取值已在 `_SAFE_GATE_SKIP_REASONS` 锁定 |
| `gate_score_space` | `str \| None`，有界枚举 | 阈值语义随它变（`l2_inv` vs 真余弦），不可读则阈值不可解释 |
| `vector_candidates_gated_out` | `int \| None` | 门禁实际砍掉多少——判"门禁是否真的在工作" |
| `lexical_gated_out` | `int \| None` | 同上（词法侧）；漏此项会让"纯关键词命中的不相关文档"不可见 |

**合规性**：四者均为有界枚举或整数，符合该白名单既有准则
（"只放行计数器/版本号/耗时，绝不透传候选文档 id"）。
不含自由文本、不含 PII。`None` 值由 key 过滤自然透传。

**不改**：`_SAFE_GATE_POLICIES` 等 metrics 侧已正确的部分；不动服务层诊断内容。

### 3.2 一致性护栏（防复发）

新增**契约测试**，锁定两道白名单在 gate 相关键上的一致性：

```python
# 断言：metrics 侧放行的 gate 三键，HTTP 侧也必须放行
assert _METRICS_GATE_KEYS <= set(_ANSWER_DIAGNOSTIC_KEYS)
```

其中 `_METRICS_GATE_KEYS` 从 `_SAFE_GATE_POLICIES` / `_SAFE_SCORE_SPACES` /
`_SAFE_GATE_SKIP_REASONS` 对应的**诊断键名**推导（字面量，非动态）。

**关键约束**：该测试必须在"未来新增 gate 诊断键但只改一处"时**失败**。
因此断言方向是 `metrics ⊆ http`，并附一条**反向**检查说明——
HTTP 侧允许比 metrics 侧宽（如需 `gate_passed_candidates` 这类无枚举键），
但**每个 gate 前缀键都必须两侧可见**。

### 3.3 HTTP 层端到端断言（当前零覆盖）

新增测试：构造门禁跳过的检索结果 → 走 `ChatOrchestrator.answer()` →
断言**响应 `diagnostics`** 含可读的 `gate_skipped_reason`。

与既有测试的分工：
- `test_retrieval_service.py:886` —— 服务层（`.diagnostics`）
- `test_safe_diagnostics_gate_keys.py` —— metrics 层（`_safe_diagnostics`）
- **本次新增** —— HTTP/编排层（`AnswerResponse.diagnostics`）← 缺口

三条合起来才构成"字段从产生到可被运维看到"的完整链路。

---

## 4. 不在本次范围（显式排除）

| 项 | 理由 |
|---|---|
| `/metrics` 端点 | `MetricsRecorder` 是**请求级内存列表**（`metrics.py:476`），改进程级累计是**有并发语义的重构**，不应与"补白名单字段"捆绑。用户已决策本次不做 |
| 统一两道白名单为单一实现 | 触及模块耦合方向（`chat/` → `observability/`），需独立评估。本次用契约测试锁一致性替代 |
| 告警通道本身（Prometheus 规则/报警渠道） | 确属运维配置，在仓库外 |
| D12（`calibration_mismatch_records` 仍哈希） | 需逐字段脱敏逻辑，独立任务 |

---

## 5. 安全与合规不变量

| # | 不变量 | 断言方式 |
|---|---|---|
| H1 | `gate_skipped_reason` 在 HTTP 响应中**可读** | 端到端：构造跳过场景 → `response.diagnostics["gate_skipped_reason"] == "vector_path_unavailable"` |
| H2 | `None` 值**不被误转成哈希** | 正常（未跳过）时该键为 `None`，不是 `sha256:...` |
| H3 | 两道白名单 gate 键一致 | `metrics_gate_keys <= set(_ANSWER_DIAGNOSTIC_KEYS)` |
| H4 | **不扩展自由文本面** | 未知键仍被丢弃：`_answer_diagnostics({"note": "患者姓名张三"})` 不含 `note` |
| H5 | 候选文档 id **绝不外泄** | `_answer_diagnostics({"candidate_ids": [...]})` 不含该键（回归线，本次不得放宽） |
| H6 | 既有响应字段**逐字节不变** | 现有 `test_chat_orchestration.py` 全部用例不改一行且通过 |

---

## 6. 验收标准

1. `_ANSWER_DIAGNOSTIC_KEYS` 含上述 4 键；新增契约测试全绿。
2. HTTP 层端到端断言：门禁跳过 → 响应含可读 `gate_skipped_reason`。
3. H4/H5 负例绿（未知键与 `candidate_ids` 仍被丢弃）。
4. 全量测试 **零回归**（基线 777 passed / 3 skipped）。
5. `tests/integration/test_chat_orchestration.py` **一行未改**。
6. 交付：原 spec §6.4 增补边界修正说明；告警采集清单（3 条判据 + 对应键 + 触发条件）。

---

## 7. 风险

| 风险 | 评估 | 缓解 |
|---|---|---|
| 新增键泄漏敏感信息 | **低**：四者皆为有界枚举/整数，无自由文本 | H4/H5 负例测试 |
| 改动 `orchestrator.py` 影响 chat 主链路 | **低**：仅改一个常量元组的成员 | H6 + 全量回归；改动不触及控制流 |
| 契约测试过紧导致未来误报 | **中**：若未来新增合法 gate 键只改一处会失败 | 这正是设计意图（防复发）；失败信息需给出明确指引 |

---

## 8. 实施任务（供 plan 展开）

| # | 任务 | 类型 |
|---|---|---|
| W1 | 契约测试：`metrics_gate_keys ⊆ _ANSWER_DIAGNOSTIC_KEYS`（先红） | TDD 红 |
| W2 | HTTP 白名单增补 4 键（转绿） | 实现 |
| W3 | HTTP 层端到端断言：门禁跳过可读 | 新测试 |
| W4 | 负例：未知键、`candidate_ids` 仍被丢弃 | 新测试 |
| W5 | 全量回归 + ruff + 原 spec 增补 + 交付告警采集清单 | 收尾 |
