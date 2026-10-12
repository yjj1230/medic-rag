# 余弦相关性门禁：恢复性实施规格（Re-implementation Spec）

## 1. 文档信息

- 日期：2026-10-12
- 状态：**待用户批准**（未批准不写代码）
- 性质：**恢复性实施**（restoration），**非新设计**
- 上游 spec（已批准、已验收，代码随 `src/` 丢失）：
  - `docs/superpowers/specs/2026-09-24-cosine-relevance-gate-design.md`（门禁本体，阶段 1）
  - `docs/superpowers/specs/2026-09-28-per-index-calibration-matching-design.md`（闭合前置 P0-1）
- 上游 plan：`docs/superpowers/plans/2026-09-24-cosine-relevance-gate-implementation.md`、
  `2026-09-28-per-index-calibration-matching-implementation.md`
- 关联报告：`docs/检索模块评估与调参报告.md`（§0/§3/§4 实测数据）、`docs/RAG项目专业评审报告-2026-10-12.md`（P0-4）
- 本 spec 的定位：**这三份设计已定论且经真实链路验收（4/4 + 双索引 6 项断言），但实现随源码丢失。本文只规定"把已验证的实现重建到当前恢复的源码树上"，不重新设计、不重新标定。**

---

## 2. 背景与现状取证

### 2.1 事实链（逐条可复核）

| # | 事实 | 证据 |
|---|---|---|
| F1 | 交付目录中 `src/`、`tests/`、`scripts/` 丢失，git 对象库损坏 | `git log` → `fatal: bad object HEAD`；git 索引含 `src/` 96、`tests/` 84 个文件 |
| F2 | 后端源码唯一副本 `.i18n_backup_src_medical_rag/` 是 **09-24/09-28 修复之前**的版本 | 该副本 grep `score_space` / `cosine_threshold` **零命中**；`GatePolicy` 只有 4 个取值 |
| F3 | 标定 artifact **存在且是修复后口径** | `config/calibration/cosine-gate-2026-09-26.json` 含 `cosine_threshold:0.50`、`score_space:"l2_inv"`、`public_index_version:"public-v1"`、`private_index_version:"private-v1"` |
| F4 | `.env` **已启用** artifact 路径 | `.env:66` `CALIBRATION_ARTIFACT_PATH=config/calibration/cosine-gate-2026-09-26.json`（未注释） |
| F5 | 因此当前源码 + 当前配置 = **启动即失败** | `load_calibration_artifact()` 执行 `CalibrationArtifact(**payload)`；旧 dataclass 无这 4 个字段 → `TypeError` → 被包装为 `ValueError("incomplete or has unknown fields")` → fail-closed 抛出，应用起不来 |
| F6 | `vector_score` 语义已由本次 P1-5 修复消歧，可直接作为门禁输入 | 新增 `VECTOR_SCORE_SPACE = "l2_inv"`（`retrieval/opensearch_store.py`）、`SearchRecord.vector_score_space`（`retrieval/contracts.py`）、诊断键 `vector_score_space` |

### 2.2 结论

**这不是"要不要做门禁"的问题，而是"代码与已生效的配置/artifact 严重错位"的问题。** 恢复实现是解除启动阻塞的唯一正解；临时注释 `.env:66` 只能让服务起来，但会**永久关闭拒答能力**（回到评审 P0-4 的原始缺陷），故不作为方案。

### 2.3 原实现已验收的结论（本次必须逐值复现，不得放宽）

| 断言（来自 `检索模块评估与调参报告.md`） | 原验收结果 |
|---|---|
| 拒答用例 2/2 正确拒答（`reason=insufficient_evidence`） | ✅ |
| Hit/Recall/MRR/nDCG 与 baseline **逐值相等**（零损失） | ✅ |
| 零容忍违规 == 0 | ✅ |
| 端到端 P95 劣化比 ≤ 1.2× | ✅ 1.01–1.02× |
| 双索引下混合作用域可答用例返回 0 条 | 修复前 **5** → 修复后 **0** |
| `calibration_artifact_mismatch` 用例数 | 修复前 **6**（42 条记录）→ 修复后 **0** |
| 全量测试 | 754 passed / 3 skipped / 0 failed（基线 730，新增 24） |

---

## 3. 恢复范围

### 3.1 必须恢复（缺失即启动失败或能力缺失）

| # | 组件 | 依据 |
|---|---|---|
| R1 | `CalibrationArtifact` 4 个新字段 + `__post_init__` 校验 + `expected_index_version_for()` | 09-24 spec §5.2、09-28 spec §4 |
| R2 | `GatePolicy.COSINE_ABSOLUTE` 枚举值 | 09-24 spec §5.1 |
| R3 | `RetrievalService` 新构造参数 + 策略解析优先级 + 互斥校验 | 09-24 spec §5.3 |
| R4 | 门禁逻辑（候选级 intersect，落点在 ACL 过滤后、`fuse` 前） | 09-24 spec §6.1 |
| R5 | 按 `visibility` 分派的标定比对（替代单一 `index_version` 全局比对） | 09-28 spec §4 |
| R6 | 诊断键：`gate_score_space`、`vector_gated_candidates`、`vector_candidates_gated_out`、`lexical_gated_out`、`gate_skipped_reason`、`calibration_mismatch_records` | 09-24 §5.4、09-28 §6 |
| R7 | 测试：单元 / 契约 / 组装（原 24 项新增测试的等价重建） | 09-24 §10、09-28 |
| R8 | 端到端验收脚本（经**正式 artifact 文件**装配） | 09-24 §10.4、09-28 |

### 3.2 明确不做（沿用原 spec 的非目标）

- 不改 `space_type` 为 `cosinesimil`（需重建索引，属阶段 2）
- 不重标定 `cosine_threshold`（维持 0.50，`l2_inv` 空间）
- 不改 `top_k`（阶段 3）
- 不做运行时 `space_type` 自省（阶段 2；本次仅 `score_space` 显式声明）
- 新增**不修**项：`_safe_diagnostics` 哈希掉 `gate_policy`/`gate_score_space`（D1，独立验收）

---

## 4. 设计要点（逐条照搬原 spec，仅记录本树特有偏差）

### 4.1 契约（R1/R2）

```python
class GatePolicy(StrEnum):
    CALIBRATED = "calibrated"
    EXPLICIT_THRESHOLD = "explicit_threshold"
    RANK_GATE = "rank_gate"
    TOP_K = "top_k"
    COSINE_ABSOLUTE = "cosine_absolute"      # 新增

@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    index_version: str
    embedding_model: str
    rrf_k: int
    threshold: float                          # 语义不变：RRF 归一化阈值
    eval_dataset_version: str
    cosine_threshold: float | None = None     # 新增
    score_space: str | None = None            # 新增：{"l2_inv","cosinesimil"}
    public_index_version: str | None = None   # 新增
    private_index_version: str | None = None  # 新增
```

校验规则（缺一即拒，杜绝"半配置"）：
1. `cosine_threshold` 非 None → 必须 `0.0 ≤ v ≤ 1.0`、非 bool；
2. `cosine_threshold` 与 `score_space` **成对**（缺一 → `ValueError`）；
3. `score_space` ∈ `{"l2_inv","cosinesimil"}`；
4. `public_index_version` 与 `private_index_version` **成对**（缺一 → `ValueError`）；
5. **旧 artifact（4 字段全缺）行为逐字节不变**：`expected_index_version_for()` 回退到全局 `index_version`。

### 4.2 策略解析优先级（R3）

```text
artifact.cosine_threshold is not None        → COSINE_ABSOLUTE（阈值取 artifact.cosine_threshold）
elif artifact is not None                    → CALIBRATED
elif cosine_threshold is not None            → COSINE_ABSOLUTE（显式参数）
elif fused_score_threshold is not None       → EXPLICIT_THRESHOLD
elif rank_gate is not None                   → RANK_GATE
else                                         → TOP_K
```
`cosine_threshold` 与 `fused_score_threshold`/`rank_gate` **不得同时显式传入**（fail-fast）。

### 4.3 门禁落点与语义（R4）

```text
词法候选 ─┐
          ├─ ACL/引用过滤 ─► 【余弦门禁：vector>=threshold，词法取交集】 ─► RRF 融合 ─► 重排 ─► top_k
向量候选 ─┘                              │
                                        └─ 全被拦 → items=[] + insufficient_evidence
```

判定链（09-24 §4.3 原文）：
1. 向量路径是否实际运行（`vector_attempted and not vector_errored`）？
   - **否 → 跳过门禁**（`gate_skipped_reason="vector_path_unavailable"`），退回 top_k 截断；
   - **是 → 继续**。
2. 保留 `vector_score >= cosine_threshold` 的向量候选；
3. 词法候选仅保留 id 出现在第 2 步结果中的（**intersect**）；
4. 结果为空 → `items=[]` + `_empty_reason(...)`（两路均失败仍为 `retrieval_unavailable`）。

**⚠️ 本条是原 spec 最重要的取舍**：embedding 不可用时若照常执行门禁，词法交集会把全部词法候选剔除 → 每次查询都拒答。故"跳过门禁"是**有意降级**，代价（该状态下恢复"永不拒答"）显式接受并已在诊断中标记。

### 4.4 与本次 P1-6 改动的关系（本树特有偏差，必须记录）

今日 P1-6 修复把 **fused-score / rank 门禁**移到了 rerank 之后（`fuse → rerank → gate → top_k`）。
两者作用于**不同阶段**，不冲突，且配置上互斥：

| 门禁 | 作用对象 | 落点 |
|---|---|---|
| `COSINE_ABSOLUTE`（本次恢复） | 候选级（单路向量分数 + 词法交集） | **`fuse` 之前** |
| `CALIBRATED` / `EXPLICIT_THRESHOLD` / `RANK_GATE`（既有） | 融合后候选集（`calibrated_score`/名次） | `rerank` 之后 |

`P1-5` 新增的 `vector_score_space`（记录级标注）与本次的 `gate_score_space`（阈值级语义）是**两个键**，
前者可用于将来交叉校验（阶段 2 运行时自省的基础），本次**只记录、不据此 fail-closed**。

### 4.5 标定分派（R5）

```python
def expected_index_version_for(self, visibility: str) -> str | None:
    # 两字段均缺 → 回退全局 index_version（旧行为）
    # 按 visibility 分派：public → public_index_version；private → private_index_version
    # visibility 不在已知枚举内 → None（调用方 fail-closed）
```
- `embedding_model` **保持全局校验**（向量空间身份不因索引而异）；
- 不符记录写入 `calibration_mismatch_records`（**最多 5 条**，只含 `id`/`visibility`/`index_version`/`embedding_model`，**不含正文** —— 防 PHI 出诊断面）。

---

## 5. 安全与合规不变量（逐条可断言，沿用 09-24 §8）

| # | 不变量 | 断言方式 |
|---|---|---|
| I1 | 门禁只做减法 | 门禁后 id 集 ⊆ 门禁前 id 集 |
| I2 | 越权命中恒 0 | 跨患者/租户用例 0 条 + `permission_violations == 0` |
| I3/I4 | 删除残留 / 复核泄漏恒 0 | `deleted=true` / `retrieval_eligible=false` 记录不出现 |
| I5 | "不可用" ≠ "证据不足" | 两路均失败 → `retrieval_unavailable` |
| I6 | 引用四要素齐全 | `_has_citation` 仍在门禁前执行 |
| I7 | 私有记录不跨患者 | ACL 过滤在门禁前 |
| I8 | 阈值不硬编码 | 无 artifact/无显式参数 → `TOP_K` |
| I9（新增） | 诊断面无 PHI | `calibration_mismatch_records` 不含正文 |

---

## 6. 测试与验收

### 6.1 测试点（对照 09-24 §10 / 09-28）

- **单元**（`tests/unit/test_retrieval_service.py`）：拒答、保留相关、纯减法(I1)、词法交集、向量未运行跳过(§4.3)、`search_vector` 报错跳过、两路失败为 `retrieval_unavailable`(I5)、无 artifact 为 `TOP_K`(I8)、互斥参数 `ValueError`、诊断计数、越权在门禁前过滤(I2)。
- **契约**（`tests/unit/test_retrieval_metrics.py`）：新字段接受/成对拒绝/越界拒绝/非法 `score_space` 拒绝/**旧 artifact 行为不变**。
- **分派**（按 09-28 spec）：`public`/`private` 各自比对正确版本；半配置 `ValueError`；`mismatch_records` 不含正文。
- **组装**（`tests/integration/`）：`.env` 同款 artifact → `gate_policy is COSINE_ABSOLUTE`；不带新字段 → 仍 `CALIBRATED`；**配置了路径但未装配 → 启动期报错**（enablement spec 的启动自检）。

### 6.2 验收标准（可复现）

```bash
python -m pytest tests/ -q                      # 全绿（记录 passed/skipped）
python scripts/retrieval_cosine_gate_eval.py    # 拒答 2/2；Hit/MRR/nDCG 与 baseline 逐值相等；零容忍 0；P95 ≤1.2x
python scripts/cosine_gate_enable_e2e.py        # 经正式 artifact 文件装配，断言 ①–⑥
```

**完成判定**：spec 批准 → 全目标实现、非目标确实没做 → 每个任务独立验收 → 全量测试通过 → 端到端真实链路跑通有输出证据 → §5 九条不变量逐条断言 → 偏差与遗留显式记录 → 临时探针清理。

---

## 7. 风险与未决问题

| # | 风险 | 处置 |
|---|---|---|
| K1 | 恢复的实现与原实现**不可能逐行一致**（原代码已丢失） | 以**行为等价**为验收口径：§2.3 六项断言逐值复现；不追求 diff 相同 |
| K2 | `tests/` 84 个文件全部丢失，原 24 项新增测试无法比对 | 按 §6.1 用例清单重建；其余测试属独立恢复任务（见 plan T0） |
| K3 | D1（`_safe_diagnostics` 哈希 `gate_policy`）未修 → 监控读不到门禁口径 | 显式保留为遗留项；本次不扩大范围 |
| K4 | 0.50 阈值为冷启动初值（30 相关 + 62 不相关候选、18 条用例） | 阶段 3 语料扩容后重标；本次沿用并标注 |
| K5 | 恢复源码与丢失版本存在**其他未知漂移**（不止门禁这一处） | plan T0 增加"配置-代码对齐体检"：逐项核对 `.env` 中所有生效配置是否都能被当前源码接受 |

---

## 8. spec 自检

| 检查项 | 结果 |
|---|---|
| 占位符扫描 | 无 TBD/TODO；阈值 0.50、`score_space=l2_inv`、索引版本均取自已存在的 artifact 文件 |
| 一致性 | §4 契约与 §6.1 测试一一对应；§4.3 判定链与 §4.1 门禁语义一致；§4.4 显式消解了与今日 P1-6 改动的顺序冲突 |
| 范围 | 单一实施计划可完成；阶段 2/3 与非目标明确拆出 |
| 歧义 | ① "余弦门禁"命名 vs `vector_score` 实为 `1/(1+L2²)` → `score_space` 消歧；② `intersect` vs `emptiness` → 取 intersect（原 spec 结论）；③ 向量不可用 → 跳过门禁（原 spec 结论，本文照搬并记录代价）；④ 与 P1-6 的顺序 → §4.4 明确两门禁作用于不同阶段 |
