# 复合 Query 拆解与逐项检索设计

## 1. 文档信息

- 日期：2026-09-16
- 状态：设计已确认，待实施方案
- 依赖：`docs/superpowers/specs/2026-09-11-query-understanding-routing-design.md`（Query 理解管线）、`docs/superpowers/specs/2026-09-09-medical-patient-rag-design.md`
- 目标：在 Query 理解管线之前插入**确定性复合问题拆解**，逐子问题完成意图路由与并行检索，合并证据后**单次**生成回答，并对未覆盖的子问题给出**确定性提示**

## 2. 背景与动机

真实患者会在一句话里问多个子问题（部分主题相关、部分相互独立），当前链路把整句压成**单一意图、单一检索口径**，导致部分子问题的检索口径被忽略：

例：`我血红蛋白偏高吗？还有我该吃什么？`

- `_REPORT` 命中 0（无"报告/结果/我的"）；`_LAB` 命中"偏高"得 1 分；`_EDUCATION` 命中"应该吃什么"得 1 分
- 按优先级排序 → 意图 = `lab_indicator` → 检索口径 = 公共+私有混合
- "我该吃什么"本应走 `health_education` 的**只搜公共**口径，该口径丢失

`2026-09-11` 设计 §11 曾明确将 Query Decomposition 列为范围外（当时无真实复合问句输入）。本设计在真实复合问句已出现的背景下解禁该能力，但保持 YAGNI 边界与"永不劣于现状"的降级原则。

## 3. 范围与非目标

### 3.1 v1 范围

用户一句提问含**多个并列子问题**（主题相关但各自可独立检索）时：

1. 规则拆解为子问题（确定性，无模型调用）
2. 每个子问题**各自**完成归一化、指代补全、意图路由 → 各自正确的 `include_public / include_private` 检索口径
3. N 个子问题**并行**检索，证据合并去重
4. **单次**模型生成（prompt 明确要求逐项回答）
5. 有子问题未检索到证据时，在回答中**确定性**附加未覆盖清单（不解析模型输出）

### 3.2 非目标（明确不做）

| 非目标 | 原因 |
|---|---|
| 多跳 / 串行依赖（"那我需要吃药吗"） | 需两轮检索，8s 预算守不住；真实场景主要是主题相关、各自可独立检索 |
| 每个子问题独立生成再拼接 | N 次模型调用，延迟翻 N 倍 |
| LLM 拆解兜底（v1 实现） | 延迟 + EXTERNAL_MODEL 同意链路成本；规则先行，真实语料证明不够再上（接口预留） |
| HyDE / Agentic / 工具调用 | 与目标无关 |
| 修改 `RetrievalService` 内部检索逻辑 | 混合检索 / RRF / ACL / 门控全部保持不变 |
| 修改 `ChatRequest` / `OpenAIChatCompletionsAdapter` 契约 | 证据仍平铺进 prompt，分组信息走问题文本（见 §6.5） |
| 修复 IntentRouter LLM 兜底装配缺口 | 独立的既有缺口（`composition.py` 未传 `llm_classifier`），与本设计解耦；见 §12 |

## 4. 总体架构与数据流

```text
question
 → safety.classify（整句，急症/用药/诊断 → 安全模板短路）        [现有，不变]
 → DecomposeStep（规则拆解 → 子问题 1..N，N ≤ 3）                [新增，位于理解步骤之前]
 → 逐子问题: NormalizeStep → ContextCompletionStep → IntentRouter [复用现有组件，跑 N 次]
 → 逐子问题安全复检: 任一子问题非 ALLOW_RETRIEVAL → 整句安全模板   [新增，fail-closed]
 → 逐子问题 derive_strategy(intent, has_private)                 [复用现有]
 → N 个子问题并行检索（有界线程池 + RetrievalService）             [新增编排]
 → 证据并集去重 + 逐组覆盖记录                                    [新增]
 → 逐组兄弟上下文扩展（各组自己的口径，复用 Task 9 seam）          [新增]
 → 单次生成: SYSTEM_PROMPT 逐项指令; 证据平铺（扩展后并集）        [小改]
 → CitationValidator（扩展后并集校验 + 段落覆盖强制逐项引用）      [不变]
 → AnswerResponse + 未覆盖子问题确定性 AnswerSection              [新增]
```

### 4.1 关键顺序决策：拆解必须在 Normalize / ContextCompletion 之前

`ContextCompletionStep` 将锚点实体追加到**整句末尾**（`context_completion.py`：`completed = f"{ctx.current} {anchor}"`）。若先补全后拆解：

```text
"它偏高吗？还有我该吃什么？"  (历史含"血红蛋白")
 → 补全 → "它偏高吗？还有我该吃什么？ 血红蛋白"
 → 拆解 → 碎片1="它偏高吗？"（指代未解决！）
          碎片2="我该吃什么？ 血红蛋白"（锚点落错碎片！）
```

先拆解后逐碎片补全，则碎片1 自己拿到锚点 → `"它偏高吗？ 血红蛋白"`，指代解决落在正确碎片。

`NormalizeStep` 为局部子串替换（TERM_MAP），逐碎片执行与整句执行等价，不受顺序影响。

### 4.2 降级不变式（永不劣于现状）

`ProcessedQuery.sub_questions` **默认空元组**。为空时 orchestrator 完全走现有单查询路径。以下场景均落入空元组：

- `QUERY_DECOMPOSITION_ENABLED=false`
- 规则拆解无有效碎片（返回单元素 = 原句）
- 有效碎片数超过上限（见 §6.1，放弃拆解而非截断）
- 拆解组件抛出任何异常

## 5. 模块结构

```text
src/medical_rag/query/
  decompose.py              # 新增：QuestionDecomposer（确定性规则拆解）
  contracts.py              # 修改：SubQuestion；ProcessedQuery.sub_questions
  pipeline.py               # 修改：process() 重构为 拆解 → 逐子问题理解
src/medical_rag/chat/
  subquestion_retrieval.py  # 新增：SubQuestionRetriever（并行检索编排 + 证据合并）
  orchestrator.py           # 修改：answer() 编排循环与覆盖提示
  prompts.py                # 修改：SYSTEM_PROMPT 逐项指令；build_chat_request 可选 sub_questions
src/medical_rag/core/
  config.py                 # 修改：2 个新配置项
tests/
  unit/test_query_decompose.py        # 新增
  unit/test_subquestion_retrieval.py  # 新增
  integration/test_compound_query.py  # 新增
```

## 6. 组件职责与接口

### 6.1 QuestionDecomposer（query/decompose.py）

确定性、纯函数：同一输入恒产同一输出；不调用模型。

```python
class QuestionDecomposer:
    def __init__(self, max_sub_questions: int = 3) -> None: ...
    def decompose(self, text: str) -> tuple[str, ...]: ...
    # 返回长度 1 表示不拆：常规情形 = 原句；去重折叠到单碎片时 = 该碎片（重复提问视为同一问题）
```

拆解规则（按序应用）：

1. **问号/分号切分**：在 `？！;；` 处切分，两侧非空才成碎片
2. **连接词切分**：连接词（`还有/以及/另外/同时/并且/然后/顺便`）仅当其**前方紧邻标点**（`，。；！？`）时作为切分点（防止"报告里还有哪些异常"这类"还有=仍然有"被误拆）
3. **问题性校验**：碎片必须含疑问信号（`吗|呢|什么|怎么|如何|哪些|多少|是不是|能否|可否`）或以 `？/?` 结尾，且长度 ≥ 4；不满足者并回相邻碎片
4. **去重**：归一化空白后相同的碎片合并；折叠后仅剩 1 个碎片时返回该碎片（重复提问视为同一问题），其余单碎片情形仍返回原句
5. **上限保护**：有效碎片数 > `max_sub_questions` → **放弃拆解**（返回单元素），rewrite_log 记录原因。宁可走现状单查询路径，也不截断患者的问题
6. 任何异常 → 返回单元素

v1 不实现 LLM 兜底；构造函数预留 `llm_fallback=None` 参数位，未来接入必须走 `ModelGateway`（EXTERNAL_MODEL 同意 + 审计）。

### 6.2 契约扩展（query/contracts.py）

```python
@dataclass(frozen=True, slots=True)
class SubQuestion:
    text: str                    # 该子问题最终用于检索的文本（已归一化/补全）
    intent: Intent
    search_strategy: SearchStrategy

@dataclass(frozen=True, slots=True)
class ProcessedQuery:
    text: str                    # 保留现有语义（单查询路径使用）
    original: str
    intent: Intent               # 兼容字段：子问题中 PRIORITY 最高者；无子问题时 = 现有整句意图
    search_strategy: SearchStrategy
    used_llm: bool = False
    rewrite_log: tuple[str, ...] = ()
    sub_questions: tuple[SubQuestion, ...] = ()   # 新增；空 = 未拆分（旧路径）
```

兼容性要求：`ProcessedQuery` 现有构造调用（位置参数与关键字）全部不变；`sub_questions` 有默认值。

### 6.3 QueryPipeline 重构（query/pipeline.py）

```text
process(original, history, has_private, access_context):
  ctx = QueryContext(original, current=original, ...)
  fragments = decomposer.decompose(original)          # 新增第一步
  if len(fragments) == 1:
      # 现有单查询流程，一字不变（normalize → intent → completion → strategy）
  else:
      for fragment in fragments:
          碎片ctx = 新 QueryContext(current=fragment, history=history, ...)
          NormalizeStep → ContextCompletionStep → IntentRouter 逐碎片执行
          strategy = derive_strategy(碎片intent, has_private)
      整句 intent = 子问题中 PRIORITY 最高者（复用 intent_rules.PRIORITY）
  rewrite_log 记录拆解决策与每碎片动作
```

历史（`history`）对每个碎片可见——指代补全依赖跨轮历史，不属于碎片内部上下文。

### 6.4 SubQuestionRetriever（chat/subquestion_retrieval.py）

单一职责：把 N 个子问题变成 N 组证据。

```python
@dataclass(frozen=True, slots=True)
class SubQuestionEvidence:
    question: str
    items: tuple[Evidence, ...]
    search_strategy: SearchStrategy   # 该组检索口径（供逐组兄弟上下文扩展使用）
    reason: str | None        # None=成功; "insufficient_evidence"/"retrieval_timeout"/...

@dataclass(frozen=True, slots=True)
class MergedRetrieval:
    groups: tuple[SubQuestionEvidence, ...]

    @property
    def items(self) -> list[Evidence]:
        """并集：按 evidence id 去重、保序（首次出现优先）。"""

class SubQuestionRetriever:
    def __init__(self, retriever, *, max_workers: int = 4) -> None: ...
    def retrieve_all(self, sub_questions, context) -> MergedRetrieval: ...
```

- 有界 `ThreadPoolExecutor`，**进程内单例**（lazy 创建，不随请求建池）
- 每子问题：`SearchQuery(text=碎片text, include_public=strategy.include_public, include_private=strategy.include_private, patient_id=context.patient_id)` → `retriever.retrieve(query, context)`
- 单组失败（异常/超时）只标记该组 `reason`，不影响其他组；组内异常不向上传播
- 复合路径的 Task 9 兄弟上下文扩展：orchestrator 对每个非空组用**该组自己的** `search_strategy` 调 `_expand_evidence`（组携带口径，不依赖 groups 与 sub_questions 的顺序配对），再按证据 id 去重合并为模型输入与引用校验集合；单查询路径的扩展行为不变。兄弟上下文开销随组数同向增长（每组 sibling 预算与单查询路径相同），由 §10.4 评测把关
- 并发安全依据（已核实）：`CachedEmbeddingModel` 持 `threading.Lock` + LRU；`HybridRanker.fuse` 为纯函数；`RetrievalService._search_candidates` 每路独立异常隔离；OpenSearch client 线程安全
- 组级超时：`future.result(timeout=剩余总预算)`（orchestrator 传入 `total_timeout_seconds - 已耗时`，不新增配置项）；超时组标记 `retrieval_timeout`（底层线程无法强杀，池有界故泄漏有上限，可接受）

### 6.5 生成与覆盖提示

**Prompt（chat/prompts.py）**：

- `SYSTEM_PROMPT` 增补逐项指令：用户问题可能包含多个子问题，必须**逐项**回答；某项证据不足时，**仅对该项**明确说明无法可靠回答，不影响其他项
- `build_chat_request(question, evidence, *, sub_questions=(), ...)`：当提供 `sub_questions` 时，把子问题清单与逐项要求写入问题文本；**证据仍平铺（并集）、`[evidence-id]` 标记格式不变**（`CitationValidator.citation_pattern` 与渲染端共用格式，不得漂移）
- 生成提示词使用**原始用户问题**（沿用 2026-09-11 设计 §8.1 约定：改写只服务于检索）

**引用校验（不变）**：`CitationValidator.validate(answer, evidence=扩展后并集)`（并集先经逐组兄弟上下文扩展，见 §6.4）。段落覆盖规则（≥30 字段落必须含 `[id]`）天然强制模型对每个子问题的回答段落给出引用。

**未覆盖提示（确定性，不解析模型输出）**：凡 `reason is not None` 的组，向 `AnswerResponse.sections` 附加一个确定性 section（title=`未能回答的子问题`，text=逐条列出未覆盖子问题原文与原因提示）。哪些子问题无证据是我们自己检索时即可知的事实，无需依赖模型输出。

**整组为空**：所有组 `items` 为空 → 走现有 `insufficient_evidence` 路径，行为不变。

### 6.6 安全复检

每个子问题 text 单独 `safety.classify`；**任一子问题非 `ALLOW_RETRIEVAL` → 整句走 `_safe_response` 安全模板**（fail-closed，绝不部分回答急症/用药问题）。与现有"归一化/补全后二次分类"先例（orchestrator 对 `processed.text` 的 recheck）语义一致，扩展到碎片粒度。

已知代价：碎片可能丢失整句上下文，命中整句不会命中的规则 → 拦截率可能上升。医疗上 fail-closed 正确；上升幅度必须用真实语料量化（§10）。实现备注：当前安全规则为大小写折叠后的子串匹配（safety/rules.py），且碎片文本是整句的子串或加法变换（归一化只替换医学术语、补全只追加锚点实体），故现有规则下碎片复检几乎不会新增拦截；该复检是对未来上下文相关规则的防御纵深，拦截率仍按 §10 实测。

## 7. 延迟与资源

| 阶段 | 增量 | 依据 |
|---|---|---|
| 拆解 | 毫秒级（纯规则） | 无模型调用、无 IO |
| 检索 | 墙钟 ≈ max(各组) ≈ 现有单次检索同量级 | 并行执行；OpenSearch client 已有 timeout=10 |
| Embedding | 每子问题一次，并发执行 | `CachedEmbeddingModel` LRU 去重相同措辞；带锁并发安全 |
| 生成 | **零增量**（仍单次模型调用） | 证据平铺，prompt 增量仅子问题清单文本 |

P95 目标：不明显高于现状（≤ 8s）。验收以计时测试 + 真实语料评测双重确认，不靠估算。

时间预算保护：orchestrator 现有 `remaining ≤ _MIN_MODEL_BUDGET_SECONDS` 检查保留；检索阶段组级超时见 §6.4。

## 8. 配置（新增）

```ini
QUERY_DECOMPOSITION_ENABLED=true
QUERY_DECOMPOSITION_MAX_SUBQUESTIONS=3
```

`Settings`（core/config.py）新增对应字段；关闭时 `DecomposeStep` 直接返回单元素。

## 9. 修改文件清单

**新增**：`query/decompose.py`、`chat/subquestion_retrieval.py`、`tests/unit/test_query_decompose.py`、`tests/unit/test_subquestion_retrieval.py`、`tests/integration/test_compound_query.py`

**修改**：`query/contracts.py`、`query/pipeline.py`、`chat/orchestrator.py`、`chat/prompts.py`、`core/config.py`；受影响的既有测试保持全绿。

当前工作区无 Git 仓库：不创建提交，以测试结果与变更清单作为任务完成凭证（沿用 document-quality-indexing 计划的约定）。

## 10. 测试与验收

### 10.1 单元

- **decomposer**：问号切分、连接词切分、"还有=仍然有"不误拆（`报告里还有哪些异常`）、碎片并回（`我`）、上限放弃、确定性（同输入同输出）、空串/单问句不拆、异常降级
- **pipeline 拆解顺序回归（关键）**：`它偏高吗？还有我该吃什么？` + 历史含`血红蛋白` → 碎片1 的补全结果必须包含锚点（锚点落对碎片）
- **subquestion_retrieval**：并集去重保序、单组异常不影响他组、组级超时降级、结果顺序稳定
- **prompts**：逐项指令渲染；`[evidence-id]` 标记格式不变；不提供 sub_questions 时输出与现状一致

### 10.2 契约

- `ProcessedQuery` 向后兼容：`sub_questions` 默认空时，现有全部测试不修改即通过

### 10.3 集成

- 跨意图复合问题：报告解释碎片 `include_public=False`，科普碎片 `include_private=False`（各自口径正确）
- 部分覆盖：一组空证据 → 其余正常回答 + 确定性未覆盖 section
- 全部为空 → `insufficient_evidence`
- 碎片命中急症/用药 → 整句安全模板
- `QUERY_DECOMPOSITION_ENABLED=false` → 行为与现状一致
- 计时：fake retriever 固定延迟，断言并行墙钟 ≈ max 而非 sum
- 权限回归：跨租户/跨患者记录在碎片路径下仍被 ACL 拦截

### 10.4 评测（真实语料）

用真实复合问句建 golden 集（含人工标注的子问题划分与期望意图），开关前后对比：

| 指标 | 要求 |
|---|---|
| 拆解准确率（碎片划分与人工标注一致） | 达到设定基线后才允许默认开启 |
| 每子问题证据召回覆盖 | 不低于开关前 |
| 拦截率（safe_template / UnsupportedClaim 占比） | 记录变化幅度并评审；显著上升需调整碎片安全复检策略（仍 fail-closed） |
| P95 延迟 | 不明显高于基线（≤ 8s） |
| 未覆盖提示准确性 | 确定性逻辑，应为 100% |

## 11. 明确不做（v1 范围外）

- LLM 拆解兜底的实现（仅预留接口；启用前提：真实语料证明规则拆解不足，且走 ModelGateway 同意/审计链路）
- 多跳 / 串行依赖子问题
- 每子问题独立生成
- 修改 `ChatRequest` / `OpenAIChatCompletionsAdapter` / `RetrievalService` 内部契约
- IntentRouter LLM 兜底装配修复（独立缺口，见 §12）

## 12. 已知风险与遗留缺口

| 项 | 说明 | 处置 |
|---|---|---|
| 拦截率可能上升 | 碎片单独过安全分类 + 段落引用强制，可能比整句更容易触发安全模板/引用校验失败 | golden 集量化；医疗 fail-closed 优先，显著上升时调整策略并评审 |
| 短碎片意图分类弱化 | 碎片丢失整句上下文，规则可能不命中 | 兜底 `ORDINARY` = 现状混合口径，不会更差；用数据确认 |
| IntentRouter LLM 兜底装配缺口（既有） | `composition.py` 构造 `IntentRouter(enable_llm=...)` 未传 `llm_classifier`，`_should_use_llm` 恒 False，配置开关打开也不生效；`LLMRewriteStep` 触发条件亦窄于设计（缺"意图低置信度"） | 独立缺口，不阻塞本设计（v1 规则内闭环）；若未来启用 LLM 兜底须先修 |
| 未来多跳需求 | v1 明确不支持 | `SubQuestion` 结构可向后扩展依赖字段，不需要破坏性变更 |
