# 余弦相关性门禁（Cosine Relevance Gate）设计

## 1. 文档信息

- 日期：2026-09-24
- 状态：**待用户批准**（未批准不写代码）
- 适用项目：`medical-rag`
- 关联评估：`docs/检索模块评估与调参报告.md`、`eval_out/指标达标核对表.md`
- 关联既有 spec：`2026-09-14-p0-rag-optimization-design.md`（§9.3 融合策略、§11 错误与降级）

---

## 2. 背景

### 2.1 触发问题的原始诉求

评估发现一个**上线级缺陷**：当前生产配置**永不拒答**。与知识库毫无关系的问题
（如"乳腺癌的靶向治疗方案"、"我的胸部 CT 结果"）会返回满额 `top_k` 条高血压/血常规
内容，`reason=None`、`gate_policy=top_k`、链路状态 `ok`。

对医疗 RAG 而言这比排序质量更严重：LLM 拿到 3 条不相关的高血压内容，仍可能组织出
一段看似有据的回答；正确行为是明确回答"证据不足"。

### 2.2 现状根因

两层叠加：

1. `RetrievalService` 默认 `GatePolicy.TOP_K`——**无任何相关性门禁**，入选范围完全由
   `top_k` 决定。
2. 唯一可用的门禁分数 `calibrated_score`（RRF 归一化）**不具备相关性判别力**。它衡量
   的是"两路检索是否一致地把该文档排在前面"，不是"该文档是否相关"。候选池只有 5~7 条
   时两路都必须返回 top-N，于是每篇文档同时出现在两个榜单里，分数被压进
   `[0.9385, 1.0]`——相关与不相关**几乎完全重叠**。

在 `calibrated_score` 上做阈值扫描，最好只能做到拒答 1/2 且可答命中率跌到 0.714。
**这是方向性错误，不是调参问题。**

### 2.3 修复方向与已验证的效果

原始单路分数保留了绝对尺度。原型验证（`scripts/retrieval_gate_prototype.py`）：

| 配置 | 拒答正确 | 命中率 | MRR | nDCG | 零容忍违规 |
|---|---|---|---|---|---|
| 生产现状（无门禁） | **0/2** | 1.000 | 0.964 | 0.946 | 0 |
| 向量分数门禁 0.50 | **2/2** | 1.000 | 0.964 | 0.946 | 0 |

**拒答 0/2 → 2/2，排序指标零损失。**

### 2.4 ⚠️ 本次设计必须纠正的一个事实错误

前一份报告把 `vector_score` 称为"向量余弦相似度"，**这个标签是错的**。实测
（`.workbuddy/space_type_probe.py`）：

```
medical-rag-public-local  embedding mapping : {'type': 'knn_vector', 'dimension': 512}
medical-rag-private-local embedding mapping : {'type': 'knn_vector', 'dimension': 512}
```

`ensure_mapping` 建 `knn_vector` 时**没有声明 `space_type`**，OpenSearch 对 `knn_vector`
的默认值是 **`l2`** 而非 `cosinesimil`。实测吻合度：

| id | OpenSearch `_score` | 真余弦 | `1/(1+L2²)` |
|---|---|---|---|
| 39f7ae94 | 0.55734 | 0.60288 | **0.55734** ✓ |
| 7498fde6 | 0.47583 | 0.44920 | **0.47583** ✓ |
| 217f6ad9 | 0.43986 | 0.36327 | **0.43986** ✓ |

结论：`vector_score` 实际是 **`s = 1/(1 + L2²)`**。对已 L2 归一化的向量，
`L2² = 2 − 2·cos`，故

```
s = 1 / (3 − 2·cos)          cos = 1.5 − 0.5/s
```

**这是余弦的严格单调变换**，所以：
- 排序与按余弦排序**完全一致**（原型结论不受影响，仍成立）；
- 但**数值不等于余弦**，阈值的语义与可移植性必须显式声明。

换算对照（分离区间）：

| 真余弦 | 1/(1+L2²) | 含义 |
|---|---|---|
| 0.4073 | 0.4576 | 不相关最小值 |
| 0.4556 | 0.4788 | 不相关最大值 |
| **0.4951** | **0.4976** | **分离中点** |
| 0.5000 | 0.5000 | ← 巧合：两空间在 0.5 处数值相同 |
| 0.5346 | 0.5179 | 相关最小值 |
| 0.6724 | 0.6042 | 相关最大值 |

> **注意这个巧合只在阈值 0.5 处成立**（`1/(3−2·0.5) = 0.5`）。其他取值两空间不相等，
> 例如余弦 0.53 对应 `1/(1+L2²)` 的 0.5155。**因此阈值必须连同"分数空间"一起声明**，
> 否则后续重新标定时会产生静默偏移。

---

## 3. 目标与非目标

### 3.1 目标

1. **新增显式的余弦相关性门禁**（`GatePolicy.COSINE_ABSOLUTE`），使"知识库确实无相关内容"
   的查询返回 `items=[]` + `reason=insufficient_evidence`，而不是把不相关内容当证据。
2. **不引入任何排序质量回退**：可答用例的 Hit@K / Recall@K / MRR / nDCG@K 相对现状
   **零损失**（容差 0）。
3. **不破坏任何安全不变量**：越权 / 删除残留 / 复核泄漏三项保持为 0。
4. **阈值可标定、可失效**：阈值必须绑定 embedding 模型 + 索引版本 + 分数空间，
   任一项变化即失效重标（复用既有 `CalibrationArtifact` fail-closed 机制）。
5. **门禁语义可观测**：外部能从 `diagnostics` 判断当前用的是哪种门禁、阈值多少、
   被拦下多少候选。

### 3.2 非目标（本次明确不做）

| 非目标 | 原因 |
|---|---|
| **不改 `space_type` 为 `cosinesimil`** | `space_type` 建索引后不可原地修改，需重建索引。且 0.5 处两空间数值巧合相同，本阶段无收益。列为**阶段 2**。 |
| **不做运行时 space_type 一致性校验** | 需要给 `SearchIndex` 协议新增"索引自省"接缝（服务只持有协议，不保证是 OpenSearch）。本阶段仅把 `score_space` 记入 artifact 与 diagnostics 供追溯，**不声称已做运行时强校验**。列为阶段 2。 |
| **不改 `calibrated_score` / RRF 逻辑** | `calibrated_score` 对"两路一致性"的度量本身没错，错的是拿它做相关性门禁。既有 `EXPLICIT_THRESHOLD` / `CALIBRATED` 路径保持原样。 |
| **不改 `top_k` 默认值** | `RETRIEVAL_TOP_K=8→3` 是独立的配置变更，需单独验收。列为**阶段 3**。 |
| **不清理重复/乱码语料** | 属数据治理，独立于本次代码变更。 |
| **不实现 `deletion_residuals` / `review_required_leakage` 负样本集** | 属评估集建设，独立任务。 |
| **不改 `_safe_diagnostics` 的字符串哈希行为** | 见 §9「已发现但本次不修的相邻缺陷」。 |

---

## 4. 总体架构

### 4.1 变更位置

只有一处生产代码路径发生变化——`RetrievalService._retrieve_hybrid()` 的**融合前**：

```text
词法候选 ─┐
          ├─► ACL/引用过滤 ─► 【新增：向量分数绝对阈值门禁】─► RRF 融合 ─► 重排 ─► Top-K
向量候选 ─┘                        │
                                   └─ 全部被拦 → items=[] + insufficient_evidence
```

### 4.2 为什么放在融合前

| 方案 | 评价 |
|---|---|
| **融合前过滤向量候选，并对词法候选做交集**（采用） | 门禁即"相关性准入"，语义清晰；融合只看到相关候选，RRF 排名不受不相关内容干扰 |
| 融合后按 `vector_score` 过滤 | 词法独有候选（`vector_score=0.0`）会被一并误杀，且语义含混 |
| 只过滤向量、词法全保留（`emptiness`） | 纯关键词命中的不相关文档会绕过门禁；原型实测在阈值 ≤0.53 时与交集等价，但高阈值下查准更差 |

原型实测两种作用范围在阈值 ≤0.53 时结果等价；**采用 `intersect`**（更严格、语义更清晰、
高阈值下查准更优）。

### 4.3 门禁生效的完整判定链

```text
1. 向量路径是否实际运行？（embedding 可用 且 search_vector 未报错）
   ├─ 否 → 【跳过门禁】保持现状行为，diagnostics 记 gate_skipped_reason
   └─ 是 → 继续
2. 过滤：保留 vector_score >= threshold 的向量候选
3. 交集：词法候选仅保留 id 出现在第 2 步结果中的
4. 若第 3 步结果为空 → items=[] + insufficient_evidence
   （但若两路均报错，仍由 _empty_reason 返回 retrieval_unavailable）
```

---

## 5. 核心数据契约

### 5.1 `GatePolicy` 新增枚举值

```python
class GatePolicy(StrEnum):
    CALIBRATED = "calibrated"
    EXPLICIT_THRESHOLD = "explicit_threshold"
    RANK_GATE = "rank_gate"
    TOP_K = "top_k"
    COSINE_ABSOLUTE = "cosine_absolute"   # 新增
```

语义：**按单路向量分数的绝对阈值做相关性准入**。与 `CALIBRATED`/`EXPLICIT_THRESHOLD`
（作用于 RRF 归一化分数）互斥。

### 5.2 `CalibrationArtifact` 新增字段

```python
@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    index_version: str
    embedding_model: str
    rrf_k: int
    threshold: float                     # 既有：RRF 归一化阈值（保持原义，不改）
    eval_dataset_version: str
    # --- 新增（可选；存在即启用余弦门禁）---
    cosine_threshold: float | None = None
    #: 该阈值所处的分数空间。取值：
    #:   "l2_inv"       → 1/(1+L2²)（OpenSearch knn_vector 默认 space_type=l2）
    #:   "cosinesimil"  → 真余弦（space_type=cosinesimil 时）
    score_space: str | None = None
```

**为什么用可选新字段而不是复用 `threshold`**：复用会让既有已部署 artifact 的
`threshold=0.42` 从"RRF 0.42"**静默变成**"向量分数 0.42"，含义完全不同。这是
`FEATURE-WORKFLOW` 规则 4 禁止的静默语义变更。新增可选字段做到：
- 不含新字段的旧 artifact → 行为**逐字节不变**；
- 含 `cosine_threshold` 的 artifact → 走新策略，且 `threshold` 仍被校验以保持
  构造期一致性检查不失效。

校验规则（`__post_init__`）：
- `cosine_threshold` 非 None 时必须为 `0.0 ≤ v ≤ 1.0` 的数值且非 bool；
- `cosine_threshold` 非 None 时 `score_space` **必须**同时提供，且 ∈
  `{"l2_inv", "cosinesimil"}`（缺一即拒——避免"阈值存在但不知道它是什么单位"）；
- `score_space` 提供但 `cosine_threshold` 为 None 时同样拒绝（无意义组合）。

### 5.3 `RetrievalService` 新增构造参数

```python
def __init__(
    self,
    index: SearchIndex,
    *,
    ...
    fused_score_threshold: float | None = None,
    rank_gate: int | None = None,
    cosine_threshold: float | None = None,      # 新增
    score_space: str | None = None,             # 新增（诊断用，随阈值一同声明）
    calibration_artifact: CalibrationArtifact | None = None,
) -> None:
```

策略解析优先级（保持既有顺序，新策略插入）：

```text
calibration_artifact.cosine_threshold is not None
        → COSINE_ABSOLUTE
elif calibration_artifact is not None
        → CALIBRATED
elif cosine_threshold is not None
        → COSINE_ABSOLUTE
elif fused_score_threshold is not None
        → EXPLICIT_THRESHOLD
elif rank_gate is not None
        → RANK_GATE
else
        → TOP_K
```

互斥校验：`cosine_threshold` 与 `fused_score_threshold` / `rank_gate` **不得同时**显式传入
（两个门禁同时配置意味着调用方不清楚哪个生效，fail-fast）。

### 5.4 `diagnostics` 新增键

| 键 | 类型 | 含义 |
|---|---|---|
| `gate_policy` | str | 既有；新增取值 `"cosine_absolute"` |
| `gate_threshold` | float \| None | 既有；余弦门禁下为该门禁的阈值 |
| `gate_score_space` | str \| None | 新增；阈值所处分数空间（`l2_inv`/`cosinesimil`） |
| `vector_gated_candidates` | int | 新增；通过余弦门禁的向量候选数 |
| `vector_candidates_gated_out` | int | 新增；被余弦门禁拦下的向量候选数 |
| `lexical_gated_out` | int | 新增；因未通过门禁交集而被剔除的词法候选数 |
| `gate_skipped_reason` | str \| None | 新增；门禁被跳过的原因（`vector_path_unavailable`） |

---

## 6. 设计细节

### 6.1 门禁在 `_retrieve_hybrid` 中的落点

插入位置：**ACL/引用过滤之后、`self.ranker.fuse(...)` 之前**。

```python
lexical = [r for r in lexical if authorized_and_cited(r)]
vector  = [r for r in vector  if authorized_and_cited(r)]

# --- 新增：余弦相关性门禁 ---
if self.gate_policy is GatePolicy.COSINE_ABSOLUTE:
    if vector_attempted and not vector_errored:
        gated = [r for r in vector if r.vector_score >= self.cosine_threshold]
        gated_ids = {r.id for r in gated}
        lexical_before = len(lexical)
        lexical = [r for r in lexical if r.id in gated_ids]
        diagnostics["vector_gated_candidates"] = len(gated)
        diagnostics["vector_candidates_gated_out"] = len(vector) - len(gated)
        diagnostics["lexical_gated_out"] = lexical_before - len(lexical)
        vector = gated
    else:
        # 向量路径未运行：跳过门禁而不是拒答一切（见 §7.2）
        diagnostics["gate_skipped_reason"] = "vector_path_unavailable"

fused = self.ranker.fuse(lexical, vector, candidate_limit)
```

### 6.2 空结果与拒答语义

门禁清空候选后，**复用既有的空结果分支**，不新增 reason：

```python
if not fused:
    return RetrievalResult(
        items=[],
        reason=self._empty_reason(
            lexical_errored=..., vector_attempted=..., vector_errored=..., embedding_error=...,
        ),
        diagnostics=diagnostics,
    )
```

这样：
- 门禁正常拦下 → `insufficient_evidence`（"没有足够医学证据"）；
- 两路真的都失败 → `retrieval_unavailable`（"服务暂不可用"）。

**这两者绝不能混同**——把集群宕机呈现成"没有足够医学证据"会误导患者。

### 6.3 门禁不参与安全过滤

`_is_authorized` / `_has_citation` 仍在门禁**之前**执行，门禁只做**减法**（移除候选），
不可能引入新的越权/残留/泄漏。安全不变量不因本次变更而弱化。

### 6.4 阈值来源与标定流程

阈值**不得硬编码在代码里**。标定流程（离线）：

```text
1. 用真实评估集跑两路候选，收集每个 (query, doc) 的 vector_score 与人工相关性判定
2. 分别统计相关/不相关的分数分布，求分离区间 [noise_max, gold_min]
3. 取区间内的值作为候选阈值，用 eval 集验证：
     拒答正确率 == 100%  且  排序指标零损失
4. 把选定阈值 + score_space + index_version + embedding_model + eval_dataset_version
   写入 calibration artifact JSON
5. 组装根通过 CALIBRATION_ARTIFACT_PATH 加载（既有机制，不改）
```

本次标定结果（已由原型验证，见 §2.3 / §2.4）：

| 项 | 值 |
|---|---|
| `cosine_threshold` | **0.50** |
| `score_space` | **`"l2_inv"`** |
| 等效真余弦 | 0.50（0.5 处两空间数值相同） |
| 安全窗口 | `[0.490, 0.516]`（= 余弦 `[0.48, 0.53]`） |
| 依据 | 拒答 2/2 + 排序指标零损失；窗口上沿 0.516 处查准率 0.333→0.512 |

> ⚠️ **这是冷启动初值**。分离区间来自 30 条相关 + 62 条不相关候选、评估集仅 18 条用例。
> 必须在真实评测集上复核。更换 embedding 模型、切网关后端（1536 维）、或语料分布
> 变化后**必须重新标定**。

### 6.5 组装根接入

`api/composition.py` 不需要新增逻辑：`CalibrationArtifact` 的解析走既有
`load_calibration_artifact()`。只要 artifact JSON 带上 `cosine_threshold` 与
`score_space`，`RetrievalService` 自动切到 `COSINE_ABSOLUTE`。

`core/config.py` 亦无需新增配置项——阈值归属 `CalibrationArtifact`（与既有
`threshold` 一致的设计意图）。

---

## 7. 错误与降级

### 7.1 降级路径总表

| 场景 | 行为 | `reason` | 患者看到 |
|---|---|---|---|
| 门禁拦下全部候选（知识库确实无相关内容） | `items=[]` | `insufficient_evidence` | "当前没有足够且可定位的医学证据" |
| 两路候选检索均失败（集群宕机） | `items=[]` | `retrieval_unavailable` | "服务暂不可用" |
| embedding 不可用，只有词法一路 | **跳过门禁**，按现状返回词法结果 | `None`（或 `embedding_unavailable` 视既有分支） | 正常回答（降级） |
| `search_vector` 抛错但词法存活 | **跳过门禁**，返回词法结果 | `None` | 正常回答（降级） |
| artifact 声明的 `index_version`/`embedding_model` 与记录不符 | `items=[]`（既有 fail-closed） | `calibration_artifact_mismatch` | "服务暂不可用" |
| 索引不暴露混合候选接缝但配了 artifact | `items=[]`（既有 fail-closed） | `calibration_artifact_mismatch` | "服务暂不可用" |

### 7.2 ⚠️ 关键降级决策：向量路径不可用时**跳过**门禁

这是本次设计**最重要的一个取舍**，必须显式记录。

**问题**：若向量路径未运行（embedding 不可用 / `search_vector` 报错），则"通过余弦门禁的
向量候选"集合为空。此时若照常执行门禁，词法交集会把**全部词法候选一并剔除**，结果是
**每一次查询都拒答**——把一次可降级运行（仍有 BM25 精确术语匹配能力）变成**完全不可用**。

**决策**：向量路径未实际运行时**跳过门禁**，退回现状的 `top_k` 截断，并记
`gate_skipped_reason="vector_path_unavailable"`。

**理由**：
1. 词法证据仍然是**有效证据**。因为向量侧不可用就拒绝所有证据，是把基础设施降级
   放大成产品不可用，对患者是净损失。
2. 反向选择（fail-closed 返回 `retrieval_unavailable`）更糟——它会把"BM25 明明找到了
   相关条款"变成"服务不可用"。
3. 这不是可被利用的绕过路径：embedding 不可用本身是**可观测的降级事件**
   （`diagnostics.embedding_degraded` / `vector_error`），监控应当据此告警，而不是靠
   门禁失败来发现。

**已知代价（显式接受）**：降级模式下系统**恢复"永不拒答"**。该状态下不相关查询会重新
返回词法结果。这是可接受的——降级模式的首要目标是可用性，且该状态已被显式标记。

**恢复条件**：embedding 恢复后门禁自动重新生效，无需人工干预。

### 7.3 门禁与 `_empty_reason` 的顺序不变量

门禁**不得**改变"检索不可用"的判定。实现上通过在门禁之后调用既有 `_empty_reason`
保证：`lexical_errored` / `vector_attempted` / `vector_errored` 的语义与门禁前一致。

---

## 8. 安全与合规不变量（逐条可断言）

| # | 不变量 | 断言方式 |
|---|---|---|
| I1 | 门禁只做减法，不引入新候选 | 门禁后候选集 ⊆ 门禁前候选集（按 id 集合包含） |
| I2 | 越权命中数恒为 0 | 跨患者 / 跨租户用例返回条数 == 0，且 `permission_violations == 0` |
| I3 | 删除残留恒为 0 | 含 `deleted=true` 记录时该记录不出现在结果中 |
| I4 | 复核泄漏恒为 0 | 含 `retrieval_eligible=false` 记录时该记录不出现在结果中 |
| I5 | 门禁不改变"不可用"与"证据不足"的区分 | 两路均失败时 `reason == retrieval_unavailable`（不是 `insufficient_evidence`） |
| I6 | 引用四要素齐全才可返回 | `_has_citation` 仍在门禁前执行；门禁不产生无引用证据 |
| I7 | 私有记录不跨患者/租户 | 既有 ACL 过滤在门禁前执行，门禁不绕过 |
| I8 | 阈值不可硬编码 | 构造期无 artifact/显式参数 → `gate_policy == TOP_K`（行为与今天一致） |

---

## 9. 已发现但本次不修的相邻缺陷（显式上报）

按 `FEATURE-WORKFLOW` 规则 4，不静默绕过：

| # | 缺陷 | 影响 | 处理 |
|---|---|---|---|
| D1 | **`_safe_diagnostics` 会把 `gate_policy` 哈希掉** | `observability/metrics.py:_safe_diagnostics` 对字符串按 key 分派：`gate_policy` 不在 `{"reason","status","index_version","embedding_model"}` 白名单内，故被 `sha256` 哈希。**监控侧读不到门禁策略**，新增的 `cosine_absolute` 同样不可读 | 本次**不修**（会改变既有指标载荷，需独立验收）。列为后续任务，与 `gate_score_space` 一同处理 |
| D2 | `space_type` 未显式声明，依赖 OpenSearch 默认值 | 默认值变更或换引擎会静默改变分数语义 | 阶段 2（需重建索引） |
| D3 | 无运行时 space_type 一致性校验 | artifact 声明的 `score_space` 与实际索引不符时不会 fail-closed | 阶段 2（需新增索引自省接缝） |
| D4 | `deletion_residuals` / `review_required_leakage` 是"假绿" | 语料中无对应记录，测出的 0 不能证明过滤生效 | 独立任务：构造负样本集 |
| D5 | 私有库 4 条重复 + 2 条乱码记录 | 占用 top_k 槽位，拉低查准 | 独立任务：数据治理 |

---

## 10. 测试点

### 10.1 单元测试（`tests/unit/test_retrieval_service.py`）

| 测试 | 断言 |
|---|---|
| 门禁拦下全部候选 → 拒答 | `items == []`、`reason == "insufficient_evidence"`、`gate_policy == "cosine_absolute"` |
| 门禁保留相关候选 | 通过门禁的候选进入结果；被拦候选不出现 |
| 门禁为纯减法（I1） | 门禁后 id 集合 ⊆ 门禁前 id 集合 |
| 词法交集生效 | 词法独有但未通过门禁的候选被剔除 |
| 向量路径未运行 → 跳过门禁（§7.2） | 词法候选**照常返回**，`gate_skipped_reason == "vector_path_unavailable"` |
| `search_vector` 报错 → 跳过门禁 | 同上 |
| 两路均失败 + 门禁配置 → `retrieval_unavailable`（I5） | `reason == "retrieval_unavailable"`，**不是** `insufficient_evidence` |
| 无 artifact 且无显式阈值 → `TOP_K`（I8） | `gate_policy == "top_k"`，行为与既有测试一致 |
| `cosine_threshold` 与 `fused_score_threshold` 同时传入 → 拒 | `pytest.raises(ValueError)` |
| 诊断计数正确 | `vector_gated_candidates` / `vector_candidates_gated_out` / `lexical_gated_out` 数值符合构造 |
| 越权记录被门禁**之前**过滤（I2） | 跨患者记录不出现在结果，且不进入门禁计数 |

### 10.2 契约测试（`tests/unit/test_retrieval_metrics.py`）

| 测试 | 断言 |
|---|---|
| `CalibrationArtifact` 接受 `cosine_threshold` + `score_space` | 构造成功 |
| `cosine_threshold` 无 `score_space` → 拒 | `pytest.raises(ValueError)` |
| `score_space` 无 `cosine_threshold` → 拒 | `pytest.raises(ValueError)` |
| `cosine_threshold` 越界（>1 / <0 / bool）→ 拒 | `pytest.raises(ValueError)` |
| `score_space` 非法取值 → 拒 | `pytest.raises(ValueError)` |
| **旧 artifact（无新字段）行为不变** | 构造成功，`threshold` 语义仍是 RRF |

### 10.3 组装测试（`tests/integration/test_composition_wiring.py`）

| 测试 | 断言 |
|---|---|
| artifact 带 `cosine_threshold` → 组装出 `COSINE_ABSOLUTE` | `retriever.gate_policy is GatePolicy.COSINE_ABSOLUTE` |
| artifact 不带新字段 → 仍是 `CALIBRATED` | 既有测试继续通过 |

### 10.4 端到端真实链路（`scripts/`）

复用 `scripts/retrieval_gate_prototype.py` 的评估口径，但**改为调用生产
`RetrievalService`**（不再是原型类），验证：

1. 拒答用例返回 0 条且 `reason == "insufficient_evidence"`（**2/2**）；
2. 可答用例 Hit@K / Recall@K / MRR / nDCG@K **与现状逐值相等**（零损失）；
3. 安全用例返回 0 条且零容忍三项为 0；
4. 分阶段 P95 相对现状的劣化 ≤ 1.2×（门禁是纯内存过滤，预期 ≈0 增量）。

---

## 11. 实施阶段（每阶段可独立验收）

### 阶段 1（本次）：门禁代码 + 单元/契约/组装测试 + 真实链路验证

- 新增 `GatePolicy.COSINE_ABSOLUTE`
- `CalibrationArtifact` 新增 `cosine_threshold` / `score_space`（含校验）
- `RetrievalService` 新增参数、策略解析、门禁逻辑、诊断键
- 全部测试
- **不改** `space_type`、**不改** `top_k`、**不改** 语料

验收：§10 全部测试通过 + §10.4 四项端到端断言成立。

### 阶段 2（后续）：分数空间显式化

- `OpenSearchIndexWriter` mapping 显式声明 `space_type: "cosinesimil"`
- 重建两个索引（当前均为调试残留语料，重建成本低）
- `score_space` 切换为 `"cosinesimil"`，阈值仍 0.50（0.5 处两空间数值相同，预期零行为变化）
- 新增索引自省接缝，实现 `score_space` 运行时 fail-closed 校验（关闭 D3）

验收：重建后评估指标与阶段 1 完全一致 + `score_space` 不一致时拒绝启动。

### 阶段 3（后续）：`top_k` 调整 + 评估集扩充

- `RETRIEVAL_TOP_K=8 → 3`（依据：k=5 冗余翻倍、k=1 掉召回）
- 评估集扩到 50~100 条真实用例，重新标定阈值
- 补 `gold_evidence_ids` 到 2~3 条/query，使查准率口径可达标

---

## 12. 验收标准（可观测、可复现）

```bash
# 1. 全量测试
python -m pytest tests/ -q

# 2. 真实链路：拒答正确性 + 零损失
python scripts/retrieval_cosine_gate_eval.py

# 预期输出：
#   拒答用例          2/2 正确拒答（reason=insufficient_evidence）
#   可答用例 命中率   1.000  （与现状相等）
#   可答用例 MRR      0.964  （与现状相等）
#   可答用例 nDCG@K   0.946  （与现状相等）
#   零容忍违规        0
#   分阶段 P95 劣化   <= 1.2x
```

**完成判定**（对齐 `FEATURE-WORKFLOW` §5）：
- [ ] spec 已批准，全部目标实现，非目标确实没做
- [ ] 每个任务独立验收
- [ ] 全量测试通过（记录通过/跳过数）
- [ ] 真实链路端到端跑通，有输出证据
- [ ] §8 八条安全不变量逐条断言通过
- [ ] §7 降级路径验证过（含"向量路径不可用 → 跳过门禁"）
- [ ] 偏差与遗留项显式记录（§9）
- [ ] 临时探针文件已清理

---

## 13. spec 自检

| 检查项 | 结果 |
|---|---|
| **占位符扫描** | 无 TBD / TODO。`cosine_threshold` 默认值明确为 0.50 且注明是冷启动初值 |
| **一致性** | §5.2 的字段校验规则与 §10.2 的测试一一对应；§6.1 代码落点与 §4.1 架构图一致；§7.2 的跳过逻辑与 §6.1 代码一致 |
| **范围** | 阶段 1 为单一实施计划可完成的粒度；阶段 2/3 明确拆出 |
| **歧义** | ① "余弦门禁"的名称与 `vector_score` 实为 `1/(1+L2²)` 的冲突 → 已用 `score_space` 字段消歧，并在 §2.4 显式纠正。② `intersect` vs `emptiness` → 已选定 `intersect` 并给出依据。③ 向量不可用时门禁行为 → 已选定"跳过"并给出完整理由与代价 |
