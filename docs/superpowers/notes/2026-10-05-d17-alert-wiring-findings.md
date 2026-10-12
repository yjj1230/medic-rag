# D17 告警接线：现状调研（先行结论）

> 日期：2026-10-05
> 触发：用户批准"接三条告警"（上一轮我列为优先级 1）。
> 结论：**spec §6.4 把 D17 划出仓库范围的假设，与本仓库现状不符**——
> 仓库内没有可被采集的告警出口，且关键字段被**两道**白名单挡掉。

---

## 1. 调研发现的三个事实

### 事实 1：仓库内**没有任何指标导出通道**

| 检查项 | 结果 |
|---|---|
| `prometheus` / `statsd` / `pushgateway` / `opentelemetry` | `grep src/` → **0 命中** |
| `/metrics` HTTP 端点 | **不存在**（`app.py` 只有 `/health`、`/health/ready`） |
| `MetricsRecorder.record_retrieval()` 的生产调用方 | **无**。仅测试与 `indexing/reconciliation.py` 使用 |
| 检索诊断的生产出口 | 仅 `POST /answers` 响应体的 `diagnostics` 字段 |

**含义**：spec §6.4:338 写"告警系统的接线…属运维配置，不在本仓库代码范围内"。
但运维**没有可接的东西**——没有端点、没有指标名、没有可采集格式。
该句把"定义告警条件"（已完成）与"提供可采集信号"（**未完成**）混为一谈。

### 事实 2：告警条件 1 的字段**不在 HTTP 出口白名单里**

`chat/orchestrator.py:37-60` 的 `_ANSWER_DIAGNOSTIC_KEYS` 是 HTTP 响应的唯一过滤点
（`_answer_diagnostics()`，在 line 293/440/518 三处出口调用）。现行白名单：

```
status, gate_policy, gate_threshold, rank_gate, gate_passed_candidates,
candidate_limit, lexical_candidates, vector_candidates, fused_candidates,
reranked_candidates, lexical_error, vector_error, rerank_error,
embedding_degraded, embedding_model, index_version, calibration_artifact,
calibration_artifact_mismatch, stage_elapsed_ms,
public_fallback, public_fallback_items, public_fallback_reason
```

**缺失的三个字段**：

| 字段 | 告警用途 | 现状 |
|---|---|---|
| `gate_skipped_reason` | **告警条件 1**（静默失去拒答） | ❌ 被白名单挡掉 |
| `gate_score_space` | 阈值语义（`l2_inv` vs 真余弦） | ❌ 被挡掉 |
| `vector_candidates_gated_out` | 门禁实际砍掉多少 | ❌ 被挡掉 |
| `lexical_gated_out` | 同上（词法侧） | ❌ 被挡掉 |

**这是 A1 修复的漏网之鱼**。A1（阶段 3A）只修了 `observability/metrics.py` 的
`_safe_diagnostics`（监控侧），但 HTTP 响应侧是**另一个独立的过滤函数**
（`orchestrator._answer_diagnostics`），二者白名单**各自维护、互不知晓**。

后果：门禁跳过时，运维从 `POST /answers` 响应里**看不到 `gate_skipped_reason`**，
只能看到 `vector_error: true`。而 `vector_error` 与"门禁被跳过"不是同一条件——
`vector_attempted=False`（embedding 未配置）时门禁也跳过，但 `vector_error` 为 false。

### 事实 3：两道白名单的存在本身是**结构性缺陷**

```
RetrievalService.diagnostics（约 25 键，含 PII 风险字段如 candidate_ids/ranks）
        │
        ├──→ MetricsRecorder._safe_diagnostics ──→ 哈希未知键（A1 已修 gate_* 三键）
        │        但：**无生产调用方**
        │
        └──→ ChatOrchestrator._answer_diagnostics ──→ 丢弃非白名单键（本次发现缺口）
                 唯一生产出口，但缺 gate_skipped_reason
```

同一份诊断数据、两个过滤实现、两套白名单、对同一字段给出**不同结论**。
新增一个诊断键时必须同时改两处，且**没有任何测试或机制强制这一点**。
这是 D1 同类缺陷的复发根源。

---

## 2. 与 spec 的关系：需要一次偏差上报

spec §6.4 的三条告警条件定义（判据本身）是**正确的、可用的**，无需改。
需要修正的是其边界声明：

| spec 原话 | 实测 | 处理 |
|---|---|---|
| "本次只定义告警条件与指标可读性" | 条件已定义；可读性**只完成一半**（metrics 侧 done，HTTP 侧未做） | 偏差上报 |
| "告警系统的接线…不在本仓库代码范围内" | 接线确实在仓库外，但**仓库必须先提供可采集出口** | spec 需补一节 |

因此本任务**不是纯运维**，包含仓库内代码改动。按仓库工作流需 spec + plan。

---

## 3. 建议方案（待评审）

### 3.1 最小必需（修正确性，不加新机制）

1. **HTTP 白名单补齐**：`_ANSWER_DIAGNOSTIC_KEYS` 增补 `gate_skipped_reason` /
   `gate_score_space` / `vector_candidates_gated_out` / `lexical_gated_out`。
   - 四者均为**有界枚举或整数**，无自由文本、无 PII，符合现有白名单"计数器/版本号"准则。
   - `gate_skipped_reason` 需给 `None` 值——白名单是 key 过滤，`None` 自然透传。
2. **两道白名单一致性护栏**：新增契约测试，断言
   `{A1 三个 gate 键} ⊆ _ANSWER_DIAGNOSTIC_KEYS`，且**未知键仍被丢弃**。
   这防止未来"改了 metrics 忘了 orchestrator"。
3. **HTTP 层端到端断言**：新增测试——构造门禁跳过的检索结果，走
   `ChatOrchestrator.answer()`，断言响应 `diagnostics` 含可读的
   `gate_skipped_reason == "vector_path_unavailable"`。
   （当前**零覆盖**，见事实 2。）

### 3.2 是否加 `/metrics` 端点（**需用户决策**）

| 方案 | 优点 | 代价 |
|---|---|---|
| **B1. 不加端点**，仅补齐响应字段 + 交付采集脚本 | 改动最小；无新依赖；符合"单机试点"现状 | 运维需自己从响应/log 采集；无标准指标格式 |
| **B2. 加 `/metrics`（Prometheus 文本格式）** | 标准可采集；一次到位 | 需新增计数器（当前 `MetricsRecorder` 是**每请求内存列表**，非进程级累计），涉及并发与生命周期设计；需 `prometheus_client` 依赖 |
| **B3. 加结构化日志出口** | 无新依赖；日志栈已有 | 需定义日志格式契约；log 聚合能力取决于运维 |

**我的建议**：先做 **3.1**（这是"门禁跳过了但你看不见"的正确性缺陷，必须修），
`/metrics`（3.2）作为独立任务在 3.1 验收后单独决策——理由是 `MetricsRecorder`
当前是请求作用域内存列表（`metrics.py:476`），要变成可采集的进程级累计指标
是一次**有并发语义的真正重构**，不应与"补一个白名单字段"捆绑。

---

## 4. 已核实的环境事实（供 plan 引用）

- 无 `/metrics`：`grep -n "prometheus|statsd|/metrics|opentelemetry"` → 0。
- `MetricsRecorder` 定义 `metrics.py:472`，`retrievals: list` 为**实例级内存**，
  `snapshot()` 返回 `MappingProxyType`，无累计/导出。
- `_answer_diagnostics` 定义 `orchestrator.py:73`，白名单常量 `:37-60`，
  调用点 `:293 / :440 / :518`。
- 服务层诊断键全集见 `service.py:394-417` + `:517-530`。
- 现有测试引用 `gate_skipped_reason` 的三处（`test_scheme_b_rollout.py:316`、
  `test_retrieval_service.py:886/905`、`test_safe_diagnostics_gate_keys.py`）
  **全部止于服务层/metrics 层**，无一触及 HTTP 响应。
