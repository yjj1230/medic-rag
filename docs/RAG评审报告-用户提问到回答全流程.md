# 医疗 RAG「用户提问 → Agent 回答」全流程专家评审报告

> 评审对象：`C:\Users\lyx\Desktop\RAG`（medical_rag 患者端医疗问答系统）
> 评审方式：通读全部核心模块源码（编排 / 检索 / 安全 / 引用 / 查询 / 领域 / 索引 / OpenSearch 适配器），逐环节核查接线。
> 评审日期：2026-09-09
> 前置说明：本节全文基于**代码级**核查，非仅设计文档。

---

## 1. 实际链路还原（与源码一一对应）

```
POST /v1/chat/answers                      api/routes/chat.py
 └─ ChatOrchestrator.answer()               chat/orchestrator.py
    ├─ 1. safety.classify(question)         原始问题安全分流（safety/service.py + rules.py）
    ├─ 2. 会话归属校验 + 加载历史           _resolve_session / _load_history
    ├─ 3. 是否有私有报告                     _has_private_documents
    ├─ 4. query_pipeline.process()          Normalize → IntentRouter → ContextCompletion（query/pipeline.py）
    ├─ 5. 改写后再安全分类                   processed.text 二次 safety.classify
    ├─ 6. SearchQuery 组包
    ├─ 7. retriever.retrieve()              retrieval/service.py
    │     ├─ consent 校验                   ConsentService.require_active_consent
    │     ├─ embedding 生成                 失败降级纯词法
    │     ├─ 词法 + 向量 并行检索            ThreadPoolExecutor（2 worker）
    │     ├─ 权限过滤                        _is_authorized + _has_citation
    │     ├─ RRF 融合                        ranking.HybridRanker.fuse
    │     ├─ 阈值/排名门控                   fused_score_threshold / rank_gate
    │     └─ Reranker 重排                   reranker.rank
    ├─ 8. _expand_evidence()                parent/child 兄弟块扩展（retrieval/expansion.py）
    ├─ 9. 时间预算检查                      _MIN_MODEL_BUDGET_SECONDS（10s）
    ├─ 10. build_chat_request() + 模型调用  chat/prompts.py / ModelGateway
    ├─ 11. CitationValidator.validate()     chat/citations.py（高风险断言+引用存在+段落覆盖）
    └─ 12. _persist_answer()                会话持久化（幂等 client_request_id）
```

整体架构**非常扎实**：权限先行、fail-closed、降级路径完整、引用强制校验、审计贯穿。以下按环节给出问题与建议。

---

## 2. 逐环节专业评审

### 2.1 环节 1 — 问题安全分流
**文件**：`safety/service.py`、`safety/rules.py`

#### 现状代码核查
- 三个等级：`URGENT`（立即急症提示，短路检索与生成）、`MEDICATION_CHANGE`（拒绝用药）、`DIAGNOSIS_REQUEST`（拒绝诊断）、其余 `ALLOW_RETRIEVAL`。
- 规则全部为 `substring in casefold(text)` 的**纯字符串包含匹配**。
- 词表（`rules.py`）：
  - `URGENT_PATTERNS`：胸痛、胸部疼痛、胸口疼、呼吸困难、喘不上气、意识丧失、大出血、严重过敏、抽搐、中风、自杀、昏迷等 16 项。
  - `MEDICATION_CHANGE_PATTERNS`：加倍、加量、减量、停药、换药、改药等。
  - `DIAGNOSIS_PATTERNS`：是不是癌、能确诊吗、帮我诊断、我是不是得了等。

#### ✔ 做得好的
- 急症直接短路，不等检索与 LLM，符合设计 §11「优先显示就医提示，不等待复杂检索」。
- 改写后**二次分类**（orchestrator L290），堵住「问题正常、改写后变高危」的绕过。

#### ⚠ 专业问题
| 严重度 | 问题 | 说明 |
|---|---|---|
| 🔴 高 | **纯包含匹配，无法处理否定 / 条件 / 转述** | 例：`医生说我这不是癌症吧`→命中 `DIAGNOSIS_PATTERNS` 的「是癌」子串被误拦；`我能不能停药`的否定式医学问答被误伤。 |
| 🔴 高 | **急症词表可能误伤一般知识问询** | 例：`我在网上看到有人说胸痛是怎么回事`→命中「胸痛」，患者只是问一般知识却被当成急症直接短路。词表未做「陈述主体 / 是否描述自身症状」区分。 |
| 🟠 中 | **用药/诊断问题是「直接拒答」而非引导到安全科普** | 设计上可降级为「仅检索公共知识 + 强化免责 + 建议就医」，而非一律挡死，可提升可用性。 |

**建议**：规则仅作第一道快速闸；引入 **LLM 语义/否定/情感辅助分类**（`intent_llm_classify_enabled` 目前默认关闭，详见 §4）；急症词表配套「描述自身症状 vs 一般知识」判断。

---

### 2.2 环节 2 — 查询预处理流水线
**文件**：`query/pipeline.py`、`query/normalize.py`、`query/intent.py`、`query/intent_rules.py`、`query/context_completion.py`、`query/llm_rewrite.py`

#### ✔ 做得好的
- `Normalize → Intent → ContextComplete` 分层清晰，`rewrite_log` 全程可审计。
- `derive_strategy` 根据意图 + 是否有私有报告决定公私检索范围（REPORT_EXPLANATION 但无报告时降级纯公共，合理）。

#### ⚠ 专业问题
| 严重度 | 问题 | 说明 |
|---|---|---|
| 🔴 高 | **意图规则召回可能连带检索范围错误** | 患者问「我**报告里**血糖高**正常吗**」（有报告上下文应走 REPORT_EXPLANATION），但 `_LAB` 的「正常吗/高」命中数更高，`PRIORITY` 权重下可能误判为 LAB_INDICATOR → 混合检索，可能把患者私有报告也拉进来。需评估集验证（设计 §13 已有要求）。 |
| 🟠 中 | **`ContextCompletionStep` 锚点实体为硬编码枚举** | `_ANCHOR_ENTITY` 仅覆盖 `血红蛋白|白细胞|...|Hb|WBC` 等十几种。患者说「我**上次那个结果**降了吗」→代词命中但锚点提取不到，只记 `needs_llm`；而 LLM 改写默认关闭 → 指代丢失，检索质量直接下降。 |
| 🟠 中 | **同义词/缩写展开未实际启用** | `SYNONYM_EXPANSIONS` 注释明确「预留接口，不在文本里展开」，检索召回没有用上；`llm_rewrite.py` 默认 `enabled=False`（config `query_llm_rewrite_enabled=False`）。 |

**建议**：意图路由与上下文补全纳入质检集评估；考虑**默认开启受控 LLM 改写**（`enabled=True` + 保守策略）。

---

### 2.3 环节 3 — 检索服务
**文件**：`retrieval/service.py`、`retrieval/ranking.py`、`retrieval/contracts.py`

#### ✔ 做得好的（全链路最强的一环）
- **权限先行**：`_is_authorized` 在融合/重排**之前**过滤（含 deleted / inactive / in-eligible + tenant+patient 双校验），完全符合设计 §6.5。
- **混合检索 fail-closed**：calibration artifact 存在但 index 缺 `search_lexical/search_vector` 时直接返回空，而非返回不可证明来源的记录。
- **embedding 降级纯词法**（而非整体失败）；词法/向量**并行**（2 worker）压缩时延。
- **RRF 融合 + 阈值门控 + Reranker 重排**，`diagnostics` 非常完整，可观测性一流。
- `HybridRanker.fuse`：基于 rank（而非原始分）融合，`calibrated_score` 归一化到 0..1，跨候选集规模阈值可比，**math 上正确**。

#### ⚠ 专业问题
| 严重度 | 问题 | 说明 |
|---|---|---|
| 🔴 高 | **consent 缺失时静默降级、无原因提示** | Service L164-169：consent 校验仅在 `include_private` 时进行，且缺失只 `private_allowed=False`。患者以为能查自己的报告，实际只检索了公共知识，无任何提示。建议透传 `reason=consent_missing` 或 diagnostics。 |
| 🟠 中 | **Reranker 异常只打 `rerank_error` 标记，无降级/告警** | L402-413：重排失效时静默使用融合顺序，医学场景需有可观测告警。 |
| 🟠 中 | **`rerank_limit` / `rank_gate` / `fused_score_threshold` 相互作用的优先级未文档化** | 同时设置时先阈值截断再重排截断，两个门控含义不同，需明确优先级。 |
| 🟡 低 | **`ThreadPoolExecutor` 单例无显式 shutdown** | 长生命周期单例持有线程池，优雅关闭时可能泄漏线程。`min_score` 参数已 deprecated 但未移除。 |

---

### 2.4 环节 4 — 父→子上下文扩展（Task 9）
**文件**：`retrieval/expansion.py`

#### ✔ 做得好的
- Token 预算只约束**新增兄弟块**，从不裁剪已选命中。
- ACL 边界通过 `ScopedSiblingLookup` 严格带上 tenant/patient，兄弟扩展不跨患者。
- 找不到 `search_by_parent` seam 时 warn 降级而非整体失败。

#### 🔴 接线核查结论（重点）
expansion.py 的 docstring 自述：「the OpenSearch adapter is deferred — that store lives on the parallel session; production wiring only needs an object exposing `search_by_parent`」（第 11-13 行）。

我核查了 `retrieval/opensearch_store.py` 全文，**确认**：

1. **`OpenSearchStore` 没有实现 `search_by_parent` 方法**（类中只有 `search` / `search_lexical` / `search_vector` / `_visibility_filter` / `_candidate_search` / `_record_from_hit`）。
2. **`_record_from_hit` 没有读取 `parent_block_id` 字段**——即 OpenSearch 返回的 SearchRecord 里 `parent_block_id` 恒为 `None`（SearchRecord 构造时未传该字段）。

再核查 `api/composition.py`：

3. **`build_chat_orchestrator`（L112-119）构造 `ChatOrchestrator` 时未传 `expansion_index`**，采用默认值 `None`。

**结论**：在真实生产接线（`composition.py` + `OpenSearchStore`）下，**父→子上下文扩展完全不会生效**——`_expand_evidence` 因 `expansion_index is None` 直接透传（orchestrator L243），Task 9 仅在测试（`test_parent_child_retrieval.py` 用 InMemorySearchIndex）中生效。此为「纸上扩展」，**上线前必须接线**：
- 给 `OpenSearchStore` 增加 `search_by_parent(parent_block_id, *, limit, tenant_id, patient_id, include_public, include_private)`。
- `_record_from_hit` 透传 `parent_block_id`（mapping 中需有该 keyword 字段，当前 `ensure_mapping` 未含）。
- `composition.py` 把 `expansion_index=` 传入 `ChatOrchestrator`。

---

### 2.5 环节 5 — 证据组装 + 提示构建
**文件**：`chat/prompts.py`

#### ✔ 做得好的
- system prompt 明确「只能根据证据回答、区分报告事实与公共知识、不诊断不开药、句末带 [evidence-id]」，直接对接引用校验。

#### ⚠ 专业问题
| 严重度 | 问题 | 说明 |
|---|---|---|
| 🟠 中 | **私有/公共证据在模型输入混排，未区分来源与可信度** | `ChatRequest.evidence` 平铺所有 Evidence 文本，没有区分哪些是患者客观事实、哪些是一般知识。设计 §8 明确要求「区分报告事实、一般医学含义、下一步建议」，当前提示结构未给模型显式分层输入，容易让模型混淆病历事实与科普知识。 |

**建议**：给模型输入做结构化分层（`public_evidence` vs `private_evidence` + 来源标注），并放在 prompt 的不同段落。

---

### 2.6 环节 6 — 引用校验
**文件**：`chat/citations.py`

#### ✔ 做得好的
- **三层把关**：高风险断言正则 + 引用必须存在且可定位（source_id/title/version/location 齐全）+ 段落覆盖检查（≥30 字符无引用即拒）。这是全系统防幻觉的关键。
- 未通过校验的答案**向患者展示前被拦截**（orchestrator L339-347），fail-closed。

#### ⚠ 专业问题
| 严重度 | 问题 | 说明 |
|---|---|---|
| 🔴 高 | **高风险断言正则为纯正则，误杀安全解释** | `_high_risk_claims` 对 `"确诊为"`、`"is cancer"` 等子串直接拦。例：回答「**不能**仅凭此确诊为癌症」→命中「确诊为」被 UnsupportedClaim 拦截。医学回答防过头会降低可用性（误杀大量合法的解释性回答）。 |
| 🟠 中 | **引用校验只验证"引用了存在且可定位的证据"，不验证"证据文本是否真的支持该断言"** | 这是事实一致性（含金量）缺口：引用了正确的来源但内容无关也会通过。设计 §13 要求「引用与原文一致率 ≥ 95%」，当前代码只能保证可定位，**无法证明一致性**。 |
| 🟡 低 | 段落覆盖检查以 30 字符阈值 + `[ ]` 标记严格绑定 | 若 LLM 用其他引用格式则频繁 UnsupportedClaim，需对齐契约。 |

---

### 2.7 环节 7 — 编排持久化与降级
**文件**：`chat/orchestrator.py`

#### ✔ 做得好的
- session 归属越权直接 `AccessDenied`（视为安全事件）。
- 持久化幂等（client_request_id）；持久化失败不阻断已校验答案投递；超时预算保护防半截请求；降级文案清晰。

#### ⚠ 专业问题
| 严重度 | 问题 | 说明 |
|---|---|---|
| 🟠 中 | **`safety_notice` 在"安全拦截"与"服务降级"两条路径复用同一字段** | `_safe_response` 与 `_degraded` 的 safety_notice 语义不同（一个是拒答、一个是故障），前端难以区分「被拒答」还是「系统崩溃」。`status` 已有 `answered/safe_template/insufficient_evidence/degraded`，建议明确分离。 |
| 🟡 低 | `safety.classify` 首问+改写后各调一次，主线程串行 | 规则简单尚可；若未来换 LLM 分类会增加延迟/成本，需计入预算。 |

---

## 3. 横切问题（最重要）

1. **【最高】全链路多个核心闸门共用"关键词 + 正则"体系**：安全分流、引用断言、意图分类全是词法驱动。医学长尾明显，语义/否定/转述覆盖能力不足。建议统一改为「**规则塑形 + LLM 语义复核**」双层方案，并在上线前对规则词表做红队测试（设计 §13 已要求，尚未见强落地）。
2. **【高】`_expand_evidence` 的 OpenSearch seam 生产未接线**（见 §2.4），Task 9「父→子扩展」上线前形同虚设。
3. **【中】`answer()` 对 retrieval 只做了「有空/无空」两分支，未把 `diagnostics` 透传**（consent 降级原因、融合分、rerank 状态、calibration 状态）给前端/监控。建议关键诊断写入 AnswerResponse 或 trace。
4. **【中】私有/公共证据在模型输入混排**（见 §2.5）。
5. **【低】`min_score` deprecated 参数未清理；`ThreadPoolExecutor` 未优雅关闭。**

---

## 4. 配置默认值核查（`core/config.py`）

```
query_pipeline_history_size   = 4
query_llm_rewrite_enabled     = False   # LLM 改写默认关闭
intent_llm_classify_enabled   = False   # LLM 意图分类默认关闭
embedding_model               = text-embedding-3-small（service 默认）
```

**影响**：设计文档强调的「受控 LLM 辅助」在默认配置下均未启用，系统在纯规则模式下运行，召回与安全判断的语义能力受限。

---

## 5. 评审结论与整改优先级

| 优先级 | 事项 | 环节 |
|---|---|---|
| 🔴 高 | **接线 OpenSearch `search_by_parent`**：加方法 + `_record_from_hit` 透传 parent_block_id + composition 传 expansion_index + mapping 加字段 | 检索/扩展 |
| 🔴 高 | 安全/引用/意图规则增加**否定/条件/转述语义处理**，避免误拦漏拦（含红队测试） | 安全分流 |
| 🔴 高 | 引用校验增加**证据文本事实一致性**校验（对齐 §13「引用一致率 ≥95%」） | 引用校验 |
| 🟠 中 | 私有/公共证据**显式分层**输入模型，助 LLM 区分事实与一般知识 | 提示构建 |
| 🟠 中 | consent 降级 / rerank 失败 / calibration 状态 **诊断透传与告警** | 检索/编排 |
| 🟠 中 | 评估并**默认开启受控 LLM 改写与意图辅助分类**（改写词表、否定消解） | 查询预处理 |
| 🟡 低 | `min_score` 清理、线程池优雅关闭、status/safety_notice 语义分离 | 检索/编排 |

---

## 附：核查覆盖文件清单

| 模块 | 文件 |
|---|---|
| 编排 | `chat/orchestrator.py`、`chat/prompts.py`、`chat/citations.py`、`chat/contracts.py` |
| 查询 | `query/pipeline.py`、`query/intent.py`、`query/intent_rules.py`、`query/normalize.py`、`query/context_completion.py`、`query/llm_rewrite.py`、`query/contracts.py` |
| 检索 | `retrieval/service.py`、`retrieval/ranking.py`、`retrieval/contracts.py`、`retrieval/expansion.py`、`retrieval/indexing.py` |
| 安全 | `safety/service.py`、`safety/rules.py` |
| 授权 | `auth/policy.py` |
| 领域 | `domain/models.py`、`domain/repositories.py` |
| 知识 | `knowledge/contracts.py`、`knowledge/chunker.py`、`knowledge/semantic_chunker.py` |
| 模型 | `models/contracts.py` |
| 组装 | `api/chat.py`、`api/composition.py` |
| OpenSearch | `retrieval/opensearch_store.py`、`retrieval/opensearch_writer.py` |
