# 按索引分派的标定一致性校验（Per-Index Calibration Matching）设计

## 1. 文档信息

- 日期：2026-09-28
- 状态：**待用户批准**（未批准不写代码）
- 适用项目：`medical-rag`
- 关联 spec：`2026-09-24-cosine-relevance-gate-design.md`（本设计闭合它的一个遗留阻塞）
- 关联评估：`eval_out/门禁启用端到端验证报告.md`（本缺陷的发现现场）
- 关联 workflow：`docs/superpowers/FEATURE-WORKFLOW.md` 规则 2（隐性不变量必须写进文档）、规则 4（偏差显式上报）

---

## 2. 背景

### 2.1 触发场景

2026-09-26 做"余弦门禁启用后端到端验证"时发现：**配置任何 `CALIBRATION_ARTIFACT_PATH`
后，生产默认作用域的每个查询都返回 0 条 + `calibration_artifact_mismatch`。**

实测矩阵（真实 OpenSearch 双索引 + 生产 `RetrievalService`）：

```
artifact.index_version  作用域              返回   判定     reason
public-v1               纯公共(H-EDUCATION)  3    PASS    None
public-v1               纯私有(REPORT)       0    FAIL    calibration_artifact_mismatch
public-v1               混合(LAB/ORDINARY)   0    FAIL    calibration_artifact_mismatch
private-v1              纯公共(H-EDUCATION)  0    FAIL    calibration_artifact_mismatch
private-v1              纯私有(REPORT)       3    PASS    None
private-v1              混合(LAB/ORDINARY)   0    FAIL    calibration_artifact_mismatch

生产默认作用域（混合）可用: False
```

**不存在任何一个 `index_version` 取值能让混合作用域通过。**

而 `query/pipeline.py:_strategy_for` 的默认路由是：

| 意图 | 作用域 | 配 artifact 后 |
|---|---|---|
| `HEALTH_EDUCATION` | 纯公共 | ✅ 可用 |
| `REPORT_EXPLANATION` | 纯私有 | ✅ 可用 |
| **`LAB_INDICATOR` / `ORDINARY`** | **混合** | ❌ **全部拒答** |

"我的血红蛋白是多少"这类核心场景全军覆没。这是**引入门禁反而让系统不可用**的
最坏形态——比不启用更危险。

### 2.2 根因

`retrieval/service.py` 的一致性检查用**单一**期望版本比对**两个索引**的全部候选：

```python
if self.calibration_artifact is not None:
    artifact = self.calibration_artifact
    mismatched = [
        record
        for record in (*lexical, *vector)          # ← 公共 + 私有全部候选
        if record.index_version != artifact.index_version   # ← 单一期望值
        or record.embedding_model != artifact.embedding_model
    ]
```

而两个索引的 `index_version` **按设计就不同**：

| 索引 | `index_version` | 来源（代码事实） |
|---|---|---|
| `medical-rag-public-local` | **`public-v1`** | `KnowledgeIngestService.__init__` 默认 `embedding_index_version="public-v1"`（`ingest_service.py:130`）；镜像于 `ReconciliationService.DEFAULT_PUBLIC_INDEX_VERSION`（`reconciliation.py:128`） |
| `medical-rag-private-local` | **`private-v1`** | `IndexJob.index_version` 默认 |

实测记录分布（只读探针，公共 5 条 / 私有 7 条）：

```
public : index_version {"public-v1": 5}   embedding_model {"BAAI/bge-small-zh-v1.5": 5}
private: index_version {"private-v1": 7}  embedding_model {"BAAI/bge-small-zh-v1.5": 7}
```

**只有 `index_version` 冲突；`embedding_model` 两侧一致。**

### 2.3 关键判断：这是既有缺陷，不是余弦门禁引入

A/B 对照（同一混合作用域查询）：

| 变体 | `gate_policy` | 返回 | reason |
|---|---|---|---|
| A. legacy artifact（仅 `threshold`） | `calibrated` | 0 | `calibration_artifact_mismatch` |
| B. cosine artifact（带 `cosine_threshold`） | `cosine_absolute` | 0 | `calibration_artifact_mismatch` |

一致性检查在门禁逻辑**之前**执行，A 与 B **同样失败**。

> **即：只要在双索引部署里配置任何 artifact，检索就已经整体不可用。**

之所以一直没暴露，是因为生产 `.env` 从未配置 `CALIBRATION_ARTIFACT_PATH`——
整段检查被跳过。这是一个**潜伏至今的既有 P0**（与 2026-09-24 spec 的 §9 遗留项
并列，但严重度更高：它是"装配后立刻不可用"，而非"某个指标不合格"）。

### 2.4 为什么"按索引分版本"是正确语义，而不是放宽校验

项目本身**已经显式按作用域建模版本**。`ReconciliationService` 分两套版本键：

```python
_PRIVATE_VERSION_KEYS = ("parser_version", "embedding_model", "index_version")
_PUBLIC_VERSION_KEYS  = ("parser_version", "index_version")
```

并且 `expected_public` 写 `"index_version": self.public_index_version`（默认 `public-v1`），
`expected_private` 用 `private-v1`。**入库侧本来就承认两索引版本不同。**

因此安全性质的正确定义是：

> **每条记录与「它所在索引」的标定身份相符。**

而不是"所有记录与同一个常量相符"。当前实现把后者当成了不变量，这是一个
**表达错误**——它既过严（双索引必然全灭），又过松（一旦版本恰好相同就无法
区分候选来自哪个索引）。本次修改让该性质的表达**更精确**，不是更松。

---

## 3. 目标与非目标

### 3.1 目标

1. **使双索引部署在配置 artifact 后可用**：混合作用域（`LAB_INDICATOR`/`ORDINARY`）
   的查询恢复正常返回，且余弦门禁正常生效。
2. **保留 fail-closed 的实质**：任何一条记录的标定身份与其所在索引声明不符时，
   仍然 `items=[]` + `calibration_artifact_mismatch`。**不新增"跳过校验"的分支。**
3. **旧 artifact 行为逐字节不变**：不含新字段的 artifact 走既有全局比对路径。
4. **闭合测试覆盖空洞**：新增能真实触发
   `artifact + 双索引 + 混合作用域 + 真实 index_version 差异` 的测试。
5. **配套观测**：新增诊断键，使"哪条记录、属于哪个索引、期望什么版本"可追溯。

### 3.2 非目标（本次明确不做）

| 非目标 | 原因 |
|---|---|
| **不改 `embedding_model` 的校验方式** | 两侧模型名实测一致（都是 `BAAI/bge-small-zh-v1.5`）。保持既有全局比对——它是对"向量空间身份"的约束，本就应当全局统一。**改它没有收益，只会扩大变更面。** |
| **不统一两索引的 `index_version` 取值** | 需重建两个索引，且与 `ReconciliationService` 的按作用域建模相冲突（那是既有正确设计）。属阶段 2 的"版本标识规范"，独立验收。 |
| **不做运行时 `space_type` 自省** | 沿用 2026-09-24 spec 的阶段 2 结论，本次不引入索引自省接缝。 |
| **不改 `_safe_diagnostics` 白名单** | 见 §8 D1。新增诊断键会被哈希，但这是既有行为，独立验收。 |
| **不改 `CALIBRATION_ARTIFACT_PATH` 的部署状态** | 本设计只让"启用"成为可能；是否启用由运维决策。 |
| **不修订 `Settings.embedding_index_version`** | 实测**没有任何调用点**（`_knowledge_ingest_service` 未传该参数）。属死配置，本次不动（见 §8 D2）。 |

---

## 4. 总体架构

### 4.1 变更位置

只有一处生产代码路径变化——`RetrievalService._retrieve_hybrid()` 的一致性检查块：

```text
词法候选 ─┐
          ├─► ACL/引用过滤 ─► 【变更：按 index_name 分派期望版本的标定校验】
向量候选 ─┘                        │
                                   ├─ 任一条不符 → items=[] + calibration_artifact_mismatch（fail-closed 保留）
                                   └─ 全部相符 → 余弦门禁 ─► RRF 融合 ─► Top-K
```

### 4.2 期望版本解析规则

```text
对每条候选 record：
    expected = artifact.expected_index_version_for(record)

expected_index_version_for(record):
    legacy = artifact.public_index_version is None and artifact.private_index_version is None
    if legacy:
        return artifact.index_version          # ← 旧 artifact：全局单一版本（行为不变）
    if artifact.public_index_version is None or artifact.private_index_version is None:
        return None                            # ← 半配置：不可解析（见 §4.3）
    if record 来自公共索引:
        return artifact.public_index_version
    if record 来自私有索引:
        return artifact.private_index_version
    return None                                # ← 归属未知：不可解析
```

### 4.3 两个必须显式决策的边界

**决策 1：半配置（只给了其中一个版本字段）→ 构造期拒绝，而非运行期 fail-closed。**

理由：半配置是一个**不可能正确**的状态——另一侧索引的期望版本无从得知，
任何查询只要命中该侧就必然 fail-closed。这与 `cosine_threshold`/`score_space`
的"成对必填"完全同构，复用同一条设计原则：**拒绝，而不是猜测调用方意图。**

**决策 2：候选归属未知（`record.index_name is None` 或不在两个索引内）→ fail-closed。**

理由：无法判定"它应当是什么版本"，就无法声称它已通过校验。这与既有
`_record_from_hit` 对 `require_index=True` 的处理一致——**无法绑定 ACL 边界的命中
直接丢弃**。本次不引入新的宽松分支。

> 注：生产路径上 `search_lexical`/`search_vector` 都以 `require_index=True` 构造记录
> （`opensearch_store.py:346`），因此 `index_name` 必然存在。该分支是防御性的，
> 面向自定义 `SearchIndex` 实现。

### 4.4 归属判定

不新增索引名解析逻辑，**复用 `OpenSearchStore` 已有的判定口径**
（`opensearch_store.py:384-389`）：索引名相等，或以其加 `-` 为前缀。

```python
is_public = index_name == public_index_name or index_name.startswith(public_index_name + "-")
```

问题：`RetrievalService` 只持有 `SearchIndex` 协议，不保证是 `OpenSearchStore`，
拿不到 `public_index`/`private_index` 属性。

**解法（选定）**：用 `record.visibility` 判定归属，而不是索引名。

- `visibility` 是 `SearchRecord` 上的**协议字段**（`contracts.py:226`），不依赖具体实现；
- 索引与可见性已被 `_record_from_hit` 交叉校验（公共索引必须 `visibility=public`，
  私有索引必须 `visibility=private`，否则返回 `None`）。因此 `visibility` 在此
  等价于索引归属，且**已被上游强校验**；
- 不引入新参数、不改 `RetrievalService` 构造签名。

这也让 §4.3 决策 2 的"归属未知"收敛为"`visibility` 不在已知枚举内"，
无需访问 `index_name`。

---

## 5. 核心数据契约

### 5.1 `CalibrationArtifact` 新增字段

```python
@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    index_version: str
    embedding_model: str
    rrf_k: int
    threshold: float
    eval_dataset_version: str
    cosine_threshold: float | None = None
    score_space: str | None = None
    # --- 新增（可选；二者必须成对出现）---
    #: 公共索引记录的期望 index_version。提供后，公共记录与它比对，
    #: 私有记录与 private_index_version 比对（替代全局 index_version 比对）。
    public_index_version: str | None = None
    #: 私有索引记录的期望 index_version。与 public_index_version 成对必填。
    private_index_version: str | None = None
```

**字段语义**：

| 字段 | 既有/新增 | 作用 |
|---|---|---|
| `index_version` | 既有 | **语义收窄但不改变默认行为**：当 `public_index_version`/`private_index_version` 均未提供时，它是全局期望版本（旧行为）；提供后，它退化为"artifact 标识"元数据，不再参与逐记录比对 |
| `public_index_version` | 新增 | 公共记录期望版本 |
| `private_index_version` | 新增 | 私有记录期望版本 |

> ⚠️ **这是本次设计最需要评审的一处**：`index_version` 的含义随新字段出现而改变。
> 与 2026-09-24 spec 中"复用 `threshold` 会导致静默语义变更"的判据一致吗？
> **一致但不构成问题**，因为：① 新字段存在时 `index_version` 不再参与门禁判定，
> 不存在"同一个值被两种读法解读"的歧义；② 旧 artifact（无新字段）行为逐字节不变；
> ③ artifact 的 `index_version` 将被写入 `diagnostics.calibration_artifact` 供追溯，
> 其值可见。为消除残留歧义，**docstring 必须显式写明这条收窄**。

校验规则（`__post_init__` 追加）：

- `public_index_version` 与 `private_index_version` **必须成对提供或成对省略**；
  只提供一个 → `ValueError`（半配置不可能正确）。
- 任一非 None 时必须为非空字符串（strip 后非空），与既有 `index_version` 同规则。
- 均为 None（旧 artifact）→ 保持既有全局比对语义，行为不变。

### 5.2 `CalibrationArtifact` 新增方法

```python
def expected_index_version_for(self, visibility: str) -> str | None:
    """返回某条记录（按可见性判定归属）应当匹配的 index_version。

    返回 None 表示「无法解析期望值」——调用方必须 fail-closed，
    而不是跳过校验。None 只出现在两种情形：
      * 半配置（已由 __post_init__ 拒绝构造，运行期仅为防御）；
      * visibility 不是已知的 public/private。
    """
```

放在 `CalibrationArtifact` 上而不是 `RetrievalService` 里，理由：
**这条规则属于契约本身**（"artifact 如何定义自己的适用范围"），
放服务里会让契约与校验逻辑分居两处，未来第三处调用必然漏抄。

### 5.3 `RetrievalService` 一致性检查（变更后）

```python
if self.calibration_artifact is not None:
    artifact = self.calibration_artifact
    breaking = [
        record
        for record in (*lexical, *vector)
        if self._record_breaks_calibration(record, artifact)
    ]
    # 按 id 去重（保留首次出现）：同一条记录可能同时来自词法与向量两路。
    unique: dict[str, SearchRecord] = {}
    for record in breaking:
        unique.setdefault(record.id, record)
    mismatched = list(unique.values())
    if mismatched:
        diagnostics["calibration_artifact_mismatch"] = True
        diagnostics["status"] = "degraded"
        # 新增：可追溯（不泄漏文本，只报身份）
        diagnostics["calibration_mismatch_records"] = tuple(
            {
                "id": r.id,
                "visibility": str(r.visibility),
                "index_version": r.index_version,
                "embedding_model": r.embedding_model,
            }
            for r in mismatched[:5]
        )
        diagnostics["calibration_mismatch_count"] = len(mismatched)
        diagnostics["stage_elapsed_ms"]["total"] = (perf_counter() - started) * 1000
        return RetrievalResult(
            items=[], reason=REASON_CALIBRATION_MISMATCH, diagnostics=diagnostics
        )
```

> **去重是实现期修正（2026-09-28）**：初版伪码直接遍历 `(*lexical, *vector)` 而未去重，
> 导致同一条记录被两路命中时 `calibration_mismatch_count` 虚高约一倍
> （实测 `1` 报成 `2`），并让只保留 5 条的 `calibration_mismatch_records` 被同一条
> 重复占满——两者都违背 §5.4 把该值定义为「不符**记录**数」与「可追溯哪几条记录不符」
> 的用途。去重后计数值与 §5.4 的定义一致。回归测试：
> `test_mismatch_count_deduplicates_records_hit_by_both_routes`。

```python
@staticmethod
def _record_breaks_calibration(record: SearchRecord, artifact: CalibrationArtifact) -> bool:
    """记录是否破坏了它所在索引的标定身份。

    两个维度独立判定：
      * embedding 模型 → 全局统一（向量空间身份，不因索引而异）；
      * index_version  → 按索引分派（各索引按设计可有不同版本）。
    期望版本无法解析（None）即视为破坏——fail-closed，不跳过校验。
    """
    if record.embedding_model != artifact.embedding_model:
        return True
    expected = artifact.expected_index_version_for(str(record.visibility))
    return expected is None or record.index_version != expected
```

### 5.4 `diagnostics` 新增键

| 键 | 类型 | 含义 |
|---|---|---|
| `calibration_mismatch_count` | int | 新增；不符**记录数**（按 `id` 去重后计数，同一条被两路命中只计一次；仅在不符时出现） |
| `calibration_mismatch_records` | tuple[dict,...] | 新增；最多 5 条不符记录的身份（`id`/`visibility`/`index_version`/`embedding_model`），**不含正文** |

**为什么需要**：当前只有一个布尔 `calibration_artifact_mismatch`。运维看到
"全部查询不可用"时，无法从诊断得知**是哪个索引的哪条记录不符、期望什么版本**——
本次缺陷排查就为此绕了很久。新增键只暴露身份标识，与既有
`diagnostics["ranks"]`（已暴露 `record.id` 与分数）同级，不新增隐私面。

---

## 6. 设计细节

### 6.1 与余弦门禁的顺序关系

**不变**。一致性检查仍在 ACL/引用过滤**之后**、余弦门禁**之前**：

```text
... ACL/引用过滤 ─► 【标定一致性校验（本次变更）】─► 【余弦门禁】─► fuse()
```

理由见 2026-09-24 spec §6.1（门禁只做减法，融合只应看到相关候选）。
本次变更只改一致性校验的**判据**，不改其**位置**，也不改门禁位置。

### 6.2 门禁不受影响的证明义务

变更后必须验证：**余弦门禁在混合作用域下真正生效**，即
- 混合作用域的拒答用例返回 0 条 + `insufficient_evidence`（**不是** mismatch）；
- 混合作用域的可答用例返回正常条数，且排序指标与 baseline 相等。

这是本次真正的验收核心——"能启用了"必须由"门禁确实在工作"来证明，
而不是"不再报 mismatch"。

### 6.3 `embedding_model` 为何保持全局比对

`embedding_model` 标识**向量空间身份**（哪个模型产出的向量）。两个索引的向量
必须来自同一空间才能互相比较、才能与查询向量比较。因此它**本就应该全局统一**，
按索引分派反而是错的（会允许公共用一个模型、私有用另一个模型，而 RRF 融合
会把两组不可比的分数放在一起排序）。

实测两侧一致 → 全局比对不产生任何可用性问题 → **不动**。

### 6.4 向后兼容矩阵

| artifact 形态 | `index_version` 作用 | 结果 |
|---|---|---|
| 旧 artifact（无新字段） | 全局期望版本 | **行为逐字节不变**（双索引下仍 fail-closed，这是既有正确语义） |
| 新 artifact（成对提供） | 仅作元数据 | 按可见性分派比对；双索引部署可正常服务 |
| 半配置（只给一个） | — | **构造期 `ValueError`** |

---

## 7. 错误与降级

### 7.1 降级路径总表（仅列受影响行）

| 场景 | 行为 | `reason` | 患者看到 |
|---|---|---|---|
| 记录版本与其所在索引声明不符 | `items=[]`（fail-closed 保留） | `calibration_artifact_mismatch` | "服务暂不可用" |
| **混合作用域 + 新 artifact + 版本相符** | **正常返回**（本次修复目标） | `None` 或 `insufficient_evidence`（由门禁决定） | 正常回答 |
| 记录 `visibility` 非 public/private（防御） | `items=[]` | `calibration_artifact_mismatch` | "服务暂不可用" |
| 半配置 artifact | **启动即失败**（构造期） | — | 部署不启动（正确的失败模式） |
| 旧 artifact + 双索引 | `items=[]`（既有） | `calibration_artifact_mismatch` | "服务暂不可用" |

### 7.2 为什么不给"旧 artifact + 双索引"加自动降级

有人会提议：既然旧 artifact 在双索引下必然不可用，何不自动放宽？

**不做**，理由：
1. 那会**静默改变已部署 artifact 的校验语义**——正是 2026-09-24 spec 明确拒绝的
   模式（FEATURE-WORKFLOW 规则 4）。
2. 旧 artifact 的 `index_version` 是调用方**显式声明的期望**。系统无权替它
   推断"你其实想要按索引分派"。
3. 有明确的迁移路径：在 artifact 里加两个字段。**代价可见、动作明确**，
   远优于一次隐式的语义变更。

---

## 8. 安全与合规不变量（逐条可断言）

| # | 不变量 | 断言方式 |
|---|---|---|
| I1 | 校验只做减法，不改变候选集 | 校验通过时，门禁前候选集与变更前**逐 id 相等** |
| I2 | 无可解析期望版本时 **fail-closed**，不跳过 | `expected_index_version_for` 返回 None → 结果 `items == []` + mismatch |
| I3 | 半配置 artifact **构造期即拒** | `pytest.raises(ValueError)`，不进入运行期 |
| I4 | 旧 artifact 行为**逐字节不变** | 既有 mismatch 测试（`test_retrieval_service.py:541`）与 `test_scheme_b_rollout.py` 全部继续通过，**不作任何修改** |
| I5 | `embedding_model` 仍全局校验 | 仅模型名不符（版本相符）→ 仍 `items == []` + mismatch |
| I6 | 越权/删除残留/复核泄漏恒为 0 | 三项零容忍在真实链路断言为 0（沿用既有口径） |
| I7 | 越权记录不进入 mismatch 诊断 | 跨患者记录被 ACL 过滤在校验**之前**，不出现在 `calibration_mismatch_records` |
| I8 | 诊断不泄漏正文 | `calibration_mismatch_records` 仅含 id/visibility/版本标识，无 `text` |
| I9 | 门禁在混合作用域下确实生效 | 混合拒答用例 0 条 + `insufficient_evidence`；混合可答用例排序指标与 baseline 相等 |

> I4 是本次**最重要的回归护栏**。若实现里出现了"让旧 artifact 也变可用"的
> 副作用，该测试会立刻失败。

---

## 9. 已发现但本次不修的相邻缺陷（显式上报）

| # | 缺陷 | 影响 | 处理 |
|---|---|---|---|
| D1 | `_safe_diagnostics` 把新增诊断键哈希掉 | `metrics.py:491` 对未知字符串键走 `_hash`；`calibration_mismatch_records` 是 tuple-of-dict，会落到 `_hash` 分支。**监控侧读不到 mismatch 明细** | 本次**不修**（改白名单会改变既有指标载荷，需独立验收）。与既有 `gate_policy` 问题（2026-09-24 spec D1）合并处理 |
| D2 | `Settings.embedding_index_version` 是死配置 | 实测无任何调用点（`_knowledge_ingest_service` 未传参）。运维改它不会影响入库 | 独立任务：删除或接入。**配 artifact 时易误以为 public 侧版本可配** |
| D3 | 两索引版本取值（`public-v1`/`private-v1`）无语义规范 | 命名无统一约定，靠默认值巧合 | 阶段 2：版本标识规范 + 重建索引 |
| D4 | `deletion_residuals` / `review_required_leakage` 仍是假绿 | 语料无对应记录 | 独立任务：构造负样本集（沿既有清单） |
| D5 | 索引名归属判定逻辑在 `OpenSearchStore` 内，未提为协议 | 自定义实现可能不遵循"索引名 + `-` 前缀"约定 | 本次绕开（用 `visibility` 判定，§4.4）。若未来有第二实现再提为协议 |

---

## 10. 测试点

### 10.1 契约测试（`tests/unit/test_retrieval_metrics.py`）

| 测试 | 断言 |
|---|---|
| 成对提供两字段 → 构造成功 | 字段值正确保留 |
| 只给 `public_index_version` → 拒 | `pytest.raises(ValueError)`，消息含 "pair"/"private_index_version" |
| 只给 `private_index_version` → 拒 | 同上 |
| 空字符串 → 拒 | `pytest.raises(ValueError)` |
| **旧 artifact（无新字段）→ 行为不变** | 构造成功，`index_version` 语义仍是全局 |
| `expected_index_version_for("public")` 分派正确 | 返回 `public_index_version` |
| `expected_index_version_for("private")` 分派正确 | 返回 `private_index_version` |
| 旧 artifact 下 `expected_index_version_for(任意)` | 返回 `index_version`（全局） |
| 未知 visibility → 返回 None | 返回 `None`（调用方据此 fail-closed） |

### 10.2 服务测试（`tests/unit/test_retrieval_service.py`）

| 测试 | 断言 |
|---|---|
| **混合作用域 + 两索引版本不同 + 新 artifact → 正常返回**（核心修复） | `items` 非空，`reason is None` |
| **旧 artifact + 两索引版本不同 → 仍 fail-closed**（I4/§7.2） | `items == []`，`reason == "calibration_artifact_mismatch"` |
| 新 artifact + 私有记录版本不符 → fail-closed | `items == []` + mismatch |
| 新 artifact + 公共记录版本不符 → fail-closed | `items == []` + mismatch |
| 仅 `embedding_model` 不符（版本相符）→ fail-closed（I5） | `items == []` + mismatch |
| 未知 visibility → fail-closed（I2） | `items == []` + mismatch |
| mismatch 诊断含身份但不含正文（I8） | `calibration_mismatch_records` 无 `text` 键；`calibration_mismatch_count` 正确 |
| 校验通过时候选集不变（I1） | 门禁前候选 id 集合与变更前相等 |
| 越权记录不进 mismatch 诊断（I7） | 跨患者记录不出现在 `calibration_mismatch_records` |

> **注意（本项目已知坑）**：`_DisjointCandidates` 假索引两路同源自洽，
> 必须显式构造**公共与私有版本不同**的候选集，否则测不出本次缺陷。
> 这正是原测试空洞的成因——新测试必须显式设置 `visibility` 与 `index_version`。

### 10.3 组装测试（`tests/integration/test_composition_wiring.py`）

| 测试 | 断言 |
|---|---|
| 带两字段的 artifact JSON → 组装成功 | `retriever.calibration_artifact.public_index_version` 正确 |
| 半配置 JSON → 组装期抛 `ValueError` | `pytest.raises(ValueError)` |

### 10.4 端到端真实链路（`scripts/`）

扩展现有 `scripts/retrieval_cosine_gate_eval.py`，**改为经 artifact 装配**路径
（当前它直接传 `cosine_threshold` 参数，恰好绕过了本缺陷）：

| 断言 | 期望 |
|---|---|
| ① 拒答正确率（**含混合作用域**） | 2/2，`reason == "insufficient_evidence"` |
| ② 可答用例 Hit/Recall/MRR/nDCG 与 baseline 逐值相等 | 差值为 0 |
| ③ 零容忍违规 | 0 |
| ④ P95 劣化 | ≤ 1.2×（预热 2 轮 + 计时 25 轮） |
| ⑤ **新增**：混合作用域查询返回条数 | `> 0`（修复前为 0） |
| ⑥ **新增**：`calibration_artifact_mismatch` 出现次数 | **0**（修复前为 16） |

---

## 11. 实施阶段（每阶段可独立验收）

### 阶段 1（本次）：按索引分派的校验 + 测试空洞闭合 + 真实链路验证

- `CalibrationArtifact` 新增 `public_index_version`/`private_index_version` + 成对校验
- 新增 `expected_index_version_for(visibility)`
- `RetrievalService` 一致性检查改为按可见性分派 + 新增 mismatch 诊断
- 全部测试（含显式的"双索引版本不同"用例）
- 改写验收脚本走 artifact 装配路径
- **不改** `embedding_model` 校验、**不改** `space_type`、**不改** `top_k`、**不改** 语料

验收：§10 全部测试通过 + §10.4 六项断言成立。

### 阶段 2（后续）：版本标识规范 + 索引自省

- 统一/规范两索引 `index_version` 命名约定（关闭 D3）
- 处理死配置 `Settings.embedding_index_version`（关闭 D2）
- 索引名归属判定提为协议（关闭 D5）

### 阶段 3（后续）：启用部署

- 写正式 artifact JSON（含两索引版本 + `cosine_threshold` + `score_space`）
- 配置 `CALIBRATION_ARTIFACT_PATH`，做启用后端到端验收
- 监控告警接入（含 D1 白名单修复）

---

## 12. 验收标准（可观测、可复现）

```bash
# 1. 全量测试（记录通过/跳过数，基线：730 passed / 3 skipped）
python -m pytest tests/ -q

# 2. 真实链路：经 artifact 装配的余弦门禁验收
python scripts/retrieval_cosine_gate_eval.py

# 预期输出：
#   拒答用例（含混合作用域）  2/2（reason=insufficient_evidence）
#   混合作用域返回条数        > 0        （修复前 = 0；实测 legacy 口径 = 5）
#   calibration_artifact_mismatch 出现次数   0（修复前**实测** = 6 用例 / 42 记录）
#   可答用例 命中率/召回/MRR/nDCG   与 baseline 逐值相等
#   零容忍违规                0
#   P95 劣化                  <= 1.2x（实测 1.01–1.02x）
```

> 注：上文"修复前 = 16"为 spec 初稿的未验证估值，实测更正为 **6 用例 / 42 记录 / 5 混合零返回**
> （`eval_out/cosine_gate_eval.json` 的 `legacy_demo` 块）。所有产物使用实测值。

**完成判定**（对齐 `FEATURE-WORKFLOW` §5）：

- [x] spec 已批准，全部目标实现，非目标确实没做
- [x] 每个任务独立验收
- [x] 全量测试通过（**754 passed / 3 skipped / 0 failed**，基线 730）
- [x] 真实链路端到端跑通，有输出证据（`eval_out/cosine_gate_eval.json`，断言 ①–⑥ 全 PASS）
- [x] §8 九条安全不变量逐条断言通过（**尤其 I4：旧 artifact 行为不变** —— 既有测试未改仍通过，
      且 `--legacy-demo` 现场复现双索引 fail-closed）
- [x] §7 降级路径验证过（版本不符 fail-closed / 半配置启动即失败 / 旧 artifact 双索引仍 fail-closed）
- [x] 偏差与遗留项显式记录（§9 D1–D5 + plan「执行完成记录」的规则 4 偏差上报）
- [x] 临时探针文件已清理（`.workbuddy/` 仅剩 `memory/`）

**执行完成记录**见 plan 文末「执行完成记录（2026-09-28）」——含 1 处实施期发现的真实实现缺陷
（`calibration_mismatch_count` 跨路径重复计数，已按 id 去重修复）。

---

## 13. spec 自检

| 检查项 | 结果 |
|---|---|
| **占位符扫描** | 无 TBD / TODO。所有字段默认值、错误消息关键词、验收数值均已明确 |
| **一致性** | §5.1 校验规则与 §10.1 测试一一对应；§4.2 解析规则与 §5.2 方法体一致；§4.4 用 `visibility` 判定与 §6.3 `embedding_model` 全局比对不冲突（两个独立维度）；§8 I4 与 §7.2 决策一致 |
| **范围** | 阶段 1 是单一实施计划可完成的粒度（1 个契约 + 1 个服务方法 + 诊断键 + 测试）；阶段 2/3 明确拆出 |
| **歧义** | ① `index_version` 语义随新字段收窄 → §5.1 显式标注并要求 docstring 写明，且 §6.4 给出兼容矩阵。② 归属判定用索引名还是 `visibility` → §4.4 选定 `visibility` 并给出三条理由（协议字段、已强校验、不扩构造签名）。③ 半配置是运行期 fail-closed 还是构造期拒绝 → §4.3 决策 1 选定构造期拒绝并给出理由。④ 旧 artifact 是否自动放宽 → §7.2 明确拒绝并给出三条理由 |
