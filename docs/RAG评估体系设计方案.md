# RAG 可量化评估体系设计方案

> 评估对象：`C:\Users\lyx\Desktop\RAG`（medical_rag 患者端医疗 RAG 系统）
> 设计目标：为**知识库构建**与**检索环节**两个维度建立可量化、可自动回归、可上线门禁的评估体系
> 设计日期：2026-09-19
> 与现有资产的关系：本方案**不另起炉灶**，而是把 `quality/gate.py`、`observability/metrics.py`、`observability/stage_metrics.py`、`eval/golden.py` 已有的指标原语整合成一套完整、分层、可执行的指标体系，并补齐缺口。
> 现状前提：`docs/知识库搭建评估报告.md` 已确认「评测框架是资产，评测数据尚未建立」——当前 `baseline.json` 的 recall/MRR 全为 1.0 属**构造产物**，不可作为上线依据。本方案的冷启动章节专门解决这一问题。

---

## 0. 总体框架

### 0.1 两层四段模型

```
【维度一 · 知识库构建】原始文档 ──► 解析 ──► 清洗 ──► 分块 ──► 元数据/索引
                         D1-1   D1-2   D1-3   D1-4   D1-5

【维度二 · 检索环节】  query ──► 候选 ──► 融合 ──► 重排 ──► Top-K 上下文
                       D2-1   D2-2   D2-3   D2-4
```

维度一对齐现有写路径（`parsing/service.py` → `quality/gate.py` → `indexing/orchestrator.py`）；维度二对齐现有读路径（`retrieval/service.py`）。两层的公共分母是同一批**证据单元（EvidenceUnit）**，因此构建层的缺陷会沿链路传导到检索层——这也是为什么两层必须分开度量、联合归因。

### 0.2 指标的三类标注属性

| 类别 | 含义 | 成本 | 占比目标 |
|---|---|---|---|
| **A · 全自动** | 纯统计口径，无需任何标注，从代码/索引/日志直接算出 | 零 | ~60% |
| **B · 弱监督半自动** | 需要少量人工种子，其余靠规则/模型/历史结果自举 | 低 | ~25% |
| **C · 必须人工标注** | 依赖语义真值（相关性、语义边界、答案充分性） | 高 | ~15% |

设计原则：**能用 A 类解决的绝不引入 B/C**；C 类标注只用于「校准 A/B 类代理指标」和「守住门禁的关键真值」，不做全量标注。

### 0.3 与现有代码的映射总览

| 现有实现 | 位置 | 本方案中的角色 |
|---|---|---|
| `QualityGate.evaluate` 返回 11 项 metrics | `quality/gate.py:298` | **D1 大部分指标的现成采集点** |
| `_text_coverage` / `_anchor_coverage` / `_page_coverage` | `quality/gate.py:86/250/229` | D1-1 解析完整性 |
| `_duplicate_block_ratio` / `_abnormal_character_ratio` / `_non_empty_block_ratio` | `quality/gate.py:144/127/119` | D1-4 / D1-5 |
| `_hard_split_ratio` / `_table_column_consistency` | `quality/gate.py:271/169` | D1-2 切分质量 |
| `evaluate_retrieval` → `hit_rate/recall_at_k/mrr/ndcg_at_k` | `observability/metrics.py:98` | **D2 排序与召回指标的现成采集点** |
| `permission_violations/deletion_residuals/review_required_leakage` | `observability/metrics.py:135` | D2-5 零容忍安全项 |
| `diagnostics.stage_elapsed_ms`（lexical/vector/fusion/rerank/total） | `retrieval/service.py:420` | D2-6 检索耗时 |
| `PipelineMetricsRecorder`（parse/clean/chunk/extract/gate/embed/write/retrieve） | `observability/stage_metrics.py` | 全链路分阶段埋点 |
| `run_golden_fixture` / `compute_baseline` / `release_gate` | `eval/golden.py` | 评估执行器与门禁 |
| `RolloutGate.compare`（baseline vs candidate） | `observability/metrics.py:221` | 版本 A/B 晋升门禁 |
| `QualityPolicy`（单一阈值来源） | `quality/gate.py:50` | 阈值的唯一归属地 |

**结论**：A 类指标中约 70% 已有采集点，本方案的主要工作量在「建立真实评测集 + 补齐 B/C 类口径 + 把指标接进统一报告」。

---

## 1. 维度一：知识库构建（文档解析与索引）

### 1.1 解析的完整性与准确率（D1-1）

| 指标 | 定义与计算公式 | 达标阈值（稳定期 / 冷启动） | 采集方式 | 标注类别 |
|---|---|---|---|---|
| **文本覆盖率** Text Coverage | 原始内容块非空白字符被证据文本保留的比例：`TC = min(1, Σ sig(evidence_text) / Σ sig(raw content blocks))`；分母排除 `heading` 块（标题不入正文属设计） | ≥0.95 / ≥0.85 | `QualityGate.metrics["text_coverage"]`（已实现） | A |
| **页覆盖率** Page Coverage | `PC = |覆盖的声明页| / page_total`；`page_total` 不可得时**不判定**（fail-closed） | ≥0.98 / 不判定 | `QualityGate.metrics["page_coverage"]`（已实现） | A |
| **块召回率** Block Recall | 解析产出的块集合对真值块集合的覆盖：`BR = |produced_blocks ∩ gold_blocks| / |gold_blocks|` | ≥0.98 / ≥0.90 | Golden fixture 的 `expected_blocks` 比对（`eval/golden.py`） | C（真值块需标注） |
| **字符准确率** 1−CER | `CER = 编辑距离(产出文本, 真值文本) / len(真值文本)`；按文档类型分档（数字型 PDF 更严） | ≥0.99（原生 PDF/Excel）/ ≥0.97（扫描件）/ — | 对齐后的编辑距离计算；需逐字符真值 | C |
| **锚点覆盖率** Anchor Coverage | 带 `source_anchor` 的证据占比：`AC = |有锚点证据| / |证据总数|` | 1.00 / 1.00 | `QualityGate.metrics["anchor_coverage"]`（已实现，阈值 `min_anchor_coverage=1.0`） | A |
| **锚点准确率** Anchor Accuracy | fail-closed：缺失锚点与**虚构**锚点都扣分：`AA = 1 − (|missing| + |invented|) / max(1, |expected|)` | ≥0.99 / 1.00 | `eval/golden.py:_anchor_accuracy`（已实现，锚点由几何信息派生而非复制） | B |
| **表格表头保留率** Table Header Retention | 每个期望表格的表头单元格均出现在某个表格子块中（布尔） | 100% / 100% | `eval/golden.py:_table_header_retention`（已实现） | A |
| **表格列一致性** Table Column Consistency | 每张表内各行单元格数与众数一致的最低比例：`min_over_tables(一致行数/总行数)` | ≥0.80 / ≥0.80 | `QualityGate.metrics["table_column_consistency"]`（已实现） | A |
| **OCR 置信度** OCR Confidence | 可用置信度的最小值；**同时**必须看 `confidence_unavailable_ratio`（不可判定 ≠ 低分） | ≥0.80 且 unavailable=0 / ≥0.80 | `QualityGate.metrics["ocr_confidence"]`（已实现） | A |
| **结构化字段抽取 F1** | 表格→键值对的 `F1 = 2PR/(P+R)`；面向 `<指标名, 值, 单位>` 三元组 | ≥0.90 / 抽取器可暂缓（现仅 1 个指标） | Golden fixture 的 `expected_facts` 比对（已实现 `_fact_match`） | C |

> **采集口径提示**：`_text_coverage` 已经是修正后的端到端口径（原始块 → 最终证据），不再使用恒 ≈1 的 raw↔clean 字符比；新增指标请沿用同一「端到端」原则，避免空转指标。

### 1.2 切分的语义完整性与粒度合理性（D1-2）

| 指标 | 定义与计算公式 | 达标阈值（稳定期 / 冷启动） | 采集方式 | 标注类别 |
|---|---|---|---|---|
| **粒度合规率** Size Compliance | 落在 `[min_tokens, max_tokens]` 的块占比：`SC = |合规块| / |总块|`；**超 max_tokens 零容忍** | ≥0.98 且超限=0 / ≥0.95 | 遍历 `EvidenceUnitDraft` 用 `HeuristicTokenCounter.count` 计；或从索引 `chunk_index` + token 统计 | A |
| **Token 分布** P50/P95/Max | 块 token 数的中位、95 分位、最大值 | P95 ≤ max_tokens(600)、P50 ∈ [300,500] / 同 | 同上 | A |
| **硬切率** Hard Split Ratio | 带 `hard_split` flag 的块占比：`HSR = |hard_split 块| / |总块|` | 0.00（`max_hard_split_ratio=0.0`）/ ≤0.02 | `QualityGate.metrics["hard_split_ratio"]`（已实现，任何硬切触发人工复核） | A |
| **语义边界命中率** Boundary Hit Rate | 人工标注的真实语义边界集合中，被块切分点命中的比例：`BHR = |块边界 ∩ 真值边界| / |真值边界|` | ≥0.90 / — | 人工在样本文档上标注段/句边界，与 `chunk_index` 边界比对 | C |
| **块完整率** Block Integrity | 不以残句开头/结尾的块占比（规则近似：块首不以连接词/逗号起、块尾有终止标点） | ≥0.95 / ≥0.90 | 规则检测（正则 + 标点） | B（规则代理，少量抽检校准） |
| **Overlap 冗余比** | `overlap 字符 / 总字符`，衡量上下文衔接成本 | 0.08 ~ 0.15（配置 `overlap_ratio=0.12`）/ 同 | 比较相邻块公共前缀长度（`_bounded_overlap_prefix` 产出） | A |
| **父子块覆盖度** Parent Coverage | 带 `parent_block_id` 的块占比（决定上下文扩展是否生效） | ≥0.60（目标：普通段落也建父子）/ 仅表格+超长块 | 遍历 `EvidenceUnitDraft.parent_block_id` | A |
| **跨页语义保持率** | 因按 `(page, sheet)` 分组而被切断的跨页段落占比 | ≤0.10 / — | 对比"按页分组 vs 全文档分块"的块结构差异 | B |

> **现状提醒**：`parent_block_id` 目前仅表格子块（`semantic_chunker.py:322/342`）与超长块（`:253`）赋值，普通累积块为 `None`（`:236`）。这正是「父子块覆盖度」要持续观测的原因——它决定了 Task 9 上下文扩展的真实覆盖面。

### 1.3 元数据与索引覆盖度（D1-3）

| 指标 | 定义与计算公式 | 达标阈值（稳定期 / 冷启动） | 采集方式 | 标注类别 |
|---|---|---|---|---|
| **section_path 覆盖率** | 带非空 `section_path` 的块占比 | ≥0.95 / ≥0.85 | 遍历证据的 `section_path` | A |
| **引用完整率** Citation Completeness | `source_id/title/version/location` 四要素齐全的引用占比（fail-closed） | 1.00 / 1.00 | `RetrievalService._has_citation`（已实现） | A |
| **可检索率** Retrieval-eligible Rate | `PASS` 文档占比（`retrieval_eligible=True`） | ≥0.90 / ≥0.70 | `QualityGate` 决策分布统计（`QualityDecision.status`） | A |
| **待复核率 / 失败率** | `NEEDS_REVIEW` / `FAIL` 占比 | REVIEW ≤0.08，FAIL ≤0.02 / REVIEW ≤0.30，FAIL ≤0.05 | 同上（按 `QualityFailureCode` 聚合 Top 原因） | A |
| **索引写入成功率** | 成功写入的证据数 / 计划写入数：`WSR = succeeded / attempted` | ≥0.999 / ≥0.99 | `WriteReceipt`（`OpenSearchWriteError` 已携带 attempted/succeeded/failed） | A |
| **向量化成功率** | 成功 embedding 的块占比 | ≥0.999 / ≥0.99 | `PipelineMetricsRecorder.record_embedding`（已实现） | A |
| **版本指纹完整率** | `parser/cleaner/chunker/extractor/embedding/index` 六指纹齐全的证据占比 | 1.00 / 1.00 | 证据与索引记录的版本字段非空校验 | A |
| **幂等重放无重复** | 同一 `_idempotency` 键重放不产生新增文档 | 1.00 / 1.00 | 重放同一 outbox 事件，比对索引 doc 数 | A |

### 1.4 重复与冗余内容比例（D1-4）

| 指标 | 定义与计算公式 | 达标阈值（稳定期 / 冷启动） | 采集方式 | 标注类别 |
|---|---|---|---|---|
| **精确重复块比例** Duplicate Block Ratio | 文本完全相同的块占比：`DBR = 出现>1次的块数 / 总块数` | ≤0.10 / ≤0.25（现阈值） | `QualityGate.metrics["duplicate_block_ratio"]`（已实现） | A |
| **近重复块比例** Near-dup Ratio | SimHash 汉明距离 ≤3 的块对占比 | ≤0.05 / ≤0.15 | 对块文本做 SimHash/MinHash，计算桶内碰撞率 | A |
| **索引内重复证据率** | 相同文本对应多个证据 id 的比例 | ≤0.02 / ≤0.05 | 按文本哈希聚合索引 doc 数 | A |
| **跨文档重复率** | 同一内容出现在多份文档中的比例（公共知识常见） | ≤0.10 / — | 文档级 SimHash 聚类 | B |

> **权衡说明**：表格分块会在每个子块重复表头、指标聚焦块会重复匹配行（`semantic_chunker.py:290`），这属于**设计性重复**（保证子块自洽），统计时应对 `EvidenceKind.REPORT_TABLE` 单列，不与噪声重复混算。

### 1.5 噪声及无效内容的处理（D1-5）

| 指标 | 定义与计算公式 | 达标阈值（稳定期 / 冷启动） | 采集方式 | 标注类别 |
|---|---|---|---|---|
| **异常字符比例** Abnormal Char Ratio | `\ufffd` + 控制/格式类字符（`Cc/Cf`）非结构化空白占比 | ≤0.005 / ≤0.02（现阈值） | `QualityGate.metrics["abnormal_character_ratio"]`（已实现） | A |
| **非空块比例** Non-empty Block Ratio | 非空白块占比 | ≥0.95 / ≥0.90（现阈值） | `QualityGate.metrics["non_empty_block_ratio"]`（已实现） | A |
| **模板行残留率** Boilerplate Residue | 漏网的页眉/页脚/水印行占比 | ≤0.01 / ≤0.03 | 规则检测（高频重复短行）+ 人工抽检校准 | B |
| **无效块比例** | 纯标点/空白/单字符等无信息块占比 | ≤0.005 / ≤0.02 | 正则规则 | A |
| **门禁拦截有效性** | `FAIL` 文档中确实无可用内容的比例（查准）；`PASS` 文档确实合格的比例（查全） | 查准 ≥0.95，查全 ≥0.90 / — | 分层抽样人工复核门禁判定 | C（校准门禁本身） |

---

## 2. 维度二：检索环节

### 2.1 Top-K 命中率与召回/准确率（D2-1）

| 指标 | 定义与计算公式 | 达标阈值（稳定期 / 冷启动） | 采集方式 | 标注类别 |
|---|---|---|---|---|
| **命中率** Hit Rate@K | Top-K 中**至少含一个** gold 证据的比例：`HR@K = 1[∃ top_i ∈ gold]`，对 case 集合取均值 | ≥0.95 / ≥0.80 | `evaluate_retrieval` → `hit_rate`（已实现）；case 取自评测集 | C（需 gold_evidence_ids） |
| **召回率** Recall@K | `Recall@K = |top_K ∩ gold| / min(K, |gold|)`；分母用 `min(K,|gold|)` 保证「找齐可及相关项即满分」 | ≥0.90 / ≥0.70 | `evaluate_retrieval` → `recall_at_k`（已实现） | C |
| **查准率** Precision@K | `P@K = |top_K ∩ gold| / K` | ≥0.70 / ≥0.50 | 由 gold 与返回列表计算（新增，`evaluate_retrieval` 扩展） | C |
| **证据充分率** Evidence Sufficiency | 回答该 query 所需**最小证据集**被 Top-K 完全覆盖的比例（区别于 Recall：这是"够不够答"而非"找回多少"） | ≥0.90 / ≥0.70 | case 增加 `required_evidence_ids` 字段；`1[required ⊆ top_K]` | C（需额外标注"最小充分集"） |

> **阈值校准提示**：`evaluate_retrieval` 的 `k` 默认取传入列表长度（历史行为）。作为门禁指标时必须**显式传 `k=top_k`**，否则「把所有候选都返回」会让召回率虚高——这正是当前 `_rag_metrics_for_fixture` 构造出 1.0 的机制，务必避免重演。

### 2.2 排序合理性（D2-2）

| 指标 | 定义与计算公式 | 达标阈值（稳定期 / 冷启动） | 采集方式 | 标注类别 |
|---|---|---|---|---|
| **MRR** 平均倒数排名 | `MRR = mean(1 / rank_of_first_gold)` | ≥0.80 / ≥0.60 | `evaluate_retrieval` → `mrr`（已实现） | C |
| **nDCG@K** | 二元相关性折扣累积增益：`DCG = Σ 1[rel] / log2(rank+1)`，`nDCG = DCG / IDCG`；重复 id 只计一次（防刷分） | ≥0.80 / ≥0.60 | `evaluate_retrieval` → `ndcg_at_k`（已实现，含去重） | C |
| **MAP@K** 平均精度均值 | `AP = Σ (P@i × 1[top_i ∈ gold]) / min(K,|gold|)`，对 case 取均值 | ≥0.75 / ≥0.55 | 新增（在 `evaluate_retrieval` 中扩展） | C |
| **Top-1 命中率** | 排名第 1 即为 gold 的比例 | ≥0.70 / ≥0.50 | 由返回列表首元素判定 | C |
| **分级排序质量** | 若标注多级相关性（0/1/2），用 nDCG@K 多级版本；或报告 Spearman 秩相关 | ≥0.75 / — | 需多级标注，可选进阶 | C |

### 2.3 上下文覆盖率与冗余度（D2-3）

| 指标 | 定义与计算公式 | 达标阈值（稳定期 / 冷启动） | 采集方式 | 标注类别 |
|---|---|---|---|---|
| **上下文覆盖率** Context Coverage | Top-K 证据文本覆盖回答所需信息点的比例（信息点级）：`CC = |覆盖的信息点| / |总信息点|` | ≥0.90 / ≥0.70 | 人工/LLM 将答案拆为信息点，逐点判定是否被某证据覆盖 | C（LLM 预标 + 人工抽检） |
| **上下文冗余度** Context Redundancy | Top-K 块两两相似度的均值（或去重后字符压缩率）：`CR = 1 − |去重字符| / |总字符|` | ≤0.15 / ≤0.25 | 块间 SimHash / 最长公共子串 / 去重比 | A |
| **相邻块重叠度** | Top-K 命中块间的 overlap 占比（来自分块 overlap） | ≤0.20 / ≤0.30 | 块间公共前缀检测 | A |
| **上下文 token 预算合规** | Top-K 拼接后 token ≤ 模型窗口且未被截断 | 100% / 100% | 拼接后计 token，与预算比对 | A |
| **量-质平衡** | 有效信息密度 = 命中 gold 字符数 / 上下文总字符数 | ≥0.60 / ≥0.40 | gold 标注 + 字符统计 | B |

### 2.4 噪声文档占比（D2-4）

| 指标 | 定义与计算公式 | 达标阈值（稳定期 / 冷启动） | 采集方式 | 标注类别 |
|---|---|---|---|---|
| **不相关命中率** | Top-K 中与 query 无关的证据占比：`1 − P@K` | ≤0.30 / ≤0.50 | 由标注 gold 计算 | C |
| **越权命中数** Permission Violations | Top-K 中出现不可访问证据的次数（**零容忍**） | 0 | `evaluate_retrieval` → `permission_violations`（已实现） | A |
| **删除残留数** Deletion Residuals | Top-K 中出现已删除证据的次数（**零容忍**） | 0 | `evaluate_retrieval` → `deletion_residuals`（已实现） | A |
| **待复核泄漏数** Review Leakage | Top-K 中出现 `NEEDS_REVIEW`（`retrieval_eligible=False`）证据的次数（**零容忍**） | 0 | `evaluate_retrieval` → `review_required_leakage`（已实现） | A |
| **降级/空返回率** | `insufficient_evidence` + `retrieval_unavailable` 占请求比例 | ≤0.10 / ≤0.30 | `RetrievalResult.reason` 分布（`_SAFE_REASONS` 已白名单） | A |

> **安全项优先级最高**：三个零容忍项任一非零，`release_gate` 与 `RolloutGate` 都应**无条件判失败**（现有实现已如此：`eval/golden.py:781`、`observability/metrics.py:251`）。

### 2.5 检索耗时（D2-6）

| 指标 | 定义与计算公式 | 达标阈值（稳定期 / 冷启动） | 采集方式 | 标注类别 |
|---|---|---|---|---|
| **端到端检索 P50/P95/P99** | 单次 `RetrievalService.retrieve` 的墙钟耗时分位 | P95 ≤ 800ms，P99 ≤ 1.5s / P95 ≤ 1.5s | `diagnostics.stage_elapsed_ms["total"]`（已实现） | A |
| **分阶段耗时** | lexical / vector / fusion / rerank 各阶段 P95 | 单路 ≤300ms，fusion ≤50ms，rerank ≤200ms / 宽松 2× | `diagnostics.stage_elapsed_ms`（已实现，词法/向量并行） | A |
| **超时率 / 降级率** | 超过 SLA 或触发降级（如 embedding 不可用转纯词法）的比例 | ≤0.005 / ≤0.02 | `diagnostics.embedding_degraded` / `lexical_error` / `vector_error` | A |
| **索引写入/更新耗时** | 单文档入库端到端耗时分位 | P95 ≤ 30s / — | `PipelineMetricsRecorder` 的 parse→write 阶段 | A |
| **P95 回归容忍** | 相对基线的劣化倍数（避开时钟抖动） | ≤3×（现 `P95_REGRESSION_FACTOR`）/ 同 | `release_gate`（已实现） | A |

---

## 3. 冷启动阶段：低成本启动方案

现状是「评测框架完整、评测数据为零」。以下**五步递进**路径按成本从低到高，每一步都能独立产出可用结论，不必等前一步完美。

### 阶段 0 · 零标注代理指标（可立即执行，成本≈0）

**做什么**：先用 A 类指标跑通全链路基线，完全不需要标注数据。
- **构建层**：文本覆盖率、页覆盖率、锚点覆盖率、重复块比例、异常字符比例、非空块比例、硬切率、门禁状态分布（PASS/REVIEW/FAIL 及 Top 失败原因）。
- **检索层**：检索耗时（P50/P95/P99）、安全三项（越权/删除/复核泄漏，应为 0）、空返回率、降级率。
- **价值**：立即回答「管道是否健康、有无系统性缺陷」。`docs/知识库搭建评估报告.md` 指出的锚点/覆盖率类问题即可在这一步持续监控。
- **落地**：复用 `PipelineMetricsRecorder`，对现有真实文档（哪怕只有几十份）跑一遍，产出第一版 `metrics.json`。

### 阶段 1 · 小规模真实种子集（成本低，1–2 人日）

**做什么**：选 **30–50 份**代表性文档 + **50–100 条** query，只标注**最小必要真值**：
- 文档级：`gold_blocks`（关键段落定位）、`expected_anchors`（页码/sheet/range）。
- query 级：`gold_evidence_ids`（相关证据）、`required_evidence_ids`（回答该问题的最小充分集）、`forbidden_evidence_ids`（同义干扰项，用来测噪声）。
- **覆盖策略**：按文档类型（PDF 原生/扫描、DOCX、XLSX）、query 类型（事实型/数值型/趋势型/是否型）、难度（易/中/难）**分层抽样**，每格至少 5 例。
- **迭代替换**：保留现有合成 fixture 作为**解析/分块回归测试**（它们对解析器旁路，但能守住分块与门禁逻辑）；真实种子集用于**检索质量评测**。
- **关键改造**：把 `eval/golden.py:_rag_metrics_for_fixture` 的「模拟检索（按阅读顺序全量返回）」替换为**真实调用 `RetrievalService.retrieve`**，并显式传 `k=top_k`。这是让 recall/MRR 从「数学必然的 1.0」变为「真实测量值」的**唯一关键改动**。

### 阶段 2 · 弱监督自举（成本中，降低 5–10× 标注量）

**做什么**：不再从零标注，改为**人工只做校验**：
- 用当前系统对大量 query 生成 Top-K 候选；
- 人工只对每个候选做**二元判断**（相关/不相关），或对明显错误做**一键修正**；
- 用 RRF 融合分数 + 人工判断训练一个轻量**相关性分类器**，反推其余未标注对。
- **主动学习**：优先标注「模型分数接近阈值」和「多路检索结果分歧大」的样本，同样标注量下信息量最高。
- **成本估算**：相比全量从零标注，二元校验的标注速度约快 3–5 倍，分歧采样再省 30–50% 样本。

### 阶段 3 · LLM-as-judge 辅助 + 人工校准（成本中，适合规模化）

**做什么**：用 LLM 预判「证据是否支撑回答」「答案信息点是否被覆盖」，人工只抽检 **10%** 校准。
- **必报一致性**：计算 LLM 判断与人工判断的 **Cohen's kappa**，要求 ≥0.70 才可用于自动评测；低于则退回阶段 2。
- **适用边界**：适合「上下文覆盖率」「信息点覆盖」这类语义判定；**不适合**替代零容忍安全项（越权/删除必须用确定性规则，见 `_is_authorized`）。
- **防漂移**：judge 模型版本需与 `eval_dataset_version` 一同写入 `CalibrationArtifact`，模型变更即失效重标。

### 阶段 4 · 线上隐式反馈（成本低，长期滚动）

**做什么**：把线上真实使用转化为持续评测信号（注意隐私合规，仅记录 id/计数，不落原文）：
- 引用点击率、回答被追问率、转人工/拒答率、点赞点踩；
- 无 gold 但有**相对信号**（A/B 版本同一 query 的偏好）；
- 采样进入 shadow 集，定期人工复核形成新标注。

---

## 4. 可落地的评估流程与迭代建议

### 4.1 数据集分层治理

| 数据集 | 用途 | 更新频率 | 是否门禁 |
|---|---|---|---|
| **dev set** | 快速迭代、调参、调试 | 随需增补 | 否 |
| **test set（封存）** | 上线门禁，**绝不用于调参** | 季度重标，版本冻结 | 是 |
| **regression set（合成 fixture）** | 解析/分块逻辑回归 | 随代码变更 | 是（本地/CI） |
| **shadow set（线上采样）** | 真实分布监控、主动学习 | 持续 | 否（监控告警） |

**红线**：任何一次迭代都不得在 test set 上调参后直接上线——否则门禁失去意义。评测集必须带 `eval_dataset_version`，与 `CalibrationArtifact.eval_dataset_version` 绑定。

### 4.2 评估执行流程（闭环）

```
① 数据集版本化        eval_dataset_version 冻结 → 分 dev/test
        │
② 离线评测            run_golden_fixture / evaluate_retrieval（真实检索）
        │              产出：BaselineReport（构建+D2 全指标）
        ▼
③ 门禁（构建层）      release_gate：锚点准确率、文本覆盖、零容忍安全项、
        │              真实 recall/MRR/nDCG 不得低于已存 baseline；P95 容忍 3×
        ▼
④ 门禁（版本层）      RolloutGate.compare(baseline, candidate)：
        │              质量三项不得回归 + 安全项必须为 0 → promote/hold/rollback
        ▼
⑤ 线上监控            PipelineMetricsRecorder + 隐式反馈，异常自动告警
        │
⑥ 反馈闭环            失败 case / 用户反馈 → 归因（构建层 or 检索层）→ 进入 dev
```

### 4.3 归因方法（两层联合定位）

当检索指标下降时，按以下顺序定位根因：

1. **先查安全项**：越权/删除/复核泄漏非零 → 权限链路问题，与排序无关，优先级最高。
2. **再查构建层**：若 `可检索率` 下降或 `PDF/扫描件` 分档的文本覆盖率下降 → 是**内容没进库**，不是检索变差。
3. **对比分档指标**：按 `file_type` / `parser_version` / `chunker_version` 维度切分（`PipelineMetricsRecorder` 已带这些维度），定位是哪类文档拖低整体。
4. **最后查检索层**：在 gold 已进库的前提下，Hit 高但 nDCG/MRR 低 → **排序问题**；Hit 也低 → **召回问题**（候选生成/门禁阈值）。
5. **门禁诊断**：检查 `diagnostics.gate_policy` 是否为 `TOP_K`（未校准）——未校准时 `top_k` 截断会被误读为低召回。

### 4.4 迭代节奏

| 周期 | 动作 |
|---|---|
| **每次代码改动（CI）** | 跑 regression set + 合成 fixture；构建层 A 类指标全绿才允许合并 |
| **每个版本发布** | 跑 test set 全量 → `release_gate` + `RolloutGate`；未过门禁禁止上线 |
| **每周** | 线上指标看板巡检：耗时 P95、空返回率、降级率、门禁状态分布漂移 |
| **每月** | 从 shadow 集主动学习采样，补充/修正标注，重算基线 |
| **每季度** | test set 全量重标（防标注老化 + 语料分布漂移），更新 `eval_dataset_version` 与 `CalibrationArtifact` |

### 4.5 达标路线图（建议）

| 里程碑 | 构建层目标 | 检索层目标 |
|---|---|---|
| **M1（当前→2 周）** | 阶段 0 代理指标全量上线，门禁状态分布可观测 | 耗时 + 安全三项接入监控 |
| **M2（1 个月）** | 真实种子集建立，`_rag_metrics_for_fixture` 替换为真实检索 | Hit@K ≥0.80，Recall@K ≥0.70（冷启动阈值） |
| **M3（2–3 个月）** | 文本覆盖 ≥0.95，重复率 ≤0.10，可检索率 ≥0.90 | Hit@K ≥0.95，nDCG@K ≥0.80，P95 ≤800ms |
| **M4（季度）** | 生成并审核 `CalibrationArtifact`，门禁升级 `TOP_K`→`CALIBRATED` | 上线 A/B 常态化，RolloutGate 接入发布流程 |

---

## 附录 A：指标注册表（可直接转配置/代码）

```yaml
# metrics_registry.yaml —— 单一阈值来源，与 QualityPolicy / 门禁共用
version: "1.0"
layers:
  build:   # 维度一
    - id: text_coverage
      name: 文本覆盖率
      formula: sum(sig(evidence))/sum(sig(raw_blocks))
      target: {stable: 0.95, cold_start: 0.85}
      direction: higher
      source: QualityGate.metrics.text_coverage
      label: auto
    - id: anchor_coverage
      formula: has_anchor/total_evidence
      target: {stable: 1.0, cold_start: 1.0}
      source: QualityGate.metrics.anchor_coverage
      label: auto
    - id: hard_split_ratio
      formula: hard_split_chunks/total_chunks
      target: {stable: 0.0, cold_start: 0.02}
      direction: lower
      source: QualityGate.metrics.hard_split_ratio
      label: auto
    - id: duplicate_block_ratio
      formula: duplicated_blocks/total_blocks
      target: {stable: 0.10, cold_start: 0.25}
      direction: lower
      source: QualityGate.metrics.duplicate_block_ratio
      label: auto
    - id: abnormal_char_ratio
      formula: abnormal_chars/total_chars
      target: {stable: 0.005, cold_start: 0.02}
      direction: lower
      source: QualityGate.metrics.abnormal_character_ratio
      label: auto
    - id: block_recall
      formula: |produced ∩ gold|/|gold|
      target: {stable: 0.98, cold_start: 0.90}
      source: eval.golden expected_blocks
      label: manual
  retrieval:  # 维度二
    - id: hit_rate_at_k
      target: {stable: 0.95, cold_start: 0.80}
      source: evaluate_retrieval.hit_rate
      label: manual
    - id: recall_at_k
      target: {stable: 0.90, cold_start: 0.70}
      source: evaluate_retrieval.recall_at_k
      label: manual
    - id: ndcg_at_k
      target: {stable: 0.80, cold_start: 0.60}
      source: evaluate_retrieval.ndcg_at_k
      label: manual
    - id: mrr
      target: {stable: 0.80, cold_start: 0.60}
      source: evaluate_retrieval.mrr
      label: manual
    - id: permission_violations
      target: {stable: 0, cold_start: 0}
      direction: lower
      zero_tolerance: true
      source: evaluate_retrieval.permission_violations
      label: auto
    - id: deletion_residuals
      target: {stable: 0, cold_start: 0}
      direction: lower
      zero_tolerance: true
      source: evaluate_retrieval.deletion_residuals
      label: auto
    - id: review_required_leakage
      target: {stable: 0, cold_start: 0}
      direction: lower
      zero_tolerance: true
      source: evaluate_retrieval.review_required_leakage
      label: auto
    - id: p95_latency_ms
      target: {stable: 800, cold_start: 1500}
      direction: lower
      regression_factor: 3.0
      source: diagnostics.stage_elapsed_ms.total
      label: auto
```

## 附录 B：需新增的采集埋点清单

| 缺口 | 建议实现 |
|---|---|
| Precision@K / MAP@K | 扩展 `evaluate_retrieval`，返回 `precision_at_k` / `map_at_k`（复用现有 `top`/`gold` 计算） |
| 证据充分率 | `EvaluationCase` 增加 `required_evidence_ids` 字段，评测时判定 `required ⊆ top_K` |
| 粒度合规率 / Token 分布 | 新增纯函数模块（如 `observability/size_metrics.py`），对 `EvidenceUnitDraft` 列表统计 |
| 近重复块比例 | 新增 SimHash 工具（可用标准库 `hashlib` + 分桶），或引入 `datasketch` |
| 上下文冗余度 | 在 `RetrievalService.retrieve` 返回后做块间去重比统计 |
| 块完整率 / 模板行残留率 | 新增规则检测（正则 + 标点），输出到 `QualityGate.metrics` |
| 真实检索评测 | 改造 `eval/golden.py:_rag_metrics_for_fixture` → 调用真实 `RetrievalService`，显式 `k=top_k` |
| 统一报告 | 新增 `eval/report.py`：聚合构建层 + 检索层指标，输出 `BaselineReport` 扩展版 |

---

## 附录 C：一句话总结

**构建层守「内容是否完整、干净、可索引地进库」，检索层守「给定问题是否准确、有序、低噪、快速地取回」；两层共享同一批证据单元，用 A 类代理指标零成本起步、用真实种子集校准排名质量、用零容忍安全项兜底，把已有的 `QualityGate` / `evaluate_retrieval` / `PipelineMetricsRecorder` 从「框架」变成「可上线依据」。**
