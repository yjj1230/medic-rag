# RAG Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用真实 OpenSearch、真实 RetrievalService 和真实回答样本，建立一套可复现、可解释、不会把小样本误判为达标的 RAG 评估流程。

**Architecture:** 评估按数据/索引、检索、拒答与分流、安全、答案忠实度、性能六层拆开。每一层先产生逐样本明细，再计算聚合指标；安全项采用零容忍规则，质量指标同时报告点估计和置信区间，最终报告保留“通过 / 未通过 / 样本不足 / 未覆盖”四种状态。

**Tech Stack:** Python 3.12 bundled runtime, `scripts/retrieval_tuning_lab.py`, `scripts/retrieval_rejection_threshold_sweep.py`, `scripts/faithfulness_eval.py`, OpenSearch 2.18, JSONL evaluation cases, Markdown/JSON artifacts。

**Spec:** `docs/RAG评估体系设计方案.md`、`docs/RAG评估入门指南.md`、`docs/superpowers/specs/2026-10-06-faithfulness-evaluation-harness-design.md`

## Global Constraints

- 所有检索指标必须真实调用 `RetrievalService.retrieve`，禁止使用会把所有候选全量返回的模拟检索路径。
- public/private 索引必须使用真实访问上下文，跨患者、跨租户、删除记录和复核记录不能混用身份。
- 安全违规任一非零即判失败；没有真实负样本时只能标记“未覆盖”，不能写成通过。
- quality 样本量小于 30 时只报告置信区间，不宣布达标或未达标。
- 评估集必须分为 dev 和 test；test 用例在完成标注后封存，调参不能反复使用 test。
- 医疗回答进入外部判官前必须脱敏；没有人工校准 artifact 时，faithfulness 只报告区间和 `undecidable`。
- 当前 bundled Python 路径为 `C:\Users\lyx\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`。

---

### Task 1: 固化环境与当前基线

**Files:**
- Read: `scripts/retrieval_tuning_lab.py`
- Read: `eval_out/retrieval_eval_results.json`
- Create: `eval_out/evaluation_run_manifest.json`

**Interfaces:**
- Consumes: 当前 OpenSearch public/private 索引、bundled Python 运行时。
- Produces: 带时间、索引、模型、用例数量、命令行参数和依赖版本的基线清单。

- [ ] **Step 1: 检查服务与索引可达性**

```powershell
Invoke-RestMethod http://127.0.0.1:9200
```

确认返回 OpenSearch 版本，并记录 `medical-rag-public-local` 与 `medical-rag-private-local` 的文档数。

- [ ] **Step 2: 运行快速基线**

```powershell
& 'C:\Users\lyx\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts/retrieval_tuning_lab.py --quick --rounds 5
```

预期：输出 5 个核心配置；若出现 `embedding_unavailable`，先修复评估运行时，不得把退化结果当成向量检索结果。

- [ ] **Step 3: 运行稳定基线**

```powershell
& 'C:\Users\lyx\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts/retrieval_tuning_lab.py --rounds 25
```

预期：输出完整配置矩阵和每个配置的 P50/P95/P99；保存 `eval_out/retrieval_eval_results.json`。

- [ ] **Step 4: 写入运行清单**

把当前日期、OpenSearch 版本、索引文档数、模型名称、维度、case_count、latency_rounds、git commit 和 Python 依赖版本写入 `eval_out/evaluation_run_manifest.json`。

- [ ] **Step 5: 判断基线状态**

只有以下信息齐全才进入下一步：真实向量链路可用、结果文件可解析、每个用例有逐样本返回 ID、延迟样本数符合命令行参数。

### Task 2: 审计评估语料和标注口径

**Files:**
- Read: `eval_out/retrieval_eval_results.json`
- Read: `tests/eval/cases.jsonl`
- Create: `eval/rag_cases.dev.jsonl`
- Create: `eval/rag_cases.test.jsonl`
- Create: `eval_out/corpus_audit.json`

**Interfaces:**
- Consumes: OpenSearch `_source`、当前真实索引 ID、领域标注结果。
- Produces: 去重后的语料审计、可答/拒答/安全三类 case、每条 case 的 gold evidence 和 forbidden evidence。

- [ ] **Step 1: 记录语料健康度**

至少记录：文档总数、唯一 `content_hash` 数、重复组大小、乱码记录、缺少 citation 的记录、`deleted=true` 数量、`retrieval_eligible=false` 数量。

- [ ] **Step 2: 清理 dev/test 边界**

`dev` 用于调参，`test` 只用于最终报告。每条 JSONL 至少包含：`id`、`question`、`scope`、`gold_evidence_ids`、`forbidden_evidence_ids`、`expected_status`、`category`。

- [ ] **Step 3: 扩充问题分布**

目标至少 50 条 dev、100 条 test，覆盖：直接事实、同义改写、数值查询、多证据问题、组合问题、拼写错误、超长问题、知识库外问题、跨患者/跨租户问题、删除/复核负样本。

- [ ] **Step 4: 做标注一致性复核**

随机抽取 20 条由第二人复核；若 gold evidence 或 expected status 不一致，先修标注，不调整检索器迎合错误标签。

### Task 3: 评估检索质量

**Files:**
- Use: `scripts/retrieval_tuning_lab.py`
- Update: `eval_out/retrieval_eval_results.json`
- Generate: `eval_out/parameter_comparison.md`

**Interfaces:**
- Consumes: `eval/rag_cases.dev.jsonl` 或当前实验台 cases。
- Produces: 每个配置的 Hit Rate、Recall@K、Precision@K、MRR、nDCG、上下文冗余和延迟。

- [ ] **Step 1: 先看 Hit Rate 和 Recall**

确认正确证据是否被找回；不要先看 Precision，因为当 `|gold| < K` 时 P@K 会被分母口径封顶。

- [ ] **Step 2: 再看 MRR 与 nDCG**

MRR 用于判断第一条正确证据是否靠前；nDCG 用于判断多条证据整体排序是否接近理想排序。

- [ ] **Step 3: 对比检索策略**

至少比较 lexical、vector、hybrid，并固定同一份 case、同一访问上下文、同一 `top_k`。

- [ ] **Step 4: 对比参数**

分别测试 `top_k`、`candidate_limit`、RRF `k`、rank gate；每次只改变一个变量，避免无法归因。

- [ ] **Step 5: 记录失败样本**

对每个 Recall 或 MRR 失败样本保存 query、gold ID、returned IDs、候选来源、分数和阶段诊断，形成可修复的问题清单。

### Task 4: 评估拒答、问题分流和急症优先级

**Files:**
- Use: `scripts/retrieval_rejection_threshold_sweep.py`
- Use: `scripts/casual_routing_live_acceptance.py`
- Create: `eval_out/rejection_and_routing_report.md`

**Interfaces:**
- Consumes: cross-domain、医学相关但知识库无覆盖、边界模糊、急症和 casual queries。
- Produces: 正确拒答率、误拒答率、边界漏答、急症召回率、非医疗误分流率。

- [ ] **Step 1: 建立拒答混淆矩阵**

区分：应答且应答、应答但误拒、应拒但回答、应拒且拒答。

- [ ] **Step 2: 运行余弦阈值对比**

```powershell
& 'C:\Users\lyx\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts/retrieval_rejection_threshold_sweep.py
```

同时记录可答问题是否保留 gold evidence，不能只追求更高拒答率。

- [ ] **Step 3: 单独评估急症分流**

急症 case 使用召回率作为硬指标；漏掉急症必须单独列为高优先级失败，不能被平均分掩盖。

### Task 5: 评估权限、安全和数据生命周期

**Files:**
- Read: `src/medical_rag/retrieval/service.py`
- Read: `src/medical_rag/auth/policy.py`
- Create: `eval/security_cases.jsonl`
- Create: `eval_out/security_report.json`

**Interfaces:**
- Consumes: 不同患者、租户、文档状态和复核状态的真实身份上下文。
- Produces: 权限违规、删除残留、复核泄漏、跨索引泄漏、PHI 暴露结果。

- [ ] **Step 1: 构造隔离负样本**

在专用评估索引中创建 `deleted=true` 和 `retrieval_eligible=false` 记录，并用不同患者/租户请求检索；不得污染正式索引。

- [ ] **Step 2: 执行零容忍检查**

任一 forbidden evidence 出现在返回结果中即失败；记录 query、身份、returned ID 和泄漏类型。

- [ ] **Step 3: 检查 PHI 评估边界**

外部 judge 只接收脱敏后的答案和证据；未脱敏时只使用本地规则判定。

### Task 6: 评估答案正确性、引用和 Faithfulness

**Files:**
- Use: `scripts/faithfulness_eval.py`
- Read: `eval_out/faithfulness_answers.json`
- Create: `eval/faithfulness_calibration.jsonl`
- Generate: `eval_out/faithfulness_eval.json`

**Interfaces:**
- Consumes: 真实 `ChatOrchestrator.answer()` 结果、检索证据、人工校准标签。
- Produces: 答案级 faithfulness、声明级支持率、弃权率、矛盾率、引用准确率和置信区间。

- [ ] **Step 1: 先做规则重放**

```powershell
& 'C:\Users\lyx\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts/faithfulness_eval.py --source replay --no-judge
```

规则模式只判可机械验证的声明，不把无法判定的声明当作错误或正确。

- [ ] **Step 2: 建立人工校准集**

从答案中分层抽取至少 30 条声明，人工标记 `entailed`、`contradicted`、`insufficient`；覆盖 numeric、causal、applicability、other 四类。

- [ ] **Step 3: 检查引用准确性**

逐句确认引用 ID 存在、来源属于当前证据集合、引用内容支持该句；“有引用”不能直接等于“引用准确”。

- [ ] **Step 4: 只有校准通过后才使用 judge**

报告 judge 与人工标签的一致性；如果样本量、校准或弃权率不满足要求，结论保持 `undecidable`。

### Task 7: 汇总报告和回归门禁

**Files:**
- Create: `docs/RAG评估报告-YYYY-MM-DD.md`
- Create: `eval_out/rag_release_gate.json`

**Interfaces:**
- Consumes: Task 1–6 的所有 JSON/Markdown 产物。
- Produces: 分层结论和下一轮回归基线。

- [ ] **Step 1: 按四态输出结论**

每个维度只能标记：`pass`、`fail`、`undecidable`、`not_covered`。

- [ ] **Step 2: 设置硬门槛**

权限泄漏、删除残留、复核泄漏、急症漏分流任一非零，release gate 失败；小样本质量指标不强行判定。

- [ ] **Step 3: 保存回归基线**

固定 test 集、索引快照、embedding 模型、prompt 版本、judge 版本和运行环境；以后每次修改只与该基线比较。

- [ ] **Step 4: 形成下一轮行动清单**

每个失败样本必须对应到一个可能修复点：语料、切块、检索、门禁、分流、提示词或生成模型，禁止只写“继续优化”。

## 预期的第一次评估结论

基于当前已有结果，第一次正式报告应诚实写成：

- 检索排序：当前混合检索优于单路检索，但样本量不足以确认稳定达标。
- 拒答：余弦阈值 0.52 优于无门禁，但仍存在边界漏答。
- 权限：已有跨患者/跨租户用例未发现泄漏；删除和复核负样本尚未覆盖。
- Faithfulness：答案样本和人工校准不足，当前只能报告 `undecidable`。
- 语料：私有索引重复、乱码和样本贫瘠会限制结论可信度。
