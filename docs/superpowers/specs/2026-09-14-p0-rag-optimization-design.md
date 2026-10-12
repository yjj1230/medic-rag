# P0 RAG Optimization Design

## 1. 文档信息

- 日期：2026-09-14
- 状态：待用户审阅
- 适用项目：`medical-rag`
- 范围：评估基线、患者私有索引、真实 Embedding、Hybrid Retrieval

## 2. 目标

在不改变医疗安全边界的前提下，打通“患者报告解析 → 私有索引 → 语义检索 → 证据回答”的端到端链路，并建立可重复的质量评估和版本化发布机制。

P0 完成后，系统应满足：

1. 上传并解析成功的患者报告可以进入私有检索索引。
2. 公共知识和患者私有证据都具备真实向量，可执行语义检索。
3. BM25 与向量检索分路执行，并在权限过滤后进行融合和重排。
4. 每次召回和回答都能被离线评估、追踪和回归验证。

## 3. 非目标

- 不在 P0 引入自主 Agent、多工具循环或开放式联网搜索。
- 不改变现有诊断、处方、药物调整和急症分流规则。
- 不把患者数据写入公共知识索引。
- 不在没有评估结果的情况下调整模型、chunk 大小或分数阈值。

## 4. 当前实现与问题

当前项目已经具备认证、知情同意、文件解析、公共知识导入、权限过滤、模型网关和引用校验的基础模块，但存在四个闭环缺口：

1. `ParsingService` 只写入 PostgreSQL 的 `EvidenceUnit`，没有私有索引写入接口。
2. `SearchQuery.embedding` 已定义但没有在入库和查询流程中生成。
3. `OpenSearchStore` 将 `match` 与 `knn` 放入同一查询，未对两类分数做可解释融合。
4. 没有固定评估集、检索诊断信息和质量回归门禁。

## 5. 总体架构

采用“模块化单体 + IndexJob/Outbox + 版本化索引”：

```text
文件上传
  → ParsingService
  → PostgreSQL: EvidenceUnit / MedicalFact / IndexJob
  → IndexWorker
      → 分批 Embedding
      → 私有或公共索引 bulk upsert
      → 标记 IndexJob 完成

用户问题
  → 安全分流
  → 查询 embedding
  → BM25 候选检索 ┐
  → KNN 候选检索  ├→ ACL/metadata 过滤 → RRF → rerank → Top-K
                  ┘
  → Prompt 组装
  → ModelGateway
  → 引用/安全校验
  → 答案与检索诊断
```

索引写入和在线检索都通过小而稳定的接口隔离 OpenSearch、Embedding 服务和 Celery。调用者不需要了解具体供应商、索引名称或重试细节。

## 6. P0-1 评估基线

### 6.1 评估数据

新增 `tests/eval/cases.jsonl`，只使用合成、脱敏或公开知识内容。每个案例至少包含：

```json
{
  "id": "private-report-001",
  "question": "我的血红蛋白是多少？",
  "scope": "private",
  "tenant_id": "tenant-a",
  "patient_id": "patient-a",
  "gold_evidence_ids": ["evidence-001"],
  "expected_status": "answered"
}
```

案例必须覆盖公共知识、私有报告、表格、双语、无证据、删除后文档、跨患者访问、诊断请求、用药变更和急症请求。

### 6.2 指标

检索层：

- Recall@k / Hit Rate
- MRR / nDCG
- 越权召回率
- 删除后残留召回率

回答层：

- 关键医学结论引用覆盖率
- 引用与原文一致率
- Unsupported Claim Rate
- 无证据拒答率

工程层：

- 检索、Embedding、rerank、模型调用的 P50/P95 延迟
- 模型调用成功率和降级率
- Embedding / rerank / LLM Token 成本

零容忍指标：跨患者召回、删除后残留召回、高风险越界回答都必须为 0。

### 6.3 评估执行

基线先使用当前实现运行一次并保存结果。后续每次只改变一个变量，按以下顺序比较：

```text
当前实现
→ 私有索引
→ 真实 Embedding
→ 分路 Hybrid + RRF
→ rerank 参数
```

评估结果和检索诊断只保存 ID、分数、版本和哈希，不记录不必要的患者原文。

## 7. P0-2 患者私有索引

### 7.1 数据契约

在 `retrieval/contracts.py` 增加：

```python
@dataclass(frozen=True, slots=True)
class PrivateIndexRecord:
    id: str
    text: str
    tenant_id: UUID
    patient_id: UUID
    document_id: UUID
    document_version_id: UUID
    evidence_unit_id: UUID
    source_title: str
    source_version: str
    page_number: int | None
    sheet_name: str | None
    cell_range: str | None
    source_anchor: str | None
    confidence: float | None
    retrieval_eligible: bool
    deleted: bool
    embedding: tuple[float, ...] | None
    embedding_model: str | None
    index_version: str
```

```python
class PrivateIndexWriter(Protocol):
    def upsert(self, records: list[PrivateIndexRecord]) -> None: ...
    def delete_document(self, document_id: UUID) -> None: ...
```

### 7.2 索引写入流程

1. `ParsingService` 保存 `EvidenceUnit` 和 `MedicalFact`。
2. 在同一数据库事务中创建 `IndexJob`，记录文档版本、解析器版本和目标索引版本。
3. `IndexWorker` 读取任务，生成或读取 embedding。
4. 批量写入私有 OpenSearch 索引。
5. 成功后记录索引版本和完成时间；失败保留错误类别和重试次数。

索引文档必须包含 `tenant_id`、`patient_id`、`document_id`、`document_version_id`、`evidence_unit_id`、`visibility=private`、`source_active`、`retrieval_eligible` 和 `deleted`。

### 7.3 Review Gate

解析任务为 `NEEDS_REVIEW` 时，证据和事实可以保存在 PostgreSQL，但对应索引记录必须设置 `retrieval_eligible=false`。只有人工复核通过后，才允许 upsert 为可检索记录。

### 7.4 一致性和幂等

- 索引幂等键：`document_version_id + evidence_unit_id + parser_version + embedding_model`。
- OpenSearch 写入使用 bulk upsert。
- 数据库任务记录是重试事实来源。
- 删除服务必须调用 `delete_document`，并验证对象存储、数据库和私有索引均无残留。

## 8. P0-3 真实 Embedding

### 8.1 配置

增加以下配置项：

```text
embedding_model
embedding_dimensions
embedding_batch_size
embedding_timeout_seconds
embedding_index_version
```

Embedding 模型必须通过现有 `EmbeddingModel` 契约和 `ModelGateway`/供应商适配器访问。

### 8.2 离线流程

```text
chunk/evidence text
  → batch embed
  → 校验返回数量和向量维度
  → 写入 index record
```

每条记录保存 `embedding_model`、维度、内容哈希和 `index_version`。相同 `content_hash + embedding_model + index_version` 不重复生成。

### 8.3 在线流程

在线查询只生成一次问题向量：

```text
用户问题
  → 标准化后的查询文本
  → EmbeddingModel.embed([text])
  → SearchQuery.embedding
```

`OpenSearchStore` 不负责调用 Embedding 服务，只接收已经生成的向量。

### 8.4 失败处理

- Embedding 调用仅允许有限次重试。
- 单批失败不丢失任务，IndexJob 进入可重试状态。
- 向量数量或维度不匹配时整批失败，不写入部分结果。
- 在线查询 embedding 超时则返回可解释降级，不执行无界重试。

## 9. P0-4 Hybrid Retrieval

### 9.1 查询契约

使用显式过滤对象，避免把权限字段散落在多个参数中：

```python
@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    tenant_id: UUID
    patient_id: UUID
    include_public: bool
    include_private: bool
    document_ids: tuple[UUID, ...] = ()
```

```python
class CandidateRetriever(Protocol):
    def search_lexical(
        self, text: str, filters: RetrievalFilters, limit: int
    ) -> list[SearchRecord]: ...

    def search_vector(
        self, embedding: tuple[float, ...], filters: RetrievalFilters, limit: int
    ) -> list[SearchRecord]: ...
```

### 9.2 执行顺序

```text
候选检索 Top-N
  → OpenSearch 层 ACL/metadata 过滤
  → 服务层二次授权校验
  → RRF 融合
  → 去重和来源多样性处理
  → rerank Top-M
  → 分数阈值与上下文预算
  → 返回 Top-K Evidence
```

公共索引和私有索引保持独立。私有记录必须同时匹配 `tenant_id` 和 `patient_id`；删除、失效和待复核记录在索引层排除。

### 9.3 融合策略

第一版使用 Reciprocal Rank Fusion，不直接比较 BM25 与向量的原始分数：

```text
RRF(d) = 1 / (k + rank_lexical(d))
       + 1 / (k + rank_vector(d))
```

建议候选规模为 `max(top_k * 5, 50)`，rerank 数量单独配置。`min_score` 只能作用于经过校准的融合分数；如果无法校准，则使用评估集确定的排名/覆盖门槛，而不是沿用当前固定 `0.35`。

### 9.4 检索诊断

`RetrievalResult` 增加诊断字段：

```python
diagnostics = {
    "lexical_candidates": 50,
    "vector_candidates": 50,
    "fused_candidates": 32,
    "reranked_candidates": 20,
    "index_version": "private-v3",
    "embedding_model": "text-embedding-3-small",
}
```

诊断中可记录候选 ID、排名、分数、过滤原因和耗时，但默认不记录患者原文。

## 10. 接口与模块边界

### `knowledge` 层

负责 chunk、内容哈希、来源版本和公共知识入库；不直接依赖 HTTP 或 OpenSearch 客户端。

### `parsing` 层

负责文件解析、证据单元和事实抽取；通过 `PrivateIndexWriter` 和 `IndexJob` 将结果交给索引流程。

### `models` 层

负责 `EmbeddingModel`、`RerankerModel` 和 `ChatModel` 的供应商适配；不向上层暴露供应商专用请求对象。

### `retrieval` 层

负责 ACL 过滤、候选召回、RRF、rerank、阈值和检索诊断；不负责生成 embedding。

### `chat` 层

负责安全分流、查询调用、证据传递、模型回答和引用校验；P0 不增加自主 Agent 循环。

## 11. 错误和降级策略

| 阶段 | 失败行为 |
|---|---|
| 解析 | 任务失败或待复核；不写入可检索证据 |
| Embedding 入库 | IndexJob 可重试；不标记索引完成 |
| 查询 Embedding | 返回可解释降级，不使用伪造向量 |
| BM25/KNN 单路失败 | 记录诊断；若另一条链路满足最低门槛可降级运行 |
| RRF/rerank 失败 | 返回已授权的基础候选，或进入证据不足 |
| 无足够证据 | 不调用模型，返回 `insufficient_evidence` |
| 模型失败 | 返回现有安全降级响应 |
| 删除失败 | 返回失败层清单并进入补偿任务，不宣称删除完成 |

## 12. 测试和验收

必须增加或更新：

- `tests/eval/test_rag_quality.py`
- `tests/unit/test_knowledge_chunker.py`
- `tests/unit/test_retrieval_service.py`
- `tests/contract/test_retrieval_store.py`
- `tests/integration/test_retrieval_permissions.py`
- `tests/integration/test_delete_residuals.py`

关键验收：

1. 上传的患者报告经过解析后可以被同一患者的私有查询命中。
2. 另一租户或另一患者永远无法命中私有记录。
3. 待复核和已删除证据不会进入可检索结果。
4. 语义相近但关键词不同的问题可以通过向量检索命中。
5. BM25、KNN、RRF、rerank 的候选数量和耗时可观察。
6. Recall@k、MRR 和引用指标不低于基线，且越权率、删除残留率、高风险越界回答为 0。
7. 试点普通问答 P95 延迟不超过设计目标 8 秒。

## 13. 发布策略

采用索引版本和 alias 切换：

```text
private-v1 / public-v1
  → 构建 private-v2 / public-v2
  → 离线评估和权限回归
  → 小流量或测试租户验证
  → alias 切换
  → 保留旧版本用于回滚
```

任何新 embedding 模型、chunk 策略或融合参数都必须先通过离线评估和删除/权限回归，再切换线上 alias。

## 14. 实施顺序

```text
1. 评估数据和基线
2. PrivateIndexRecord + IndexJob + 私有索引写入
3. Embedding 入库和查询
4. BM25/KNN 分路检索
5. RRF + rerank + 诊断
6. 删除、撤回同意、待复核回归
7. 版本化索引和灰度切换
```

