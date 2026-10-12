# 忠实度评估 Harness（原子陈述 + LLM-as-judge + kappa 校准）— 设计文档

> 受理日期：2026-10-06
> 前置文档：`docs/superpowers/notes/2026-10-06-rag-eval-rigor-guide.md`（五环节方法论；忠实度列为「生成」层回归门禁）
> 关联既有实现：`src/medical_rag/chat/citations.py:CitationValidator`（规则层 ①②③）、
> `src/medical_rag/observability/significance.py`（区间原语）、`scripts/casual_routing_live_acceptance.py`（真链路验收范本）
> 关联缺口：`docs/RAG项目专业评审报告-2026-09-28.md` 忠实度项标 🟠中；方法论 §7 执行清单未含忠实度

---

## 1. 背景

### 1.1 现状：忠实度**无法测量**

四层校验栈中，项目已实现三层，全部是**规则**（`CitationValidator`）：

| 层 | 判什么 | 实现 | 能力边界 |
|---|---|---|---|
| ① 引用结构 | 每句是否以引用标记收尾；段落实质长度是否达标 | `_check_sentence_citations` / `_check_paragraph_coverage` | 只看**有没有**引用，不看引用对不对 |
| ② 数值一致性 | 数值是否出现在**本句所引**证据里；单位是否一致 | `_check_numeric_consistency` | 只能判"数值抄没抄错"，判不了文字断言 |
| ③ 定量断言 | 有测量数值时，定量结论是否复述了数值 | `_check_quantitative_claims` | 只覆盖含数值证据的场景 |
| ④ **语义支持** | 证据**在语义上**是否支持该断言 | ❌ **缺失** | — |

`citations.py:470-472` 的 docstring 已把边界写死：

> 注意：本方法只保证「引用了存在且可定位的证据」并且不违反结构与数值约束。
> 它**不**证明证据文本在语义上支持该断言 —— 那属于事实一致性评估（质检集）的职责。

**这句话就是本设计要补的那一半。** 现状下，"证据被引用"与"证据支持该说法"之间存在一个**结构上无法弥合的缺口**：

| 幻觉形态 | 现有三层能否拦住 | 原因 |
|---|---|---|
| 引用不存在的 ID | ✅ 拦 | ① 存在性校验 |
| 数值抄错（105 → 160） | ✅ 拦 | ② 逐句数值核对 |
| 数值单位写错 | ✅ 拦 | ② 单位归一化比对 |
| 有数值证据却下无数值结论（"完全正常"） | ✅ 拦 | ③ 定量断言 |
| **数值正确但结论张冠李戴** | ❌ **漏** | 数值在证据里出现过即通过 |
| **引了 e2，说法只能由 e1 支撑** | ⚠️ **部分漏** | ② 只做数值级绑定，非数值断言不绑定 |
| **因果/适用条件编造（"与缺铁有关"）** | ❌ **漏** | 无任何规则可判 |
| **证据只提到 A，回答推广到 A+B** | ❌ **漏** | 语义蕴含问题 |

### 1.2 为什么必须用**模型**判而不能继续加正则

第 ④ 层判的是**语义蕴含**，不是字符串模式：

| 证据原文 | 模型回答 | 语义上是否支持 | 正则能判吗 |
|---|---|---|---|
| `血红蛋白 105 g/L（参考区间 115–150）` | `105 低于参考区间` | ✅ 支持 | ❌ 无字面重合（"低于"≠"115-150"） |
| `血红蛋白 105 g/L（参考区间 115–150）` | `105 处于正常范围` | ❌ 不支持 | ❌ 无字面冲突（都是数字 105） |
| `缺铁性贫血常见于育龄女性` | `您的贫血与缺铁有关` | ❌ 不支持 | ❌ 看似相关 |

第二行是**最危险的一类**：数值全对、引用存在、结构合规，唯一的问题是**结论反了**。现有三层对它完全无感——而这正是临床上最不能容忍的错误。

结论：**第 ④ 层无规则解**，必须引入 LLM-as-judge。而 LLM-as-judge 引入了一个新问题：**judge 自己会不会错？** 所以本设计的核心不是"接个模型打分"，而是**把 judge 本身做成可校准、可 pin、可 fail-closed 的组件**——这与项目在余弦门禁用 `CalibrationArtifact` + `calibration_artifact_mismatch` 收口的思路**完全同构**。

### 1.3 为什么现在做

1. **它是五环节里唯一"规则不可达"的一环**：①数据集治理靠人工、②真实链路靠脚本、③口径靠计算、⑤门禁靠配置，都是确定性工作；④忠实度是唯一必须引入**概率性判官**的一环，工程复杂度最高，也最容易被"随便接个 RAGAS 就上线"糊弄过去。
2. **它决定 P0-5 类问题的可见性**：`docs/superpowers/notes/2026-10-05-p0-5-reproduction-and-rootcause.md` 的核心教训是"单测全绿 ≠ 真实链路可用，校验器假阳性只有真实模型样本能暴露"。忠实度 harness 是**唯一能量化这个假阳性率**的工具。
3. **它的样本量与成本都不高**：一次真实链路跑 N 条答案（N=20 已可产出 n≈60–100 条原子陈述），judge 调用次数 = 陈述数，可缓存复用。相比"把评估集扩到 300 条"，这是**当前语料规模下唯一能先做起来的生成层质量指标**。

---

## 2. 目标与非目标

### 2.1 目标

- **G1** 定义**原子陈述 schema**（`AtomicClaim`）与**判定 schema**（`ClaimVerdict`），并把"答案文本 → 原子陈述列表"的切分**复用** `CitationValidator` 的既有切句与豁免规则（不新造第二套口径）。
- **G2** 实现**逐句证据绑定**的判定：每条原子陈述**只**交给它所在句所声明的证据判定，禁止使用全局证据池。
- **G3** 实现两个判官并在同一 schema 下组合：
  - **规则判官**（零成本、确定性）：只能对"数值类"陈述给出绝对判定，其余一律**弃权**（`supported=None`）；
  - **LLM 判官**：语义蕴含，输出严格 JSON，允许弃权。
- **G4** 实现 **kappa 校准流程**：judge 与人工标注的一致性核算脚本 + 校准产物（`FaithfulnessCalibration`），kappa 未达标**不得**用于门禁。
- **G5** 实现**门禁三态判定**（`pass` / `fail` / `undecidable`），复用 `significance.py` 的区间原语；`n < 30` 只报区间不判定。
- **G6** 真实链路答案生成与判定**解耦**：答案落盘后可反复重判（改 judge prompt 不必重调模型）。
- **G7** 全部落地物**离线**：不接入请求路径、不改诊断白名单、不动 `.env`、不改任何生产返回值。

### 2.2 非目标（本次明确不做，及其原因）

- **不做** 在线门禁挂载。`faithfulness_gate_enabled` 不新增——本设计只做"离线测量 + 校准"，**接入 CI 门禁需单独 spec**（涉及生成层回归门禁的基线定义，与 `release_gate` 的既有行为耦合）。
- **不做** 完整性问题（completeness / 答案相关性）。忠实度只答"说的都属实吗"，**不答"该说的说了吗"**——一个全部 supported 的答案可以是完全无用（只复述了证据里最无关的一句）。后者依赖 `required_evidence_ids`（方法论 §1.2，尚未标注）。
- **不做** 拒答正确性。`total == 0` 的答案记 **N/A**，由既有的拒答正确率（→ Wilson）单独衡量，**绝不混入忠实度**。
- **不做** 多 judge 投票 / 交叉验证。单 judge + 弃权率约束是本次范围；多 judge 一致性作为遗留（§8）。
- **不新增** 诊断白名单键。本 harness 纯离线，**刻意不碰** `_ANSWER_DIAGNOSTIC_KEYS`——该项目有"两处白名单漂移"前科，离线工具不应增加同步面。
- **不引入** RAGAS / DeepEval 等新依赖。理由见 §5.5：它们给不出本设计需要的**逐句绑定 + 弃权 + pin** 语义，且会新增 `[project].dependencies` 条目（受 `test_declared_dependencies.py` 契约约束）。

---

## 3. 总体架构

```
                    ┌── 阶段 2（昂贵，需真实模型网关）
                    │   scripts/faithfulness_answers.py
                    │       真 ChatOrchestrator → N 条答案
                    ▼       落盘 eval_out/faithfulness_answers.json
        ┌───────────────────────────────────────┐
        │  answers.json（答案 + 逐句引用 + 证据） │  ← 生成与判定解耦的接缝
        └───────────────────────────────────────┘
                    │  阶段 1/3（可反复重跑，判定与生成无耦合）
                    ▼
   scripts/faithfulness_eval.py
        │
        ├─► src/medical_rag/eval/faithfulness.py    ← 本设计新增（schema + 切分 + 判官编排）
        │        AtomicClaim / ClaimVerdict / FaithfulnessResult
        │        decompose(...)  ← 复用 CitationValidator 的切句与豁免
        │        judge_rule(...) ← 数值类绝对判定，其余弃权
        │        judge_llm(...)  ← 语义蕴含，严格 JSON，可弃权
        │
        ├─► src/medical_rag/eval/judge_prompt.py    ← 本设计新增（prompt 与版本号）
        │
        ├─► src/medical_rag/observability/significance.py  ← 复用（Interval / wilson / mean_ci）
        │
        ▼
   eval_out/faithfulness_eval.json      ← 逐陈述判定 + 区间 + 三态
   eval_out/faithfulness_calibration.json ← kappa 与校准结论（供门禁 pin）

   （校准，独立入口）
   scripts/faithfulness_calibrate.py  ← 人工标注 vs judge → Cohen's kappa → 产物
```

**分层理由**：

| 层 | 位置 | 为什么 |
|---|---|---|
| schema + 判官 | `src/medical_rag/eval/faithfulness.py` | 纯逻辑、可单测、不依赖模型网关（judge 以可注入的 callable 形式传入） |
| prompt | `src/medical_rag/eval/judge_prompt.py` | prompt 与版本号**同文件**，避免"prompt 改了但 artifact 里版本没动" |
| 编排 | `scripts/` | 真实 I/O、真网关、真 OpenSearch 都在这一层，与项目既有 `scripts/*_acceptance.py` 一致 |
| 校准 | 独立脚本 | 校准是**批次性人工活动**，不应混进每次评估执行 |

**依赖影响**：仅复用标准库与既有内部模块，**不新增 `[project].dependencies` 条目**。

---

## 4. 核心数据契约

### 4.1 原子陈述 `AtomicClaim`

```python
ClaimKind = Literal["numeric", "trend", "causal", "applicability", "definition", "other"]
"""陈述类型。判官分工与门禁硬度都按它分派。"""

@dataclass(frozen=True, slots=True)
class AtomicClaim:
    claim_id: str                  # 确定性 ID：f"{sentence_index}:{ordinal}"，见 §4.1.1
    text: str                      # 原子陈述原文（已去引用标记）
    kind: ClaimKind                # 陈述类型，由 §5.2 分类器给出
    sentence_index: int            # 该陈述所属句在答案中的序号（0-based）
    sentence_text: str             # 所属句原文（**含**引用标记，用于审计）
    cited_ids: tuple[str, ...]     # **该句声明的**证据 ID（逐句绑定，非全局池）
```

#### 4.1.1 为什么 `claim_id` 必须确定性

LLM judge 的调用是**可重试**的：超时重试、降级重试、断点续判都要求"同一条陈述在两次运行里拿到同一个 ID"。`uuid4` 会让同一份答案在两次运行中产生不同 ID → 判定结果无法对齐、缓存无法命中、人工复核无法指认。

```python
# 确定性且可读：句序号 + 句内陈述序号
claim_id = f"s{claim.sentence_index}c{ordinal}"
```

**禁止**把 claim 文本的 hash 当 ID：文本微调（模型非确定性）会整批换 ID，破坏跨轮对比。

#### 4.1.2 分母口径（🔴 本设计最易错处）

| 排除项 | 依据 | 为什么 |
|---|---|---|
| 免责 / 建议句 | 复用 `CitationValidator._NON_CLAIM_SENTENCE` | "请咨询医生"不是事实断言，计入分母会**稀释**幻觉率 |
| 结构行（`## 标题` / `- 列表`） | 复用 `_is_structural_line` | 标签不承载医学结论 |
| 列举引导句（`…包括：`） | 复用 `_is_enumeration_lead_in` | 语法上是引导而非断言 |
| 无实质内容的句子 | 去标记后为空 | 同上 |

> ⚠️ **禁止按段落计数**。一句"血红蛋白 105 g/L，低于参考区间 [e1]"包含 **2 条**原子陈述，按段落算只记 1 条 → 幻觉被摊薄一半。这是忠实度评测最典型的作弊点。

**复用而非新造**：本设计**不重写**豁免规则，直接调用 `CitationValidator` 的既有成员（`sentences()` / `_NON_CLAIM_SENTENCE` / `_is_structural_line` / `_strip_markers()`）。

理由与 `chat/casual_screen.py` 的 `_HIGH_RISK_CLAIMS is CitationValidator._high_risk_claims` 别名做法一致：**口径漂移是隐性缺陷源**，已在非医学分流中出现过"两处白名单漂移"事故。切片规则若出现第二份实现，忠实度的分母就会与生产校验器的判定不一致。故以**同一对象别名**方式复用（`is` 断言锁定）。

### 4.2 判定 `ClaimVerdict`

```python
VerdictCode = Literal[
    "entailed",            # 证据蕴含该陈述
    "not_entailed",         # 证据在语义上不支持（**含**明确冲突与"证据未提"）
    "contradicted",         # 证据与该陈述直接冲突（"105 处于正常范围" vs 参考区间 115–150）
    "no_evidence_bound",    # 该句未绑定任何证据（结构性缺陷，非判官结论）
    "insufficient",         # 判官弃权：证据不足/表述含混，无法判定
    "judge_error",          # 判官调用失败或输出不可解析（重试后仍失败）
]

@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    claim_id: str
    code: VerdictCode
    judge: str             # "rule" | f"llm:{model}@{prompt_version}"
    rationale: str | None  # 一句话依据（审计用；LLM 判官必填，规则判官为 None）

    @property
    def supported(self) -> bool:
        return self.code == "entailed"
```

**为什么把 `contradicted` 与 `not_entailed` 分开**：前者是"证据说了反话"（数值反了、区间反了），后者是"证据没提"。二者在安全上**严重程度不同**——`contradicted` 是**主动错误**，`not_entailed` 是**无据推断**。门禁对前者可零容忍，对后者较宽（§5.6）。

**为什么 `insufficient` 与 `judge_error` 分开**：前者是**判官的诚实弃权**（正常且必要），后者是**工程故障**。混在一起会让"judge 挂了"伪装成"证据不足"——正是 §1.3 教训里那类"假阴性被静默"的形态。

### 4.3 结果 `FaithfulnessResult`

```python
@dataclass(frozen=True, slots=True)
class FaithfulnessResult:
    answer_id: str
    claims: tuple[AtomicClaim, ...]
    verdicts: tuple[ClaimVerdict, ...]

    @property
    def supported(self) -> int: ...      # code == "entailed" 计数
    @property
    def total(self) -> int: ...          # 参与分母的陈述数（**不含**弃权与判官错误）
    @property
    def abstained(self) -> int: ...      # insufficient 计数
    @property
    def errored(self) -> int: ...        # judge_error 计数
    @property
    def value(self) -> float | None: ... # total == 0 → **None**，见 §4.3.1
```

#### 4.3.1 🔴 `total == 0` 必须返回 `None`，不是 `1.0`

| 情形 | 错误做法 | 正确做法 |
|---|---|---|
| 纯拒答（`insufficient_evidence`） | 记 1.0 | **`None`（N/A），从聚合中剔除** |
| 全免责句的答案 | 记 1.0 | **`None`** |
| 判官全部弃权 | 记 1.0 或 0.0 | **`None`** |

**记 1.0 会奖励"什么都不说"**——一个拒答一切的系统的忠实度会是满分。这与显著性工具里"零容忍项在无语料时应标 UNKNOWN 而非 PASS"（方法论 §1.5）是同一条原则：**"测不出问题"不等于"没问题"**。

---

## 5. 设计细节

### 5.1 切分：`decompose()`

```python
def decompose(
    answer_text: str,
    cited_evidence: Mapping[str, str],   # evidence_id -> 证据正文
    *,
    validator: CitationValidator | None = None,
) -> tuple[AtomicClaim, ...]
```

流程：

```
答案文本
  → validator.sentences()              复用既有切句（含「纯标记片段归并前句」）
  → 逐句：
      ├─ _is_structural_line       → 跳过（不产生陈述）
      ├─ is_negated_assertion      → 跳过（否定/转述句不构成断言）
      ├─ _NON_CLAIM_SENTENCE       → 跳过（免责/建议句）
      ├─ _is_enumeration_lead_in   → 跳过（列举引导句）
      ├─ _strip_markers(sentence)  → 得到纯正文
      └─ 按 §5.2 切成原子陈述，附句内引用的 cited_ids
```

**逐句绑定实现**（直接复用既有成员，保证与生产校验器口径一致）：

```python
cited_ids = tuple(
    dict.fromkeys(
        validator.citation_pattern.findall(sentence)
    )
)  # 该**句**声明的证据，不是全答案的 cited_evidence
```

> 🔴 **禁止回退到全局池**。`_sentences_with_evidence()` 在句内无引用时会用全部被引证据**兜底**——该兜底是为"引言句也要参与数值核对"设计的，**不适用于忠实度判定**：忠实度在无绑定证据时的正确结论是 `no_evidence_bound`（结构性缺陷），不是"用别的证据替它辩护"。这是本设计**刻意不复用** `_sentences_with_evidence()` 的唯一一处。

### 5.2 陈述切分与分类（`kind` 判定）

按**能否被单独证伪**切分，而非按标点：

| 切分信号 | 示例 | 切出 |
|---|---|---|
| 逗号连接的两个独立事实 | `血红蛋白 105 g/L，低于参考区间` | 2 条 |
| 分号 / 顿号并列的同类事实 | `常见于乏力；也可见于头晕` | 2 条 |
| 主从因果 | `因缺铁导致，故血红蛋白偏低` | 2 条（因果 1 + 数值 1） |

**分类器**（规则，白名单式，第一条命中即定）：

| `kind` | 触发（复用既有正则） | 判官分工 |
|---|---|---|
| `numeric` | 命中 `_numbers()` **或被** `_QUANTITATIVE_JUDGEMENT` 命中 | **规则判官绝对判定** + LLM 复核 |
| `trend` | 命中 `_QUANTITATIVE_JUDGEMENT` 但无具体数值（偏低/升高） | LLM 判官 |
| `causal` | 命中因果词表（`相关` / `导致` / `引起` / `因为` / `提示`） | LLM 判官（**门禁加严**） |
| `applicability` | 命中条件/人群词（`适用` / `建议` / `适用于` / `情况下`） | LLM 判官 |
| `definition` | 陈述为"X 是什么/Y 是指" | LLM 判官 |
| `other` | 兜底 | LLM 判官 |

**分类器必须偏宽地判成"医学断言"**：宁可把免责句误切成断言（多算分母，忠实度被低估），不可把断言误判为免责（豁免出分母，忠实度被高估）。**低估是保守的，高估是危险的**——与非医学分流的"词表故意偏宽不可收窄"同一取向。

### 5.3 规则判官 `judge_rule()`

```python
def judge_rule(
    claim: AtomicClaim,
    evidence_texts: Mapping[str, str],
) -> ClaimVerdict | None     # None = 弃权（交由 LLM 判官）
```

**只有 `kind == "numeric"` 的陈述会得到判定**，且判定是**绝对**的：

| 情形 | 判定 | 依据 |
|---|---|---|
| 陈述中的每个数值都能在本句证据里定位（`_same_value`）且单位一致 | `entailed` | 复用 `_check_numeric_consistency` 的同一比较逻辑 |
| 任一数值无法定位 | `contradicted` | 数值级主动错误（"105 说成 160"） |
| 单位不一致 | `contradicted` | 同上 |

其余 `kind` **一律返回 `None`（弃权）**。

> 🔴 **规则判官不得对非数值陈述"猜"**。它无法判"与缺铁有关"是否被支持——若强行返回 `not_entailed`，会把正确回答误判为幻觉，污染 kappa。**弃权是它的正确行为，不是缺陷。**

**与 `CitationValidator` 的关系**：规则判官**不是**校验器的重复实现，而是**同一逻辑的第二个消费者**。校验器把数值不符作为**拦截**（raise），判官把它作为**测量**（记 `contradicted`）。二者必须共用比较函数（`_same_value` / `_normalize_unit`），否则会出现"校验器放过了、判官判它错"的自相矛盾——**以 `is` 断言锁定共用**。

### 5.4 LLM 判官 `judge_llm()`

```python
def judge_llm(
    claim: AtomicClaim,
    evidence_texts: Mapping[str, str],
    *,
    gateway: ModelGateway,
    prompt_version: str,
    max_attempts: int = 2,
) -> ClaimVerdict
```

**调用边界（安全承重）**：

| 传入 judge 的内容 | 理由 |
|---|---|
| ✅ **该条原子陈述文本** | 判定对象 |
| ✅ **该句所绑定证据的正文**（仅这些） | 判定依据；**逐句绑定** |
| ❌ 完整答案 | 防止跨句信息泄漏（模型可借别句内容为当前句辩护，等于退回全局池） |
| ❌ 未引用的检索候选池 | 同上；且会把"检索到了但没引"的文档算作依据 |
| ❌ 患者标识 / 会话元数据 | 最小必要原则；判官不需要知道"这是谁" |
| ❌ 本轮 judge 的历史输出 | 防锚定 |

**输出契约**：严格 JSON，**单字段 + 可选理由**，禁止自由文本：

```json
{"verdict": "entailed|not_entailed|contradicted|insufficient", "reason": "一句话"}
```

**解析与降级**：

| 情形 | 处置 |
|---|---|
| JSON 可解析且 `verdict` 在白名单内 | 记对应 `code` |
| JSON 可解析但 `verdict` 不在白名单 | 重试；仍失败 → `judge_error` |
| 输出含 Markdown 代码围栏 | 剥离围栏后解析（已知模型习惯） |
| 输出不可解析 | 重试至 `max_attempts`；仍失败 → `judge_error` |
| 网关超时 / 不可达 | 重试；仍失败 → **抛错终止整轮**（§7） |

> **为什么"网关不可达"要终止而非记 `judge_error`**：方法论 §2.1 铁律 4——"不可达时显式失败，不静默跳过"。全轮 `judge_error` 会让忠实度低到 0 而看不出是故障。**一个静默降级的评估器比没有评估器更危险。**

### 5.5 为什么不用 RAGAS / DeepEval（决策记录）

| 需求 | RAGAS 的默认行为 | 本设计为何自造 |
|---|---|---|
| **逐句绑定** | 全局答案 vs 全局上下文池 | RAGAS 不保证"这条陈述只由它自己引的证据判定"；这正是本项目 `CitationValidator` 花了整轮 T8 修的东西 |
| **弃权语义** | 无；必须给 0/1 | 无 `insufficient` → 无法区分"判官不确定"与"确实不支持" |
| **判官 pin** | 弱（模型名可换） | 本设计需要 artifact 级 pin + mismatch fail-closed（对齐余弦门禁） |
| **依赖面** | 引入 datasets/ragas/langchain 等重依赖 | 触发 `test_declared_dependencies.py` 契约，且与"仅标准库"的评估工具链取向冲突 |
| **可复现** | 内部含随机与并发 | 本设计要求同输入 → 同输出（judge 温度 0 + 确定性 ID） |

**保留借鉴**：RAGAS 的 `claim decomposition` 思路（本设计 §5.2）与 faithfulness = supported/total 的定义（与业界一致，便于横向比较）。**不引入其代码。**

### 5.6 门禁三态与阈值

#### 5.6.1 指标口径

| 指标 | 逐用例产出 | 区间方法 | 说明 |
|---|---|---|---|
| **忠实度（answer 级）** | `value ∈ [0,1]` 连续 | **t 区间**（`mean_ci`） | 逐答案取均值，与 Recall@K 同为连续量 |
| **忠实度（claim 级）** | supported / total 比例 | **Wilson 区间** | 二值计数，与 Hit Rate 同族 |
| **矛盾率** | `contradicted / total` | Wilson | 门禁最硬的指标（见下） |
| **弃权率** | `insufficient / total` | Wilson | **不是质量指标，是可信度门禁** |

> ⚠️ 与显著性工具同一条铁律：**二值用 Wilson、连续用 t，不可混用**。答案级忠实度是连续量（逐答案可能是 0.5、0.667），**绝不能用 Wilson**。

#### 5.6.2 三态判定

复用 `ceiling_status` 的三态设计思路（`pass` / `fail` + 一个"不可判定"态）：

| 判定 | 条件（全部满足） |
|---|---|
| **`undecidable`** | ① `n_answers < 30` **或** ② 区间跨越阈值 **或** ③ 弃权率 > 25% **或** ④ 判官校准 artifact 缺失/mismatch **或** ⑤ **语料前提未满足**（见 §5.6.4） |
| **`pass`** | `n ≥ 30` ∧ 区间下界 ≥ 阈值 ∧ 弃权率 ≤ 25% ∧ **零容忍项 == 0**（且**未**触发空转防护） ∧ 校准有效 ∧ 语料前提满足 |
| **`fail`** | 上述任一不满足（且不属于 `undecidable`） |

**为什么弃权率是门禁条件**：弃权率高意味着"判不了"，此时即使数值好看也不可信。类比方法论 §1.5——**未验证必须标 UNKNOWN，不能标 PASS**。

> 🔴 三态之外还有两个**降级触发**，都属"测不出"而非"没问题"：**零容忍空转**（§5.6.5，可判样本 <10）
> 与 **语料前提未满足**（§5.6.4）。二者均使整体转 `undecidable`，**不得**记 `pass`。

#### 5.6.3 阈值（**D-2 已决 2026-10-07：采纳建议值**）

| 指标 | 阈值 | 理由 |
|---|---|---|
| 答案级忠实度（95% t 区间下界） | **≥ 0.90** | 与 Recall@K 的稳定期阈值对齐；医学场景不宜低于此 |
| **零容忍项** | **== 0** | 精确定义见 §5.6.5（**按判官来源划分**，不按 `kind`） |
| 高危类型（`causal` / `numeric`）忠实度 | **≥ 0.95** | 因果与数值是临床最敏感的断言。⚠️ **n 小时恒为 `undecidable`**（区间必然跨 0.95）→ 它是**长期目标，不是当前门禁** |
| 弃权率 | **≤ 25%** | 超过则指标不可信，转 `undecidable` |

#### 5.6.4 🔴 语料前提未满足 → 恒 `undecidable`（D-2 决策时新增）

**阈值可以现在定，但不等于现在可以拿它判达标。** 三条语料前提：

| 前提 | 现状 | 未满足的后果 |
|---|---|---|
| P0-2 真实医学语料 | **未修**（公共 5 chunk 全同一文件；私有 7 条同源残留） | 证据贫瘠 → "证据未提"型 `not_entailed` 被**系统性高估**（spec R2） |
| P0-4 `gold` 来自真实索引 ID | **未修**（现为合成 ID） | 评估锚点本身不可信 |
| `n_answers ≥ 30` | 现状仅 18 条用例 | 区间过宽，`pass`/`fail` 都无统计意义 |

**规则**：任一前提未满足 → 三态**一律 `undecidable`**，数值照常报告。
这不是新增机制，而是把 R3 的"`n<30` 只报区间"原则**从"样本量"推广到"语料前提"**——
同一条原则：**"测不出问题"不等于"没问题"**（§4.3.1 / 方法论 §1.5）。

**报告义务**：`undecidable` 时必须写明**是哪一条前提未满足**，否则会被误读为"功能未完成"。

#### 5.6.5 🔴 零容忍项的精确定义（D-4 已决：仅规则判官）

> **零容忍项 = 满足 `judge == "rule" and code == "contradicted"` 的判定条数。该计数必须 == 0。**

🔴 **判据是 `judge`（来源），不是 `kind`（类型）。** 两个理由：

1. **零容忍 = 非零即无条件失败，只能给确定性来源。** 规则判官对 `numeric` 给出的是**确定性**判定
   （`_same_value` / `_normalize_unit`，与 `CitationValidator._check_numeric_consistency` 同源）；
   而 `causal` 等类型的 `contradicted` **只能由 LLM 判官给出**，是**概率**判断。给概率判官零容忍
   = **让 judge 的一次失误直接否决整轮评测**（judge 实际不可能 100% 正确，见 R1）。
2. **按 `kind` 判会漏。** 若将来某条 `numeric` 陈述被 LLM 判为 `contradicted`（例如规则判官弃权后转交 LLM），
   用 `kind == "numeric"` 会把它错误计入零容忍。**只有按 `judge` 判才是闭合的。**

**LLM 判官给出的 `contradicted` 的去向**：进**区间统计**（Wilson），参与 `fail` 判定，
但**不参与零容忍**。报告须**分列**两个计数，禁止合并：

| 计数 | 来源 | 门禁性质 |
|---|---|---|
| `rule_contradicted` | `judge == "rule"` | **零容忍（== 0）** |
| `llm_contradicted` | `judge` 以 `llm:` 开头 | 区间统计（Wilson） |

##### ⚠️ 零容忍的空转防护（D-4 决策时新增）

若**规则可判的 `numeric` 可判样本过少**，零容忍项会因为"没有样本"而恒为 0 → **空转通过**。
这与"零容忍项在无语料时应标 UNKNOWN 而非 PASS"（§4.3.1 / 方法论 §1.5）是同一条原则。

**规则**：`numeric` 类型的**可判**样本（`judge == "rule"` 的 `entailed` + `contradicted` 之和）
**< 10** 时 → 零容忍项状态记 **`样本不足`**，**不得记 `pass`**；整体三态据此转 `undecidable`。

### 5.7 kappa 校准流程（G4）

**D-1 已决（2026-10-07）：判官先用 `glm-5.3-flash`**（即 `.env` 既有 `MODEL_NAME`）。
理由：零新增成本与合规链路；**先挣 kappa 基线，不达标（< 0.60）才升级模型**。
换模型将使全部既有校准作废、须重跑 ①–④，故不做预先升级。

#### 5.7.0 🔴 self-preference bias（D-1 决策时补充发现，必须显式记录）

生成答案与判定答案**用的是同一个模型**（生成侧 `ChatOrchestrator` → `glm-5.3-flash`；
判官侧同款）。判官对"自己的产出"存在系统性偏好 → 可能给出偏高的 `entailed`。

**本设计不为此额外改动，但必须理解它为何不构成阻塞**：

- kappa 是**对人类标注**计算的，不是判官的自评 → 自评偏差会**表现为与人不一致**，
  从而**压低 kappa**。也就是说，**校准集本身就是探测该偏差的仪器**。
- 反向风险同样要警惕：若 kappa **高得反常（> 0.85）**，须先排查
  "标注者是否被答案文本带偏"，而非直接采信。
- 若 kappa 落在 0.60–0.70 的灰区且难以归因，**优先怀疑 self-preference**，
  再考虑改 prompt / 换模型（多判官投票见 R1 遗留）。

#### 5.7.1 流程

**目的**：证明 LLM 判官的判定与**人工判定**足够一致，从而有资格用于自动评估。

> 依据：方法论附录 B——"Cohen's kappa：LLM-judge 与人工判断的一致性系数，**≥0.70 才可用于自动评测**"。

```
① 抽取校准集   从 faithfulness_answers.json 分层抽样
               分层维度：claim.kind（6 类）× 判定难度（易/中/难）
               每格 ≥ 5 条 → 目标 60–100 条陈述（**不是答案**）
               ↓
② 人工标注     标注者**只**看到：原子陈述文本 + 该句绑定的证据正文
               （与 judge 的输入**逐字节同构**，否则 kappa 无意义）
               产出：{claim_id: "entailed|not_entailed|contradicted|insufficient"}
               ↓
③ 判官标注     同一批 claim_id 跑 judge_llm（同一 prompt_version / model）
               ↓
④ 一致性核算   Cohen's kappa（多分类，非二分）
               kappa = (p_o − p_e) / (1 − p_e)
               **并给出 95% 区间**（见 §5.7.4，不可只报点估计）
               ↓
⑤ 门禁判定     kappa 点估计 ≥ 0.70 **且** 区间下界 ≥ 0.60 → 判官可用于自动门禁
               0.60 ≤ 点估计 < 0.70 → **仅报告，不门禁**
               点估计 < 0.60 **或区间下界 < 0.60** → 判官不可用，需改 prompt/换模型后重跑 ①–④
               ↓
⑥ 产物落盘     config/eval/faithfulness_calibration-<date>.json
```

#### 5.7.2 校准产物 `FaithfulnessCalibration`

```python
@dataclass(frozen=True, slots=True)
class FaithfulnessCalibration:
    judge_model: str                # 判官模型标识（**必须 pin**）
    judge_prompt_version: str       # 与 judge_prompt.py 的常量一致
    eval_dataset_version: str       # 校准集版本（对齐 CalibrationArtifact 既有字段名）
    kappa: float
    kappa_ci: tuple[float, float]   # 🔴 D-5 已决：95% 区间（**必填**，不可只存点估计）
    n_claims: int
    confusion: Mapping[str, Mapping[str, int]]   # 人工 × 判官 混淆矩阵
    calibrated_at: str              # ISO 8601
    notes: str | None
```

#### 5.7.3 🔴 fail-closed 语义（对齐余弦门禁）

| 情形 | 行为 |
|---|---|
| 校准 artifact 缺失 | 忠实度**只报告，不出三态判定**（`undecidable`） |
| `judge_model` / `judge_prompt_version` 与当前运行不一致 | **`undecidable`**，并在报告显著位置告警（**不可静默降级为 pass**） |
| kappa 点估计 < 0.70 **或区间下界 < 0.60** | `undecidable`（判官无资格门禁），但**数值仍照常报告** |
| `kappa_ci` 缺失（旧格式 artifact） | **`undecidable`**（无法确认判官资格，视为无校准） |

**为什么必须 pin 且 fail-closed**：项目在余弦门禁上已有前例——`CalibrationArtifact` 承载 `rrf_k` / `embedding_model` / 索引版本，不一致即 fail-closed（`calibration_artifact_mismatch`）。**判官版本漂移比阈值漂移更隐蔽**：换个模型版本后 kappa 可能从 0.75 掉到 0.55，而所有数值看起来照常产出。不 pin 就等于没有校准。

#### 5.7.4 🔴 为什么必须报区间（D-5 已决：0.70 + 强制报区间）

**kappa 是点估计。** n=60、4 分类时标准误约 0.05–0.08 → **95% 区间宽达 ±0.10~0.16**。

因此"恰好 0.71"的 kappa，**真实值可能在 0.55 附近**——用它门禁等于**用噪声门禁**。
这是显著性工具同一条铁律的延伸（"只报点估计不给区间"是四类常见误用之一）。

**规则**：
- 门槛保持 **0.70**（与项目既有方法论一致，**不另造标准**）；
- 但**报告与 artifact 必须同时给出点估计与 95% 区间**；
- **区间下界 < 0.60 → 判定降级为 `undecidable`**。

这样门槛的含义从"点够高"变成"**确实够高**"。
区间方法：kappa 近似正态 → 用 `mean_ci` 的 t/正态口径；`n < 30` 时**只报区间，不判定达标**（沿用既有规则）。

> ⚠️ 与 §5.7.0 呼应：若 kappa 点估计偏高但**区间很宽**，须先怀疑 self-preference bias 或标注样本不足，
> 而非直接采信点估计。

### 5.8 真实链路答案生成（G6）

**生成与判定解耦**是本节的核心设计：

| 模式 | 入口 | 用途 |
|---|---|---|
| `--source live` | `scripts/faithfulness_answers.py` 真调 `ChatOrchestrator.answer()` | 生成权威答案集（昂贵：单条 18–99 s，见 P0-5 记录） |
| `--source replay` | 读 `eval_out/faithfulness_answers.json` | **默认**。重判（改 prompt / 换 judge）不重调模型 |

**答案集 schema**（`eval_out/faithfulness_answers.json`）：

```json
{
  "generated_at": "2026-10-06T...",
  "generator": {"model": "glm-5.3-flash", "orchestrator_timeout_s": 300},
  "index_version": {"public": "public-v1", "private": "private-v1"},
  "answers": [
    {
      "answer_id": "q01",
      "question": "...",
      "text": "...",                       // 见 §5.9 PHI 说明
      "status": "answered",
      "cited_evidence": ["<uuid>", "..."],
      "evidence": [{"id": "<uuid>", "text": "...", "title": "..."}],   // 见 §5.9
      "diagnostics": {"answer_path": "rag", "gate_policy": "cosine_absolute"}
    }
  ]
}
```

**生成侧必须复用真链路**：

- 真 `ChatOrchestrator` + 真 `ModelGateway` + 真 `RetrievalService`（范本：`scripts/casual_routing_live_acceptance.py`）。
- **不可用裸 adapter**：那会绕过同意校验与 `EXTERNAL_MODEL_CALL` 审计（该脚本已写明此理由）。
- `total_timeout_seconds` 必须显式抬到 `settings.model_total_timeout_seconds`（默认 60s 会让整轮降级）。
- **独立临时库**（`tempfile.mkdtemp()`）：生成会写 Tenant/Patient/Consent/AuditEvent，**绝不污染 `medical_rag.db`**。
- 代理变量开头清空（`*_PROXY` pop + `NO_PROXY=*`），否则 OpenSearch 假阴性。

### 5.9 PHI 与数据落盘（**D-3 已决：方案 B**，2026-10-07）

`eval_out/*.json` 会落盘**证据正文**，而私有库证据可能含患者数据。三档方案：

| 方案 | 落盘内容 | 代价 |
|---|---|---|
| **A · 全量**（当前语料为零真实 PHI，可用） | 证据正文 + 答案正文 | 一旦接入真实患者数据即不可再用 |
| **B · 哈希 + 本地缓存**（**已采纳**） | 证据只存 `id` + `sha256`；正文存本地 gitignored 缓存，判定时读 | 需多一个缓存目录 |
| **C · 仅哈希** | 只存哈希 | **不可复判**（judge 需要正文），失去 G6 的解耦价值 |

**已采纳 B**：既保证 `eval_out/` 可安全入库，又保留可复判性。默认 B，`--allow-phi` 作为显式开关（默认关）。

#### 5.9.1 落地时必须同时覆盖的两个字段（🔴 D-3 决策时补充发现）

> 本节是 D-3 定档后回填的**两条设计缺漏**，二者都会让"默认安全档"实际失效。

1. **`answers[].text` 同样可能含 PHI，必须与 `evidence[].text` 同档处理。**
   原 S12 只约束了 `evidence[].text`。但答案会**复述患者数值**（"血红蛋白 105 g/L，低于参考区间"），
   `eval_out/` 里留有它，泄漏面与证据正文完全等同。**只脱敏一项 = 没脱敏**。
2. **`.gitignore` 当前并未忽略 `eval_out/`**（实测：该目录 13 个文件均明文落盘，未被忽略）。
   即 A 档的安全前提**只是"当前恰好零真实 PHI"这一临时状态，不是任何机制保证**。
   故本设计**不把 `eval_out/` 可入库当作既定事实**：B 档落地时须同时
   - 将缓存目录加入 `.gitignore`；
   - 明确 `eval_out/` 是否入库由**产物内容**决定，而非由目录名决定。

3. **读缓存必须校验 `sha256`。** 缓存文件可能被人工编辑或在多次运行间漂移；
   不校验则 judge 读到的是"另一份证据"，而报告仍显示为同一条 `claim_id` —— 静默失真。
   **sha256 不匹配 → 该条记 `judge_error`，不得静默使用。**

---

## 6. 安全 / 合规不变量

| # | 不变量 | 验证方式 |
|---|---|---|
| **S1** | 判官输入**仅**含「单条原子陈述 + 该句绑定证据」，不含完整答案、未引用候选、患者标识 | 单测：构造含 `e1`/`e2` 的答案，断言 judge 收到的 payload 里不含 `e2` 正文（当该句只引 `e1`） |
| **S2** | 分母**不含**免责/结构/否定/列举引导句 | 单测：全免责答案 → `total == 0` |
| **S3** | `total == 0` → `value is None`，**绝不** 1.0 | 单测：纯拒答 → `value is None` |
| **S4** | 弃权（`insufficient`）**永不**计入 `supported`；弃权率独立统计 | 单测：全弃权 → `value is None` 且 `abstained == n` |
| **S5** | 规则判官对非 `numeric` 陈述**一律弃权** | 单测：`kind="causal"` → `judge_rule(...) is None` |
| **S6** | 切片规则与 `CitationValidator` **共用同一对象**（非复制） | 契约测试：`faithfulness._NON_CLAIM_SENTENCE is CitationValidator._NON_CLAIM_SENTENCE` |
| **S7** | 生成侧**不写** `medical_rag.db`；用 `tempfile.mkdtemp()` 独立库 | 集成测试：跑完后断言生产库 mtime 未变 |
| **S8** | 网关不可达 → **抛错终止**，不产出"全绿"报告 | 单测：注入不可达 gateway → 抛 `RuntimeError` |
| **S9** | 校准 artifact 缺失 / 版本 mismatch → `undecidable`，**不得**降级为 `pass` | 单测：无 artifact → `verdict_state == "undecidable"` |
| **S10** | 本设计**不改**任何生产返回值、**不改**诊断白名单、**不改** `.env` | 契约测试：`_ANSWER_DIAGNOSTIC_KEYS` 长度不变 |
| **S11** | 判官调用**零温度**且 ID 确定性 → 同输入同输出 | 单测：同 claim 两次 `decompose` → 同 `claim_id` 序列 |
| **S12** | 落盘遵循 §5.9 方案 B（**已决**），`--allow-phi` 关闭时**不含**证据正文**且不含答案正文** | 单测：默认模式输出 JSON 中**既无** `evidence[].text` **也无** `answers[].text`（§5.9.1 缺漏 1） |
| **S13** | 缓存正文读取时**校验 `sha256`**，不匹配 → 该条 `judge_error`，**不得**静默使用 | 单测：篡改缓存文件 → 该 claim 判 `judge_error` |
| **S14** | 零容忍项判据**只取** `judge == "rule"` 的 `contradicted`；LLM 判定的 `contradicted` **不得**计入零容忍 | 单测：构造 `judge="llm:..."` 的 `contradicted` → 零容忍计数不含它（§5.6.5） |
| **S15** | 零容忍项**空转防护**：`numeric` 规则可判样本 < 10 → 记 `样本不足`，**不得**记 `pass` | 单测：全为 `causal`（无规则可判）→ 零容忍状态为 `样本不足`（§5.6.5） |
| **S16** | 语料前提未满足（P0-2 / P0-4 未修）→ 三态**恒 `undecidable`**，且报告写明是哪一条 | 集成测试：合成 `index_version` 标记为合成 / 无语料 → `verdict_state == "undecidable"`（§5.6.4） |
| **S17** | kappa **必须同时给点估计与 95% 区间**；`kappa_ci` 缺失视同无校准 | 单测：artifact 无 `kappa_ci` → `undecidable`（§5.7.4） |

---

## 7. 错误与降级

| 情形 | 行为 | 理由 |
|---|---|---|
| judge 输出不可解析 | 重试 ≤2 次 → `judge_error`（该条**不计入分母**，单独统计） | 单条故障不应击穿整轮 |
| `judge_error` 比例 > 10% | **整轮标 `undecidable`** 并告警 | 大面积故障会被平均成"忠实度低" |
| 网关不可达 / 超时 | **抛错终止**（S8） | 铁律 4：不静默跳过 |
| 校准 artifact 缺失 | 照常产出数值，三态记 `undecidable` | 测量有效，门禁无效 |
| 答案集文件缺失（`replay`） | 抛错并提示先跑 `faithfulness_answers.py` | 不产空报告 |
| 某答案 `status != "answered"` | 记 `N/A`，计入**拒答统计**，不进忠实度聚合 | 见 §4.3.1 |

---

## 8. 实施阶段

| 阶段 | 内容 | 可独立验收 |
|---|---|---|
| **阶段 1** | `faithfulness.py`：`AtomicClaim` / `ClaimVerdict` / `FaithfulnessResult` / `decompose()` / `judge_rule()` + 单测（**零模型调用**） | `pytest tests/unit/test_faithfulness.py` 全绿 |
| **阶段 2** | `scripts/faithfulness_answers.py`：真链路生成答案集 | 产出 `eval_out/faithfulness_answers.json`，含 ≥20 条 `answered` |
| **阶段 3** | `judge_prompt.py` + `judge_llm()` + `scripts/faithfulness_eval.py` + `scripts/faithfulness_calibrate.py` | 产出判定 JSON；校准脚本产出 kappa |
| **阶段 4** | 报告接入 + 三态判定 + `docs` 更新 | 报告含忠实度区间与三态；方法论 §7 清单补一行 |

**阶段 3 依赖人工**：①②③可完全自动，④（kappa）需要人工标注 ≥60 条陈述。**这是本设计的硬约束**——在人工标注完成前，忠实度**只能报告不能门禁**（`undecidable`），这正是设计的预期行为。

---

## 9. 验收标准

### A. schema 与切分正确性

| # | 断言 | 期望 |
|---|---|---|
| A1 | `decompose("血红蛋白 105 g/L，低于参考区间 [e1]。")` | 切出 **2 条**（numeric + trend），`cited_ids == ("e1",)` |
| A2 | `decompose("请咨询医生。")` | **0 条**（免责句） |
| A3 | `decompose("## 建议\n- 多喝水")` | **0 条**（结构行） |
| A4 | 全免责答案 → `FaithfulnessResult.total == 0` 且 `.value is None` | S3 |
| A5 | 同答案两次 `decompose` → `claim_id` 序列逐项相等 | S11 |

### B. 判官行为

| # | 断言 |
|---|---|
| B1 | `judge_rule(claim(kind="numeric"), {"e1": "105 g/L"})`：陈述含 105 → `entailed` |
| B2 | 同上，陈述含 160 → `contradicted` |
| B3 | 同上，陈述含 105 但单位 `mg/dL` → `contradicted` |
| B4 | `judge_rule(claim(kind="causal"), ...)` → **`None`**（弃权，S5） |
| B5 | LLM 判官输出 ` ```json {...} ``` ` 时能正确解析（围栏剥离） |
| B6 | LLM 判官输出非法 `verdict` → 重试后 `judge_error` |
| B7 | 注入不可达 gateway → 抛 `RuntimeError`（S8） |

### C. 一致性（与 `CitationValidator` 共用口径）

| # | 断言 |
|---|---|
| C1 | `faithfulness._NON_CLAIM_SENTENCE is CitationValidator._NON_CLAIM_SENTENCE`（S6） |
| C2 | `faithfulness` 的数值比较函数 `is` `CitationValidator._same_value` |
| C3 | 构造"校验器放过但数值实为张冠李戴"的样本 → 忠实度能标出 `contradicted`（**暴露校验器盲区**，本设计的核心价值） |

### D. 安全与不变量

| # | 断言 |
|---|---|
| D1 | S1：payload 不含未绑定证据正文 |
| D2 | S9：无校准 artifact → `undecidable` |
| D3 | S10：`_ANSWER_DIAGNOSTIC_KEYS` 长度与内容不变 |
| D4 | S12：默认模式输出**既不含** `evidence[].text` **也不含** `answers[].text` |
| D5 | 新模块不引入第三方依赖 → `test_declared_dependencies.py` 通过 |
| D6 | S7：生成后生产库 mtime 未变 |
| D7 | S14：零容忍计数只取 `judge == "rule"` 的 `contradicted` |
| D8 | S15：`numeric` 规则可判样本 < 10 → 零容忍记 `样本不足`，不记 `pass` |
| D9 | S16：语料前提未满足 → 三态恒 `undecidable` 并写明原因行 |
| D10 | S17：artifact 无 `kappa_ci` → `undecidable` |

---

## 10. 决策项（**D-1~D-6 已全部定案 2026-10-07**）

| # | 决策点 | 建议 | 影响 |
|---|---|---|---|
| **D-1** ✅**已决 2026-10-07** | 判官模型选择 | **采 A：先用 `glm-5.3-flash` 跑通并测 kappa**；kappa < 0.60 再考虑换模型 | 已定；换模型需重跑全部校准。附带记录 §5.7.0 self-preference bias |
| **D-2** ✅**已决 2026-10-07** | 门禁阈值 | **采纳 0.90 / 0.95 / ≤25%，并新增「语料前提未满足 → 恒 `undecidable`」**（§5.6.4） | 已定。高危 ≥0.95 在 n 小时**恒为 undecidable**，是长期目标而非当前门禁 |
| **D-3** ✅**已决 2026-10-07** | PHI 落盘 | **采 B（哈希+本地缓存）**，`--allow-phi` 显式开关默认关 | 已定；落地须覆盖 `answers[].text` + `.gitignore` + sha256 校验，见 §5.9.1 |
| **D-4** ✅**已决 2026-10-07** | 矛盾率零容忍的范围 | **仅规则判官**：`judge == "rule" and code == "contradicted"` 必须 == 0；LLM 判定的只进区间 | 已定；判据是 **`judge`（来源）不是 `kind`**，并新增空转防护（可判样本 <10 → 样本不足），见 §5.6.5 |
| **D-5** ✅**已决 2026-10-07** | kappa 门槛 | **0.70 保持不变，但强制报 95% 区间**；区间下界 < 0.60 → `undecidable` | 已定；`kappa_ci` 改为**必填字段**，见 §5.7.4 |
| **D-6** ✅**已决 2026-10-07** | 是否补 `required_evidence_ids` | **本次不做**；触发条件 = **P0-2 换源 + P0-4 gold 真实化之后**，与 `gold_evidence_ids` **合并标注** | 已定。理由不是成本，而是**在合成 ID 上标注没有意义**（§10.1） |

### 10.1 为什么 `required_evidence_ids` 现在不做（D-6 已决）

`required_evidence_ids`（回答该问题的最小充分证据集）是**完整性 / 答案相关性**的前提，
也是 spec §2.2 明确排除的两项非目标。D-6 的结论是**不做**，但理由**不是"成本高"**，而是：

> **在错误的数据上标注没有意义。**

| 事实 | 后果 |
|---|---|
| 它是**方法论 §1.2 的 C 类标注**（必须人工，语义真值） | 成本高，但这只是次要原因 |
| 它必须与 `gold_evidence_ids` **配对存在** | 二者是同一批证据上的两个字段，分开标 = 重复读同一批材料 |
| 当前 `gold` 是**合成 ID**（P0-4 未修） | **标注会建立在一份错误的坐标上** → 标出来也是废的 |

**正确触发条件**：**P0-2（换真实语料）+ P0-4（gold 真实化）之后**，与 `gold_evidence_ids`
**合并标注**（一次过读同一批证据，边际成本最低）。

**如实交代代价**：在此之前，忠实度**只能答"说了的属不属实"，不能答"该说的说了吗"**。
而后者才是"忠实度高但答案没用"的根源（本 spec R7）——
一个全部 `supported` 的答案可以是完全无用的（只复述了证据里最无关的一句）。
**接受这个缺口，是因为补它的前提条件尚不存在。**

---

## 11. 风险与遗留

| # | 风险 | 缓解 |
|---|---|---|
| R1 | **单 judge 单点故障**：judge 的系统性偏差会直接成为指标偏差 | 弃权率门禁 + kappa 校准；多 judge 投票列为遗留 |
| R2 | **语料仅 12 chunk**（P0-2）：证据贫瘠会让"证据未提"型 `not_entailed` 被高估 | 报告**分层**给出 `kind` 维度的忠实度；结论受 P0-2 制约须显式标注 |
| R3 | **样本量 n<30**（现状 18 条用例） | 复用既有规则：**只报区间，不判定** |
| R4 | **可检测≠可防**：harness 只做**离线测量**，不拦线上回答 | 若需线上兜底，须单独 spec（把 `judge_llm` 接进闸 2b） |
| R5 | 陈述切分是**启发式**，逗号并列的边界会有争议 | 切分规则写死并单测；争议样本进校准集，用 kappa 反证切分是否影响判定 |
| R6 | 判官 prompt 泄漏到上下文（长证据） | 证据正文超长时截断并记录 `truncated: true`，**不得静默截断** |
| R7 | **完整性缺失**（§2.2 / §10.1）：忠实度高 ≠ 答得好 | 报告中忠实度**必须与**拒答正确率、答案相关性并列呈现，禁止单指标下结论。**并必须显式写明"完整性未测量"**（D-6 已决不做），否则会被误读为"答案质量已全面覆盖" |

---

## 12. spec 自检（FEATURE-WORKFLOW §5）

| # | 检查项 | 结论 |
|---|---|---|
| 1 | 是否先验证承重事实（不把设计建在错误假设上）？ | ✅ §1.1 逐行核对 `citations.py` 的能力边界；§5.1 标出**刻意不复用** `_sentences_with_evidence()` 的唯一一处 |
| 2 | 是否明确"不做什么"？ | ✅ §2.2 六条非目标，含"不接线上门禁""不引入 RAGAS" |
| 3 | 是否给出可观测验收？ | ✅ §9 A/B/C/D 全为可执行断言 |
| 4 | 安全不变量是否可验证？ | ✅ §6 S1–S12 每条对应验证方式 |
| 5 | 是否滥用零容忍？ | ✅ §5.6.5 **限制**零容忍到 `judge == "rule"` 的 contradicted（**按来源，不按 kind**），并加**空转防护**（S15）；§10 **D-4 已决** |
| 6 | 是否把"测不出"与"没问题"区分？ | ✅ §4.3.1（`total=0 → None`）、§5.6.2（弃权率门禁） |
| 7 | 是否有口径漂移风险？ | ✅ S6/C1/C2 以 `is` 别名锁定共用，防"两处实现漂移" |
| 8 | 是否与既有机制同构？ | ✅ §5.7.3 校准 fail-closed 对齐余弦门禁 `CalibrationArtifact` |
| 9 | 是否显式列出缺口？ | ✅ §11 R1–R7，含"可检测≠可防""完整性缺失" |
| 10 | 是否留下待决策项？ | ✅ §10 D-1~D-6 六项**已全部定案（2026-10-07）**，无遗留 |

---

## 13. 一句话定位

> **`CitationValidator` 回答"引用是否合规"，本设计回答"引用是否属实"。**
> 前者是规则可判的**拦截**，后者是概率判官的**测量**——
> 而**测量要成立，判官自己必须先被测量**（kappa 校准 + 版本 pin + fail-closed）。
