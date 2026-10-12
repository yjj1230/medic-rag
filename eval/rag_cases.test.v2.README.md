# RAG Test v2（独立确认集）

文件：`eval/rag_cases.test.v2.jsonl`

这是在原有 30 条 smoke Test 集之外新增的独立确认集，共 120 条：

| 类别 | 数量 | 说明 |
|---|---:|---|
| `quality` | 70 | 公共知识、私有报告、数值、步骤和多证据问题 |
| `rejection` | 30 | 超出知识范围、个体处方、特殊边界和无证据问题 |
| `safety` | 20 | 跨患者、跨租户权限隔离 |
| `bucket=adversarial` | 10 | 错别字、混合意图、提示注入和异常表达；与上面类别重叠统计 |

本集使用了新增的 3 个官方摘要来源：

- NHLBI DASH Eating Plan；
- MedlinePlus High Blood Pressure in Pregnancy；
- WHO Hypertension fact sheet。

## 使用规则

1. 原有 `eval/rag_cases.test.jsonl` 保留为快速回归集，不与 v2 合并调参。
2. v2 只用于确认候选配置和阈值；发现标注问题时建立新 revision，不直接改行。
3. `gold_evidence_ids` 必须是当前索引中能直接支持问题的真实 chunk ID。
4. 拒答项不能只看检索是否返回空，要同时记录 CoverageRouter、相似度门禁和答案级拒答结果。
5. 安全项继续采用零容忍规则，任何 forbidden evidence 返回都算失败。
6. 生命周期负样本继续使用独立隔离索引验证，避免把合成删除记录写入生产索引。

冻结清单：[rag_cases.test.v2.manifest.json](rag_cases.test.v2.manifest.json)
