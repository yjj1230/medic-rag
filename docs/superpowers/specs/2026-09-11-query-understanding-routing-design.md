# Query 理解与路由流水线设计

## 1. 文档信息

- 日期：2026-09-11
- 状态：设计已确认，待实施方案
- 依赖：`docs/superpowers/specs/2026-09-09-medical-patient-rag-design.md`、`docs/superpowers/specs/2026-09-10-patient-frontend-design.md`
- 目标：在现有 RAG 链路的「安全分类之后、检索之前」插入一套可组合的 Query 理解与路由流水线，提升检索准确率并按意图驱动检索策略

## 2. 背景与目标

当前链路对用户 query 的处理非常直白：`安全分类 → 权限过滤检索 → LLM 生成`，中间没有 query 理解、改写、意图识别。导致三个问题：

1. **检索召回不准**：口语化、错别字、缩写、术语不一致时搜不到相关知识
2. **不支持多轮**：`session_id` 会话已持久化但 `ChatOrchestrator.answer()` 不读取历史，指代（"它""那个指标"）无法补全
3. **无意图分流**：普通问题全走"公共+私有混合检索"，不按问题类型调节检索口径

本设计在两处收敛的决策上落地：

- **核心目标**：提升检索准确率（不做意图驱动的回答路径/是否调 LLM）
- **意图路由**：驱动检索策略（`include_public / include_private`），不做回答路径短路
- **实现方式**：改写与意图分类均为「规则优先 + LLM 兜底」的混合实现

## 3. 总体架构

### 3.1 数据流

```
用户原始 query (question)
  │  safety.classify → 急症/用药/诊断 → 安全模板(短路)   [现有，最优先级]
  ▼ (普通问题)
QueryPipeline.process(query, history)
  ├─ Step 1 NormalizeStep          规则：口语/错别字归一化 + 同义词/缩写展开
  ├─ Step 2 IntentRouterStep       意图分类 → {report_explanation, health_education,
  │                                       lab_indicator, ordinary}
  ├─ Step 3 ContextCompletionStep  有多轮历史 → 指代补全
  ├─ Step 4 LLMRewriteStep         (可开关)规则兑不住 → 模型网关改写
  │                                       (LLM 兜底同时服务改写与意图低置信度)
  ▼
ProcessedQuery{
  text,             # 改写后的检索文本
  intent,           # 意图标签
  search_strategy,  # 由意图推导的检索策略
  used_llm,         # 是否走了 LLM
  rewrite_log,      # 每步动作，可审计
}
  ▼
构造 SearchQuery(text=strategy.text, ...) → 进入现有 RetrievalService（权限过滤不变）
```

### 3.2 插入位置

`ChatOrchestrator.answer()` 在 `safety` 通过之后、构造 `SearchQuery` 之前。

## 4. 模块结构与核心数据结构

### 4.1 文件结构

```
src/medical_rag/query/
  contracts.py            # ProcessedQuery / QueryContext / QueryStep / Intent / SearchStrategy
  pipeline.py             # QueryPipeline —— 顺序执行 steps
  normalize.py            # NormalizeStep：术语归一化 + 同义词/缩写展开（词典驱动）
  intent.py               # IntentRouter：规则优先 + LLM 兜底
  intent_rules.py         # keyword → 意图 的分类规则
  context_completion.py   # ContextCompletionStep：多轮指代补全
  llm_rewrite.py          # LLMRewriteStep：规则兑不住时走模型网关
```

### 4.2 ProcessedQuery

```python
@dataclass(frozen=True, slots=True)
class SearchStrategy:
    include_public: bool
    include_private: bool

@dataclass(frozen=True, slots=True)
class ProcessedQuery:
    text: str                   # 最终用于检索的改写文本
    original: str               # 原始 query（审计用）
    intent: Intent              # 意图标签
    search_strategy: SearchStrategy
    used_llm: bool              # 是否走了 LLM 改写/分类
    rewrite_log: tuple[str, ...]  # 每步动作记录，可审计
```

### 4.3 QueryContext（steps 间传递）

```python
@dataclass
class QueryContext:
    original: str
    history: tuple[ChatMessage, ...]   # 多轮历史
    current: str                        # 当前改写进度
    used_llm: bool = False
    rewrite_log: list[str] = field(default_factory=list)
```

### 4.4 Step 协议

```python
class QueryStep(Protocol):
    def process(self, ctx: QueryContext) -> QueryContext:
        ...
```

每个 step 改 `ctx.current` 并追加 `ctx.rewrite_log`；规则足够时不触发 LLM（短路）。

## 5. 各 Step 职责

### 5.1 NormalizeStep（纯规则，零外部调用）

- 内置术语映射表：口语/错别字/缩写 → 标准医学词
  - `胸疼→胸痛`、`验血→血常规`、`血压高/偏高→高血压`、`血糖高→高血糖`、`CT→计算机断层扫描(CT)`、`Hb→血红蛋白`
- 归一化后按需做同义词展开（替换为主，展开的检索用途留接口）
- 无匹配则不改写，短路进入下一步

### 5.2 IntentRouterStep（规则优先 + LLM 兜底）

- `intent_rules.py` 定义 keyword → 意图，优先级：`report_explanation > lab_indicator > health_education > ordinary`
  - `report_explanation`：`报告|结果|指标|复查|我的|上次查|这数值|偏高|偏低`（结合患者私有权重）
  - `lab_indicator`：`是什么|什么意思|偏高|偏低|参考范围|正常吗`
  - `health_education`：`怎么|如何|注意|预防|饮食|运动|生活`（无"我的/报告"语境）
- 规则输出置信度；低置信度/模糊信号 → 走 LLM few-shot 分类（复用模型网关），输出一个意图标签
- 意图 → 检索策略（见第 6 节）

### 5.3 ContextCompletionStep

- 编排器按 `session_id` 拉最近 N 条消息（默认 N=4）作为 `history`
- 当前 query 含指代词（`它|那个|这|上面|上次|这指标|这药`）且存在历史 → 启动补全
- 规则策略：用最近 assistant 回答中提到的关键实体替换指代；无法确定 → 标记 `needs_llm`
- 无历史或无指代 → 不改写，跳过

### 5.4 LLMRewriteStep（可开关，保守默认关）

- 触发条件：有有效会话历史 且（改写/指代仍含未解指代 或 意图低置信度），且 `enable_llm` 为开
- 走现有 `ModelGateway.chat`（因此仅 `EXTERNAL_MODEL` 同意链路上可用）；本地无 key 时自动降级跳过，不阻断
- Prompt 约束：只改写以提升检索，不回答、不增删医学事实，输出 `[query]`
- 标记 `used_llm=True`

## 6. 意图 → 检索策略映射

利用现有 `SearchQuery.include_public / include_private` 两个口径：

| 意图 | 检索策略 | SearchQuery 改动 |
|------|----------|------------------|
| `report_explanation` | 主搜患者私有，公共辅助 | `include_public=False`、`include_private=True` |
| `health_education` | 只走公共 | `include_public=True`、`include_private=False` |
| `lab_indicator` | 公共知识 + 当前患者私有报告 | 两者 `True`；从改写产物提取线索指标 |
| `ordinary` | 混合（现状） | 两者 `True`（保持默认） |

边界：患者无私有资料但仍要求报告解释 → 检索走公共兜底，落到现有 `insufficient_evidence` 路径，提示"未找到报告"。

## 7. 与现有 safety 的关系

- `safety`（急症/用药/诊断）：最前面，决定"是否短路"
- `intent`（报告/科普/指标）：后面，决定"普通问题怎么检索"
- 两层正交，`RiskClass` 与 `Intent` 不重叠

## 8. 集成点与配置

### 8.1 orchestrator.py

```python
decision = self.safety.classify(question)
if decision.action != ALLOW_RETRIEVAL:
    return self._safe_response(context, decision)
processed = self.query_pipeline.process(
    QueryContext(question, history=self._load_history(session_id))
)
query = SearchQuery(
    text=processed.text,
    patient_id=context.patient_id,
    top_k=8,
    include_public=processed.search_strategy.include_public,
    include_private=processed.search_strategy.include_private,
)
```

- 模型提示词仍用**原始用户问题**（改写只服务于检索，不改变用户看到的提问）

### 8.2 新增配置（.env）

```ini
QUERY_REWRITE_ENABLED=true
QUERY_PIPELINE_HISTORY_SIZE=4
QUERY_LLM_REWRITE_ENABLED=false
INTENT_LLM_CLASSIFY_ENABLED=false
```

## 9. 新增/修改文件清单

**新增（query 模块）**：
- `src/medical_rag/query/contracts.py`
- `src/medical_rag/query/pipeline.py`
- `src/medical_rag/query/normalize.py`
- `src/medical_rag/query/intent.py`
- `src/medical_rag/query/intent_rules.py`
- `src/medical_rag/query/context_completion.py`
- `src/medical_rag/query/llm_rewrite.py`

**修改**：
- `src/medical_rag/chat/orchestrator.py`（注入 pipeline + session 历史读取）
- `src/medical_rag/domain/models.py`（如需为 ChatSession 增加检索改写产物字段，可选）

## 10. 测试与验收

- **单元**：每个 step 独立测试（归一化命中/未命中/短路、意图规则命中/未命中/优先级、指代补全、LLM 短路逻辑）
- **集成**：
  - 口语问题被归一化（"胸疼"→"胸痛"）
  - 多轮指代被补全（"它偏高"→带历史完整化）
  - 各意图 → 检索策略映射正确
  - 无会话历史/无私有资料时优雅降级到 `insufficient_evidence`
  - LLM 开关关闭时完全不外发；LLM 不可用时优雅降级
  - `pytest -q` 保持全绿
- **YAGNI**：本轮不做 HyDE、Query Decomposition、agentic tool 调用、意图驱动的回答路径短路

## 11. 明确不做（本轮范围外）

- 意图配置是否调 LLM / 是否走模板回答（回答路径短路）
- Query Decomposition（复合问题拆分）
- HyDE（假设文档向量）
- Agentic RAG / 工具调用