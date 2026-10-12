# 方案 B：事件驱动统一索引平台设计

## 目标

在保留现有模块化单体、ParsingService、EmbeddingPipeline 和 RetrievalService 的前提下，把索引流程升级为可恢复、可对账、可灰度发布的生产级系统。

## 核心原则

1. PostgreSQL 是业务事实源，OpenSearch 是可重建的派生索引。
2. 公共知识和患者私有报告使用独立物理索引、独立 ACL 路径和独立 writer。
3. 数据库状态、索引任务和 Outbox 事件在同一事务中提交。
4. Embedding、索引版本、解析版本和生命周期状态必须随记录持久化。
5. 删除、失效和待复核状态必须 fail-closed；失败清理必须可重试且可审计。
6. 没有真实评估证据时，不声称质量提升，也不自动切换生产 alias。

## 目标链路

```text
业务写入
  → Document / Evidence / IndexJob / Outbox 同事务提交
  → IndexOrchestrator 领取任务
  → 解析状态与生命周期校验
  → EmbeddingPipeline 批量向量化
  → PublicIndexWriter 或 PrivateIndexWriter bulk upsert/delete
  → IndexJob 成功 + AuditEvent
  → reconciliation 定期对账
```

## 统一索引记录

公共和私有记录共享 `IndexRecord` 字段：record/document/version/evidence 标识、visibility、tenant/patient ACL、text、source anchor、content hash、parser version、embedding model/version/dimension、index version、retrieval_eligible、source_active、deleted 和时间戳。公共记录必须显式写入 `visibility=public`、`retrieval_eligible=true`、`source_active=true`、`deleted=false`。

## 任务与可靠性

`IndexJob` 使用 `PENDING → LEASED → EMBEDDING → WRITING → SUCCEEDED` 状态；可重试错误进入 `RETRYABLE_FAILED`，超过次数进入 `PERMANENT_FAILED`。任务拥有幂等键、attempts、lease、next_retry_at、error_code 和版本元数据。Outbox 支持重复投递，Writer upsert/delete 必须幂等。

## 生命周期

文档删除、版本替换、证据待复核、公共知识源停用都产生显式生命周期事件。删除先写数据库 tombstone，再删除对象、派生数据和索引；任一层失败不得记录完成事件。待执行的旧任务遇到删除状态时只能执行物理清理，不能重新写入 tombstone 记录。

## 检索与发布

BM25/KNN 分路召回后先做索引层和服务层 ACL，再做 RRF、校准门控和 rerank。阈值必须绑定索引版本、Embedding 版本、RRF 参数和评估集版本。新索引先离线评估、权限回归和测试租户验证，再由外部操作切换 alias。

## 运行前置条件

生产启动必须配置 private writer、public writer、EmbeddingPipeline、ConsentService 和审计上下文；缺失时拒绝启动或拒绝相关能力。测试可以使用显式 fake adapter，但不能默默放宽生产安全约束。

## 验收指标

权限越权率、删除残留率、待复核召回率和索引重复率为 0；IndexJob 状态和失败原因可追踪；真实脱敏语料 Recall@K、MRR、引用完整率不低于基线；普通问答 P95 不超过 8 秒；alias 切换可回滚。
