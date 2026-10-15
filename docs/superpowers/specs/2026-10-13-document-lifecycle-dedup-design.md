# 文档生命周期、去重与增量更新设计

## 目标

补齐患者上传文档的文件级去重、逻辑文档版本管理和 Chunk 级增量索引能力，同时保留历史版本、审计记录和失败回滚能力。

## 当前缺口

- `UploadService.create_document()` 每次上传都生成新的 `Document.id`、`DocumentVersion.id` 和 `ParseJob.id`，没有文件哈希去重。
- 上传路径固定创建 `version_number=1`，没有“同一逻辑文档的新版本”接口。
- 公共知识库已经在 `KnowledgeIngestService.ingest()` 中按 `source_id + content_hash` 做 Chunk 级增量更新，但患者私有文档重新解析会整体替换派生证据和索引任务。
- `Document.storage_key` 位于文档主表，无法完整保存同一逻辑文档的多版本原文件。

## 设计原则

1. 完全相同的文件必须幂等，不重复解析、向量化和索引。
2. 相同逻辑文档的修订版本必须保留历史，不能覆盖旧版本。
3. 只有解析、质量门禁和索引全部成功的新版本才能成为当前版本。
4. 新版本中消失的 Chunk 必须停止检索；失败更新不得影响当前可用版本。
5. 哈希、版本和索引身份必须由数据库唯一约束保护，不能只依赖应用层先查后写。
6. 患者私有数据的租户和患者范围必须包含在去重和更新条件中。

## 领域模型变化

- `Document` 表示逻辑文档，增加 `current_version_id`，保留当前状态和患者/租户归属。
- `DocumentVersion` 表示具体文件快照，增加 `storage_key`、`file_hash`、`file_size_bytes`、`is_current`、`superseded_at`，并保留 `parser_version` 和处理状态。
- `EvidenceUnit` 增加 `content_hash` 或 `chunk_fingerprint`，用于同一逻辑文档相邻版本的 Chunk 差异比较。
- `EmbeddingCache`（新表）按 `chunk_fingerprint + embedding_model + embedding_dimensions` 唯一，缓存可复用向量。
- `ParseJob`、`IndexJob` 和 Outbox 事件继续按 `document_version_id` 关联，保持现有异步和幂等模型。

## 更新语义

- 完全相同文件：返回已有文档/版本，响应状态为 `duplicate`。
- 同一逻辑文档的新文件：创建递增版本，旧版本保留但不再作为当前版本检索。
- 不同报告：创建新的逻辑文档；不能仅依据文件名合并。
- 更新失败：新版本进入 `failed` 或 `needs_review`，旧的 `current_version_id` 保持不变。

## 版本激活

新版本先进入 `building`/`indexing` 状态。所有解析、质量和索引任务成功后，在一个数据库事务内切换：

```text
旧版本 is_current=false
新版本 is_current=true
Document.current_version_id=新版本
```

OpenSearch 查询只允许 `is_current=true`、`retrieval_eligible=true`、未删除的证据进入结果。
