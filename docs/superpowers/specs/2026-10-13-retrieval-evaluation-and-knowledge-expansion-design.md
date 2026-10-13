# 检索评测与权威知识补全设计

## 目标

建立一套可复现的医疗 RAG 评测流程，真实测量检索质量、拒答能力、引用支持关系和安全不变量；只有在评测确认存在知识缺口时，才补充公开权威医学资料，并用明确的来源元数据和内容哈希重新入库、重新评测。

## 当前真实基线

阶段 1 盘点于 2026-10-13 完成，以下事实来自当前工作区和本地服务：

- 工作树包含大量既有未提交改动，本项目不得重置、覆盖或假设 `HEAD` 是当前运行代码。
- 本地 OpenSearch 2.18.0 可访问；`medical-rag-public-local` 有 30 条文档，`medical-rag-private-local` 有 8 条文档。
- 两个索引都使用 512 维 Lucene `cosinesimil` 向量映射，别名分别为 `medical-rag-public` 和 `medical-rag-private`。
- SQLite `medical_rag.db` 包含 30 条 `knowledge_chunks`、9 个患者文档、9 个 evidence units 和 49 条 outbox events。
- 项目 `.venv` 可导入 SQLAlchemy、OpenSearch 和 FastEmbed；系统 Python 缺少 SQLAlchemy，评测命令必须使用 `.venv/Scripts/python.exe`。
- 现有冻结集 `eval/rag_cases.test.v2.1.jsonl` 包含 120 条样本：quality 70、rejection 30、safety 20；其 manifest SHA-256 为 `ed33014ce05d20b8cf98eb915833183a8e9284691eb996d71be89b9a90c20e5b`。
- 当前 quick 真实运行结果为：纯词法 Recall@3 0.9357、混合 Recall@3 0.9429、混合+门禁 Recall@3 0.7571；门禁开启时拒答准确率为 24/30=0.80，未开启时为 0/30。
- 当前索引包含 WHO、NIH/NHLBI、CDC、MedlinePlus 和一个示例来源。源码目录中的原始知识文件未保留在工作树，不能把索引中的摘要自动当作完整原文。

这些基线只作为阶段 1 取证，不得被当作完成后的最终结论。

## 范围

### 包含

1. 审计现有 dev/test/faithfulness 数据和索引快照。
2. 保留并校验现有冻结 test 集，不反向调参。
3. 为引用评测补充 `gold_claims` 与 `supporting_evidence_ids`。
4. 在 dev 集上扫描候选门禁策略和阈值。
5. 在冻结 test 集上运行 Recall、Precision、MRR、nDCG、拒答和安全评测。
6. 对知识缺口选择公开权威来源，登记来源、版本、日期、URL、适用范围、审核状态和内容哈希。
7. 重新摄取新增来源并记录入库回执，随后重新运行评测。
8. 输出逐用例原始结果、聚合结果、阈值曲线和可复现运行 manifest。

### 不包含

- 不修改或删除现有患者数据。
- 不把搜索摘要、博客、营销页或未经审核的二手内容直接写入公共知识库。
- 不伪造临床 gold 标注；没有临床确认的陈述必须标为待审核。
- 不把 OpenSearch 不可用、embedding 降级或权限拒绝计为“医学证据不足”。
- 不在冻结 test 集上调参。

## 评测数据设计

评测集分为四类：

1. `quality`：问题可回答，包含一个或多个直接支持答案的 gold evidence。
2. `rejection`：当前知识范围不能支持安全回答，期望 `items=[]` 且 `reason=insufficient_evidence` 或由覆盖路由明确拒绝。
3. `safety`：包含跨患者、删除残留、未审核证据等禁止返回项，要求禁止项返回数为零。
4. `citation`：在可回答问题上额外提供 `gold_claims`，每条陈述关联 `supporting_evidence_ids`、陈述类型和人工审核信息。

每条 citation 样本至少包含：

```json
{
  "claim": "每日食盐摄入量建议不超过 5 克。",
  "supporting_evidence_ids": ["..."],
  "claim_kind": "numeric",
  "reviewer": "...",
  "reviewed_at": "2026-10-13",
  "review_method": "manual_evidence_alignment_v1"
}
```

已有 `eval/faithfulness_questions.jsonl` 和 `eval/faithfulness_calibration.jsonl` 先做结构与索引存在性核验；只有标注能绑定到当前索引内容时，才纳入最终引用指标。

## 指标口径

### 检索质量

- `Precision@K = top-K 中相关证据数 / top-K 返回证据数`。
- `Recall@K = top-K 中命中的 gold 证据数 / gold 证据总数`。
- `MRR = 1 / 第一条相关证据的排名`。
- `nDCG@K` 用于衡量相关证据是否排在前面；重复 ID 只能计一次。
- 等价证据组使用 `gold_evidence_groups`，不能把同一医学内容的重复索引记录当成多个独立 gold。

### 拒答

- `正确拒答率 = 正确拒答的 rejection 样本 / rejection 样本总数`。
- `误拒率 = 被拒答的 quality 样本 / quality 样本总数`。
- 单独报告原始拒答率，但不得用它代替正确拒答率。
- 检索不可用、embedding 不可用和越权拒绝必须分别记录原因。

### 引用

- `Citation validity`：引用具备来源、版本和定位，且能解析到当前有效证据。
- `Citation accuracy`：引用实际支持对应陈述的比例。
- `Citation coverage`：被有效证据支持的医学陈述数 / 医学陈述总数。
- 数值、剂量、单位、时间和版本冲突属于高风险项，单独统计。

### 安全和工程

- 越权证据返回数必须为 0。
- 已删除或停用证据残留数必须为 0。
- 未审核证据泄漏数必须为 0。
- 记录 p50/p95 延迟、检索不可用率、embedding 降级率和门禁跳过次数。

## 权威知识补全策略

只有当评测用例代表了系统应支持的范围、但现有知识库确实没有直接支持证据时，才进入补库流程。

来源优先级：

1. WHO、国家卫生主管部门和正式法规/指南。
2. 中华医学会及其他正式医学学会指南或共识。
3. NIH/NHLBI、CDC、MedlinePlus、NICE、FDA 等官方机构资料。

每个来源必须保存：机构、标题、版本或更新时间、发布日期、原始 URL、抓取时间、适用范围、语言、审核人、原文或摘要边界和内容哈希。公开网页摘要只能作为明确标注的摘要来源，不能伪装成完整指南。

补库分支必须使用 `KnowledgeSourceRegistry` 和 `KnowledgeIngestService`，保留 `IngestReceipt`，完成 embedding 后再写入公共索引；来源版本替换和停用必须产生 Outbox 生命周期事件。

## 阈值选择

1. 只在 dev 集扫描候选阈值和门禁策略。
2. 每个阈值同时计算 Recall@K、正确拒答率、误拒率、引用覆盖所需的证据保留率。
3. 选择满足安全底线且 trade-off 可解释的工作点。
4. 锁定阈值、score space、embedding model、index version 和 RRF 参数后，运行冻结 test 集。
5. 任何阈值变化都必须生成新的 calibration artifact 和运行 manifest。

## 交付物

- 数据集审计报告和冻结 manifest。
- citation 标注补充文件或明确的待临床审核清单。
- dev 阈值扫描 JSON 与曲线图。
- test 逐用例结果 JSONL。
- 指标汇总 JSON/Markdown 报告。
- 知识来源登记和入库回执。
- 最终运行 manifest：代码版本、索引别名、物理索引、embedding 模型/维度、分数空间、数据集哈希和命令。

## 验收条件

- 所有最终数字都能由保存的逐用例结果重新计算。
- 冻结 test 集没有被用于调参。
- 评测运行明确区分真实运行、历史产物和未完成项。
- citation 指标只使用已人工确认的 claim/evidence 对。
- 越权、删除残留和未审核泄漏均为零，或明确标记为失败。
- 任何新增医学来源都具备完整来源元数据和可追溯内容哈希。
