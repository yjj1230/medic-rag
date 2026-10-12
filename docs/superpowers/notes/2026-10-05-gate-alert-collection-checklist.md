# 门禁告警采集清单（D17 仓库侧交付）

> 日期：2026-10-05
> 关联：spec `2026-10-05-d17-alert-signal-availability-design.md`、
> plan `2026-10-05-d17-alert-signal-availability-implementation.md`（W5）、
> 原 spec `2026-09-28-cosine-gate-enablement-design.md` §6.4 / §6.4.1 / §9 D17、
> 部署手册 `2026-10-05-cosine-gate-enabled-deployment-checklist.md` §4。
> 目的：把「三条告警条件」变成**运维可直接采集的字段清单**。
> 边界：**本清单只定义"从哪采、采什么、什么值要报警"；监控规则与报警渠道属仓库外配置，本次不提供。**

---

## 0. 一句话前提（必读）

> **本仓库没有 `/metrics` 端点，也没有 Prometheus 导出。**
> `MetricsRecorder` 是**请求内**的内存列表（`observability/metrics.py:476`），
> 不跨请求累计、无生产调用方。把它改成进程级累加器属独立重构，**本次显式不做**。

因此**唯一可采集的生产出口**是：

| 项 | 值 |
|---|---|
| 路由 | `POST /v1/chat/answers`（`api/routes/chat.py:49`，前缀 `/v1/chat`） |
| 字段 | 响应体 `diagnostics`（`ChatAnswerResponse.diagnostics`，`chat.py:46`） |
| 同级字段 | `status`（`chat.py:37`）——**拒答发生在答案层，不只在诊断层** |
| 认证 | 需 `current_access_context`（正常业务凭据即可，无需特权） |

采集方式二选一（均为仓库外实现）：
1. **日志管道**：编排器不落 `diagnostics` 日志，若走日志需先加日志埋点 → 本次未加；
2. **响应体旁路**：在网关/服务网格/调用方侧记录 `diagnostics` → **推荐**，零代码改动。

> ⚠️ `diagnostics` 是**单请求快照**。三条告警条件的"非零"/"比例"判断
> **必须由采集侧做窗口聚合**，仓库内不存在任何累计计数器。

---

## 1. 字段可采性（已逐条验证）

`_ANSWER_DIAGNOSTIC_KEYS`（`chat/orchestrator.py:45-80`）是 key 过滤白名单：
**只保留键，不做值哈希**（与 metrics 侧不同）。以下键在 2026-10-05 补齐（W2），
并由 `tests/contract/test_diagnostic_whitelist_parity.py` 锁定与 metrics 侧一致。

| 键 | 来源（`retrieval/service.py`） | 未启用门禁时 | 启用门禁时 |
|---|---|---|---|
| `gate_policy` | `:401` = `self.gate_policy.value` | `"top_k"` | `"cosine_absolute"` |
| `gate_score_space` | `:410` = `self.gate_score_space` | `None` | `"l2_inv"` |
| `gate_threshold` | `:405-409` | `None` | `0.5` |
| `gate_skipped_reason` | `:416` 初始 `None`，`:530` 置 `"vector_path_unavailable"` | `None` | `None` 或 `"vector_path_unavailable"` |
| `vector_candidates_gated_out` | `:414` 初始 `None`，`:518` 置 int | `None` | int（≥0） |
| `lexical_gated_out` | `:415` 初始 `None`，`:519` 置 int | `None` | int（≥0） |
| `calibration_artifact_mismatch` | 来自 `RetrievalResult.diagnostics`（`service.py:270`） | 缺席 | `True` 时存在 |

**取值域（有界，可直接做等值判断）**：
- `gate_policy` ∈ `{calibrated, explicit_threshold, rank_gate, top_k, cosine_absolute}`（`retrieval/contracts.py:39-43`）
- `gate_score_space` ∈ `{l2_inv, cosinesimil}`
- `gate_skipped_reason` ∈ `{vector_path_unavailable}`（仅此一个成员）

---

## 2. 三条告警条件（照采即可）

### 条件 1 —— 门禁被跳过（**最高优先级**）

| 项 | 内容 |
|---|---|
| 判据 | `diagnostics.gate_skipped_reason == "vector_path_unavailable"` |
| 集合来源 | 逐请求响应体 |
| 触发条件 | 窗口内**任意 1 次**出现即触发 |
| 建议阈值 | 5 分钟窗口 ≥1 次 → **page**（不是 warning） |
| 含义 | 向量路径未实际运行 → 门禁整体跳过 → **系统退回"永不拒答"**。表面健康，实际等于回到修复前的行为——这是本次改动**最容易静默失效**的地方 |
| 误判排除 | **不需要**看 `vector_error`。二者**不等价**：embedding 未配置时门禁同样跳过，但 `vector_error` 为 `false`（`service.py:522-530` 注释）。**只看 `gate_skipped_reason`** |
| 配套信号 | `vector_error` / `lexical_error` / `embedding_degraded` 可作根因定位，但**不作触发条件** |

### 条件 2 —— 生产环境口径退化

| 项 | 内容 |
|---|---|
| 判据 | `diagnostics.gate_policy != "cosine_absolute"` |
| 触发条件 | 生产环境窗口内**任意 1 次**不相等 |
| 建议阈值 | 任意 1 次 → **告警** |
| 含义 | 配了 artifact 却没生效。**正常情况下启动自检已 fail-closed 拒绝启动**（`api/app.py` lifespan 自检），故此条件是自检之外的**运行期兜底**——出现即说明有自检覆盖不到的路径 |
| 备注 | 该值是构造期确定的常量；同一实例内不应变化。一旦非 `cosine_absolute`，该实例**所有**请求的门禁语义都不可信 |

### 条件 3 —— artifact 身份不符

| 项 | 内容 |
|---|---|
| 判据 | `diagnostics.calibration_artifact_mismatch` 为真（存在且 `== True`） |
| 触发条件 | 窗口内**任意 1 次**为真 |
| 建议阈值 | 任意 1 次 → **page** |
| 含义 | 记录身份与 artifact 不符 → fail-closed → `reason=calibration_artifact_mismatch` → **检索全部返回空，服务实质不可用** |
| 定位辅助 | 同一 `diagnostics` 内的 `calibration_artifact_reason`（如 `hybrid_candidate_seams_required`，`service.py:271`）**不在 HTTP 白名单**，需从服务层日志取 |

---

## 3. 建议补充信号（**我的建议，非 spec 要求**）

以下信号不是 spec §6.4 的三条，但采集侧几乎零成本，能显著缩短排障时间：

| 信号 | 用法 | 注意 |
|---|---|---|
| `status == "insufficient_evidence"` | 拒答**比例**。突增到 ~100% 或长期恒 0 都异常 | **单次拒答是正确行为，不可告警**。恒 0 → 门禁可能失效（与条件 1 互证）；恒 100% → 阈值过严/语料缺失（P0-2） |
| `vector_candidates_gated_out` / `lexical_gated_out` | `gate_policy=="cosine_absolute"` 且 `gate_skipped_reason is None` 时**必须为 int**；若仍为 `None` → 门禁代码路径异常 | `None` 是"门禁未运行"的另一个佐证 |
| `gate_score_space == "l2_inv"` | 与 artifact 声明比对 | 换 `space_type` 或换引擎会让同一阈值含义改变 |

---

## 4. 采集侧实现要点（避坑）

1. **不要在仓库内加累计计数器**（本次已明确排除）——需求是"可采集"，不是"仓库内自聚合"。
2. **窗口聚合放采集侧**：条件 1/2/3 的语义都是"任意 1 次"，用采集侧的滑动窗口即可。
3. **不要依赖 `_safe_diagnostics`（metrics 侧）**：它当前**无生产调用方**，且会
   **哈希未知值**（`metrics.py:577-578` 的 `else` 分支）——若误用，`gate_skipped_reason`
   的未知值会变成 `sha256:...` 而漏报。**HTTP 侧是 key 过滤、值原样透传，这才是采集源。**
   （该差异由 `tests/contract/test_diagnostic_whitelist_parity.py` 守护。）
4. **隐私面未放宽**：`candidate_ids` / `ranks` / 任意自由文本仍被丢弃（H4/H5，
   见 `tests/integration/test_gate_skip_observable_via_http.py`）。采集侧拿不到患者相关字段，
   这正是设计意图。

---

## 5. 交付状态与遗留

| 项 | 状态 |
|---|---|
| 三个判据键在 HTTP 响应可见 | ✅ 2026-10-05 修复（W2，白名单 22→26 键） |
| 两白名单一致性契约测试 | ✅ `tests/contract/test_diagnostic_whitelist_parity.py`（4 例） |
| HTTP 端到端可读性测试 | ✅ `tests/integration/test_gate_skip_observable_via_http.py`（5 例） |
| 字段清单（本文件） | ✅ 本文件 |
| `/metrics` 端点 / Prometheus 导出 | ❌ **不在本次范围**（独立重构） |
| 监控规则（PromQL 等） | ❌ **仓库外**，待运维按 §2 落地 |
| 报警渠道（告警接收人/分组） | ❌ **仓库外**，待运维配置 |

> **落地顺序建议**：先接条件 1（静默失去拒答，最危险）→ 条件 3（服务实质不可用）
> → 条件 2（有启动自检兜底，优先级最低）。
