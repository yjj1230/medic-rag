# 检索门禁优化与失败用例归因设计

日期：2026-10-13

## 背景

冻结的 `eval/rag_cases.test.v2.1.jsonl` 已在真实 OpenSearch 和本地 BGE 512 维模型上完成复评。当前混合检索在无门禁时 Recall@3 为 0.9429，启用余弦门禁后 Recall@3 降至 0.7571，拒答准确率为 0.80。逐条诊断发现：

1. OpenSearch 实际 `embedding` 映射为 `space_type=cosinesimil`，但 `OpenSearchStore` 将向量分数标记为 `l2_inv`，门禁诊断和校准身份不一致。
2. 门禁把词法候选限制在通过向量阈值的 ID 集内，却没有把向量候选限制在词法交集内；向量独有候选仍可进入 RRF 融合。
3. 单一绝对阈值同时承担“可答证据准入”和“无关查询拒答”，导致 10 条可答用例被误拒，同时仍有 6 条无关用例被接纳。

## 目标

- 让门禁阈值的数值空间、索引映射和运行时诊断完全一致。
- 保留真正同义表达的向量独有命中能力，同时降低低置信度向量独有命中造成的拒答失败。
- 通过失败用例归因报告区分召回缺失、门禁误拒、门禁误接纳、标注问题和基础设施问题。
- 只使用 dev 集扫描基础阈值与向量独有阈值；冻结 test 集只做最终确认。
- 不读取或写入 PHI 到评测汇总，不修改知识库内容。

## 非目标

- 不在本阶段新增医学来源或重建 OpenSearch 索引。
- 不引入 LLM 作为拒答判定器。
- 不改变现有 ACL、删除、来源活跃性和引用完整性规则。
- 不把 test 集结果用于阈值选择。

## 设计

### 1. 分数空间身份

`OpenSearchStore` 在构造时保存 `vector_score_space`，默认值改为与当前物理索引一致的 `cosinesimil`。启动或评测初始化时读取 `embedding` mapping 的 `method.space_type`；当物理映射、配置的 `score_space` 和返回记录的 `vector_score_space` 不一致时，严格门禁返回 `calibration_artifact_mismatch`，而不是继续比较不同单位的数值。

诊断字段继续包含：

- `vector_score_space`
- `gate_score_space`
- `mapping_score_space`
- `score_space_match`

报告只保存这些身份和分数，不保存正文或向量。

### 2. 两级候选门禁

校准工件新增可选字段 `vector_only_cosine_threshold`。当字段缺失时，向量独有候选使用基础 `cosine_threshold`，保持旧工件兼容；新工件必须显式记录该字段。

对通过 ACL、生命周期和引用检查的候选：

1. 向量分数低于基础阈值的候选被移除。
2. 向量分数达到基础阈值且同时存在词法候选中的候选，进入 RRF。
3. 只有向量命中的候选，必须达到 `vector_only_cosine_threshold` 才能进入 RRF。
4. 词法独有候选不能绕过向量门禁。

这样既保留“词法没有重合但语义确实相关”的候选，也不会让一个刚刚越过基础阈值的向量孤立命中直接触发回答。

诊断新增：

- `vector_only_candidates`
- `vector_only_gated_out`
- `lexical_vector_intersection`
- `vector_only_gate_threshold`

### 3. 失败归因

新增 `scripts/retrieval_failure_attribution.py`，输入冻结/开发评测结果和对应 case 文件，输出只含 case ID、类别、gold ID 哈希、返回 ID、门禁诊断和归因标签：

- `retrieval_miss`
- `gate_false_reject`
- `gate_false_accept`
- `annotation_issue`
- `insufficient_corpus`
- `permission_or_lifecycle`
- `infrastructure_failure`

归因规则优先使用真实返回原因和诊断；无法自动判定的 case 标记为 `manual_review`，不会自动改写 gold 标签。

### 4. 阈值扫描

`scripts/retrieval_threshold_sweep.py` 扩展为二维扫描：

- 基础阈值：`0.68` 到 `0.82`，步长 `0.02`
- 向量独有阈值：基础阈值到 `0.90`，步长 `0.02`

每个工作点记录 Recall@3、Precision@3、MRR、nDCG@3、拒答准确率、answerable 误拒率、安全违规数、向量独有候选数和延迟。选择规则：

1. 安全违规必须为零。
2. 在 dev 集上优先降低拒答误接纳和 answerable 误拒。
3. 若多个点相同，选择较低基础阈值和较低向量独有阈值，以降低过度拒答风险。

选定点写入新的校准工件，并绑定当前 embedding 模型、索引版本、分数空间和 dev 数据集哈希。

## 数据流

```text
真实 OpenSearch mapping
        │
        ▼
分数空间身份校验 ──不一致──> calibration_artifact_mismatch
        │
        ▼
词法候选 + 向量候选
        │
        ▼
基础阈值 ──> 词法/向量交集 ──> RRF
        │             ▲
        └─向量独有阈值──┘
                      │
                      ▼
             ACL/引用/生命周期过滤
                      │
                      ▼
                top-k 证据
```

## 错误处理

- 无法读取 mapping：严格门禁返回 `calibration_artifact_mismatch`，并记录 `mapping_unavailable`。
- mapping 与工件分数空间不一致：拒绝继续门禁，不退化为无门禁。
- 向量服务不可用：沿用现有 `vector_path_unavailable` 降级语义，跳过门禁并显式告警。
- 校准工件缺少新字段：按兼容规则将向量独有阈值回退到基础阈值，并在诊断中记录 `vector_only_threshold_fallback=true`。
- 失败归因无法确定：标记 `manual_review`，不得自动修改评测集。

## 测试策略

单元测试覆盖：

- `cosinesimil` mapping 身份被正确传播。
- mapping 与工件空间不一致时 fail-closed。
- 词法+向量交集候选通过基础阈值。
- 向量独有候选按更高阈值通过或被拒绝。
- 词法独有候选不能绕过门禁。
- 失败归因对误拒、误接纳、召回缺失和基础设施失败分类正确。
- 二维阈值扫描排序、分母和安全计数稳定。

真实验证顺序：

1. 新增聚焦测试。
2. dev 二维扫描。
3. 选定点运行冻结 test。
4. `tests/eval`、`tests/test_rejection_gate.py` 和完整 pytest。
5. ruff 检查新增和修改文件。

## 成功标准

- 运行时报告不再出现 `gate_score_space=cosinesimil` 与 `vector_score_space=l2_inv` 的身份冲突。
- dev 集安全违规保持零。
- 失败归因报告覆盖全部 120 条 test case，未自动改写任何 gold 标签。
- 最终报告同时给出门禁前后指标、二维阈值选择依据、失败类别计数和真实索引身份。
