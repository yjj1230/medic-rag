# 市面上主流 RAG 执行流程：从用户提问到 Agent 回答

- 调研日期：2026-09-14（Asia/Shanghai）
- 范围：综合 LangChain、LlamaIndex、OpenAI、Anthropic、Google Cloud、AWS 官方文档，以及 RAG 原始论文；描述可迁移的工程模式，不绑定某一家产品。

## 结论先行

市面上并不存在唯一的“RAG 标准实现”，但主流系统都可归纳为两条闭环：

1. **离线/准实时知识管道**：数据源 → 解析与清洗 → 切分（chunk）→ 元数据 → embedding 与稀疏索引 → 向量/搜索库 → 版本、权限和增量更新。
2. **在线问答/Agent 管道**：用户问题 → 会话与权限校验 → 问题改写/分类/路由 → 检索（向量、关键词或混合）→ 过滤与重排序 → 上下文组装 → LLM 生成或 Agent 工具循环 → 引用、事实性和安全校验 → 流式回答与观测记录。

传统“2-step RAG”把检索固定在生成前，延迟和行为更可预测；“Agentic RAG”让模型决定何时调用知识库、是否拆分问题及是否继续检索，适合多跳任务但需要循环上限、超时和安全控制。LangChain 将这两类分别称为 2-step RAG 与 Agentic RAG；AWS Bedrock 的 agentic retrieval 也支持子查询、多知识库迭代和结果充分性判断。[1][2]

## 一张端到端流程图

```text
用户消息 + 会话历史
        │
        ▼
入口层：认证/租户/速率限制/敏感信息策略/请求 ID
        │
        ▼
问题理解：语言检测、意图与风险分类、指代消解、查询改写、路由
        │
        ├── 无需外部知识 ───────────────┐
        │                               │
        └── 需要知识/工具                │
                ▼                       │
        检索编排：权限过滤 → dense/sparse/hybrid 检索
                ▼
        候选处理：去重、邻块扩展、rerank、上下文压缩/预算控制
                ▼
        上下文 + 问题 + 指令模板 ────────┘
                ▼
        LLM/Agent 决策
          ├─ 直接回答
          └─ 工具调用（检索、数据库、API、代码等）
                 │ 结果写回消息历史，循环直到 stop/预算耗尽
                 ▼
        输出后处理：结构化校验、引用/grounding、内容安全、脱敏
                ▼
        流式/最终答案 + 引用 + 审计、指标和反馈
```

## A. 离线（或准实时）索引流程

### 1. 数据接入与解析

从文件、网页、数据库、SaaS、API 等读取原始数据，解析 PDF/HTML/Office/图片中的文本、表格和版面信息。应保留 `source_id`、版本、页码/段落、时间、作者、租户和访问控制标签，便于引用、更新和权限过滤。Google RAG Engine 将“数据摄取”与“数据转换”列为索引前的独立阶段；LlamaIndex 的 Document/Node 模型也要求节点保留文档关系和元数据。[3][4]

### 2. 清洗、规范化与切分

去除模板噪声、重复页眉、乱码和无效内容；按标题、段落、表格或语义边界切成数十至数百 token 的 chunk，可带少量 overlap。切分大小、边界和 overlap 会直接影响召回；Anthropic 的 Contextual Retrieval 说明也把 chunk 边界列为实现时需实验的参数。[5]

### 3. 增强元数据与上下文

为 chunk 添加标题路径、产品/部门/日期/语言/权限等过滤字段；必要时生成“该 chunk 在原文中的上下文说明”（contextualized chunk），减少切分后语义孤岛。Anthropic 将 Contextual Embeddings 与 Contextual BM25 结合，并报告在其测试集上 top-20 retrieval failure rate 下降 49%，再叠加 reranking 下降 67%；这些是其特定实验结果，不应当作所有语料的保证。[5]

### 4. 建立索引与存储

- **Dense**：embedding 模型将 chunk 和查询映射到向量空间，向量库按相似度召回。
- **Sparse**：BM25/倒排索引保留精确词、编号、专有名词命中能力。
- **Hybrid**：融合 dense 与 sparse 的排名（如 RRF），并可配置权重。
- 保存原文、embedding、metadata、版本和删除标记；支持 upsert、增量重建、去重和缓存。

LlamaIndex 的 IngestionPipeline 典型顺序是 transformations（SentenceSplitter、抽取器、Embedding），然后把节点写入 vector store；它还提供按 node+transformation 缓存、文档 hash 去重和变更重处理。[6] OpenAI Retrieval API 以 vector store 为索引，支持语义搜索、metadata attribute filter、query rewrite、结果数和 score threshold，以及 hybrid ranking 参数。[7]

### 5. 索引质量与发布

对解析失败、低置信度 OCR、超长/空 chunk 进入人工或重试队列；建立索引版本（blue/green 或 alias）后再切换线上。权限标签和删除任务必须传播到原文、派生文本、向量、缓存和摘要，避免“检索到已撤销内容”。

## B. 用户提问后的在线流程

### 1. 请求入口、会话与安全边界

网关完成认证、租户/用户识别、限流、超时和审计；读取会话历史及短期记忆。先做权限过滤，再把允许访问的知识库/数据源作为检索范围，不能只在生成后再“补救”越权。

### 2. 问题理解、改写和路由

根据语言、意图、时间范围、实体和风险等级决定：

- 是否需要检索；
- 检索哪个知识库/租户/数据域；
- 将多轮指代改写为独立查询；
- 是否拆成多个子问题并行或串行检索；
- 是否转人工、拒答或走高风险流程。

OpenAI Retrieval API 提供可选 `rewrite_query`；LlamaIndex 的 RouterRetriever 可在多个候选 retriever 中选择一个或多个。[7][8]

### 3. 候选召回、过滤与重排序

常见顺序是：

1. 生成查询 embedding，同时保留原始关键词；
2. dense、BM25 或 hybrid 召回 top-N；
3. 应用 metadata/ACL/时间/租户过滤（检索引擎支持时优先在索引层预过滤）；
4. 用 cross-encoder/LLM reranker 计算 query–chunk 相关性；
5. 去重、补邻接 chunk、按来源多样性和分数截断到上下文预算。

OpenAI 文档明确返回 chunk、相似度分数、文件来源和属性，并允许 score threshold、最大结果数及 hybrid 权重调节。[7] LlamaIndex 将 postprocessor 定义为对 retrieved nodes 做过滤、转换和 rerank 的组件。[4]

### 4. 上下文组装与提示

把“问题 + 经过权限过滤的证据 + 对话历史摘要 + 系统/开发者指令”组装为模型输入。生产实现通常会：限制 token 预算、保留来源 ID/页码、去除重复内容、对冲突来源排序，并要求模型只基于证据作答或明确说明证据不足。OpenAI 的检索指南示例是先执行 vector store search，再把结果和原查询放入提示进行 grounded synthesis。[7]

### 5. 生成模式：2-step RAG、Agentic RAG、Hybrid

- **2-step RAG**：应用先检索一次，再把结果交给 LLM 生成；链路短、延迟稳定，适合 FAQ 和单跳问答。
- **Agentic RAG**：LLM 先决定是否调用 retriever/tool，可改写查询、拆分子问题、调用多个来源，再把工具结果写回历史继续推理；适合多跳研究、需要外部动作的任务。
- **Hybrid RAG**：固定的安全/权限/首轮检索加上受控 Agent 循环；常用于企业系统，以便限制成本和可观测性。

LlamaIndex 对 FunctionAgent 的循环描述为：发送最新消息、历史和工具 schema → 模型返回直接答案或工具调用 → 执行每个工具并把结果加入历史 → 再次调用模型，直到模型直接回答。[2] LangChain 的 Agent 抽象也是“模型 + 工具 + middleware/harness”，并可用 LangGraph 做更复杂的确定性与 agentic 编排。[1]

### 6. 输出校验、引用与交付

在把答案交给用户前，常见后处理包括：JSON/schema 校验、引用是否来自实际检索片段、关键句的 entailment/faithfulness 检查、敏感内容/提示注入防护、PII 脱敏、拒答和人工升级。返回内容通常包含答案、引用链接或页码、置信/限制说明；同时记录 trace、检索结果、模型版本、延迟、token 和成本。

## C. Agent 编排的最小伪代码

```python
def answer(user_msg, session):
    state = guard_and_classify(user_msg, session)
    for step in range(MAX_STEPS):
        decision = llm(
            messages=state.messages,
            tools=state.allowed_tools,       # retriever/API/database…
            instructions=state.policy,
        )
        if decision.tool_calls:
            for call in decision.tool_calls:
                args = validate_tool_args(call, state.user_acl)
                result = execute_tool(call.name, args)
                state.messages += [decision.message, tool_message(call.id, result)]
            continue
        draft = decision.text
        checked = validate_grounding_citations_safety(draft, state.evidence)
        return stream_or_return(checked)
    return safe_fallback("未能在限定步骤内获得足够证据")
```

## D. 常见增强环节（按问题而非“必选清单”启用）

- **召回质量**：query rewrite、multi-query、HyDE、父子 chunk、邻块扩展、metadata filter、hybrid BM25+dense、reranking、contextual retrieval。
- **复杂问题**：query decomposition、sub-question/parallel retrieval、知识图谱或 SQL tool、跨库聚合、结果充分性判断。
- **上下文效率**：去重、压缩、摘要、按来源多样性选择、长上下文分层和 prompt caching。
- **可靠性与安全**：ACL 在检索前生效、工具参数白名单、循环/预算/超时、提示注入隔离、引用校验、结构化输出、拒答与人工接管。
- **工程运营**：索引版本和增量更新、缓存、trace、重试/死信队列、模型与 embedding 版本固定、成本与延迟监控。

## E. 评估与持续改进

至少把评估拆成“检索层”和“回答层”，并同时做离线回归与线上监控：

| 层次 | 典型问题 | 指标示例 |
| --- | --- | --- |
| 索引/解析 | 内容是否完整、更新是否生效、权限是否泄露 | 解析成功率、重复率、删除残留率、ACL 越权率 |
| 检索 | 相关证据是否进入 top-k、排名是否合理 | Recall@k、Hit-rate、MRR、nDCG、Context Relevancy |
| 生成 | 是否回答问题、是否忠于证据、引用是否正确 | Correctness、Faithfulness、Answer Relevancy、Citation precision/recall |
| Agent/系统 | 是否选对工具、是否陷入循环、是否满足 SLO | tool-call success、任务成功率、步骤数、P95 延迟、token/成本、拒答和安全拦截率 |

LlamaIndex 将 response evaluation（正确性、faithfulness、context/answer relevancy、guideline adherence）与 retrieval evaluation（MRR、hit-rate、precision 等）分开，并支持无 gold label 的部分 LLM 评估。[9] OpenAI 的 evals 指南建议“定义任务 → 用测试输入运行 → 分析结果并迭代提示/系统”，可用于 RAG 和 Agent 的回归集。[10]

## 参考来源（官方/一手资料，访问日期均为 2026-09-14）

1. LangChain，*Retrieval*（含 2-step、Agentic、Hybrid RAG；Agent = model + harness）：<https://docs.langchain.com/oss/python/langchain/retrieval>；RAG 教程：<https://python.langchain.com/docs/tutorials/rag/>
2. LlamaIndex，*High-Level Concepts*（Agent 循环、RAG、Query Engine）：<https://developers.llamaindex.ai/python/framework/getting_started/concepts/index.md>；*Agents*：<https://developers.llamaindex.ai/python/framework/module_guides/deploying/agents/index.md>
3. Google Cloud，*RAG Engine overview*（数据摄取、转换、embedding、索引、检索、生成五阶段）：<https://cloud.google.com/vertex-ai/generative-ai/docs/rag-overview>
4. LlamaIndex，*Introduction to RAG*（Loading、Indexing、Storing、Querying、Evaluation 及 retriever/postprocessor/synthesizer）：<https://developers.llamaindex.ai/python/framework/understanding/rag/index.md>
5. Anthropic，*Introducing Contextual Retrieval*（contextual embeddings/BM25、reranking、chunk 参数与实验结果，2024-09-19）：<https://www.anthropic.com/news/contextual-retrieval>
6. LlamaIndex，*Ingestion Pipeline*（transformations、embedding、vector store、缓存、hash 去重）：<https://developers.llamaindex.ai/python/framework/module_guides/loading/ingestion_pipeline/index.md>
7. OpenAI，*Retrieval*（semantic search、query rewrite、attribute filter、hybrid ranking、synthesize responses）：<https://developers.openai.com/api/docs/guides/retrieval.md>
8. LlamaIndex，*Retriever* 与 RouterRetriever：<https://developers.llamaindex.ai/python/framework/module_guides/querying/retriever/index.md>
9. LlamaIndex，*Evaluating*（response/retrieval evaluation 与指标）：<https://developers.llamaindex.ai/python/framework/module_guides/evaluating/index.md>
10. OpenAI，*Working with evals*（定义任务、运行测试、分析迭代）：<https://developers.openai.com/api/docs/guides/evals.md>
11. AWS，*Amazon Bedrock Knowledge Bases*（托管摄取/索引/检索、权限过滤、agentic retrieval、多跳与充分性评估）：<https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html>
12. Lewis et al.，*Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks*，NeurIPS 2020 / arXiv:2005.11401（参数化与非参数化记忆结合的原始 RAG 论文）：<https://arxiv.org/abs/2005.11401>
