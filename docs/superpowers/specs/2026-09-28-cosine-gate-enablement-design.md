# 余弦相关性门禁启用（Stage 3 Enablement）设计

## 1. 文档信息

- 日期：2026-09-28
- 状态：**待用户批准**（未批准不写代码；**未单独批准不改 `.env`**）
- 适用项目：`medical-rag`
- 关联 spec：
  - `2026-09-24-cosine-relevance-gate-design.md`（门禁本体与阶段划分；本文即其**阶段 3**）
  - `2026-09-28-per-index-calibration-matching-design.md`（闭合了本阶段的前置阻塞 P0-1）
- 关联 plan：`2026-09-28-per-index-calibration-matching-implementation.md`（阶段 3 前置已验收）
- 关联 workflow：`FEATURE-WORKFLOW.md` 规则 3（设计未批准不写代码）、规则 4（偏差显式上报）

**前置状态（已满足）**：双索引 artifact 一致性缺陷（P0-1）已修复并验收
（754 passed / 3 skipped；真实 OpenSearch 双索引 E2E 断言 ①–⑥ 全 PASS）。
"启用"在技术上已成为可能。**但 `.env` 仍未配置 `CALIBRATION_ARTIFACT_PATH`——这是有意的。**

---

## 2. 背景

### 2.1 为什么"能启用"不等于"已启用"

`retrieval/service.py` 的门禁口径由 `calibration_artifact` 决定：

| `CALIBRATION_ARTIFACT_PATH` | `gate_policy` | 行为 |
|---|---|---|
| 未配置（**当前生产状态**） | `TOP_K` | 无硬门禁：硬性返回 top_k 截断，**对无关查询从不拒答** |
| 配置 legacy artifact（仅 `threshold`） | `CALIBRATED` | 作用于 RRF 归一化分数，**无相关性判别力**（已实测） |
| 配置 cosine artifact（+`cosine_threshold`/`score_space`） | `COSINE_ABSOLUTE` | 作用于单路向量分数，**唯一有判别力的绝对信号** |

即：**不配置 artifact 时，"检索相关性拒答"这个能力是不存在的**（2026-09-24 spec §2 的原始缺陷）。

### 2.2 启用会改变行为——因此需要独立评审

启用后：

1. **无关查询将被拒答**（返回 0 条 + `insufficient_evidence`）——这是**期望**；
2. **向量路径不可用时会跳过门禁**（`service.py:508,528-530`），
   此时恢复"永不拒答"——**这是刻意接受的降级项**（2026-09-24 spec 已定论），
   但**必须可观测**，否则运维会以为门禁一直在工作；
3. **门禁策略在监控侧当前不可见**（D1：`_safe_diagnostics` 把 `gate_policy` 哈希掉），
   无法从指标判断"当前实例到底有没有门禁"。**启用前必须修这个**，否则启用后
   无法验证、也无法告警。

### 2.3 当前门禁的可观测性缺口（D1，实测）

`observability/metrics.py:547-574` `_safe_diagnostics` 的键白名单：

| 诊断键 | 当前处理 | 问题 |
|---|---|---|
| `gate_policy` | 字符串 → 不在 `{reason, status, index_version, embedding_model}` → **`_hash(value)`** | 监控读不到"当前是什么门禁口径" |
| `gate_score_space` | 同上 → `_hash` | 读不到分数空间（阈值语义随它变） |
| `gate_threshold` | float → 直通 | 可读（但不知口径） |
| `gate_skipped_reason` | 字符串 → `_hash` | **读不到"门禁被跳过了"**——最危险的情形不可见 |
| `vector_candidates_gated_out` | int/None → 直通 | 可读 |
| `calibration_mismatch_records` | tuple-of-dict → `_hash` | 读不到 mismatch 明细（P0-1 修复引入） |

> `gate_policy` 是被哈希的**枚举 value**（如 `"cosine_absolute"`），
> 属于**有界枚举值**，与 `status` 同族——它本就该像 `status` 一样走白名单直通。

---

## 3. 目标与非目标

### 3.1 目标

1. **修复 D1 可观测性缺口**，使"当前门禁口径 / 是否被跳过 / skip 原因 / mismatch 明细"
   在监控侧**可读**（且**不引入**任意文本泄漏面）。
2. **产出正式 artifact JSON**（双索引版本 + `cosine_threshold` + `score_space`），
   落在仓库内的**受版本管理**路径，作为标定的**唯一事实来源**。
3. **启用前演练**：在**不修改 `.env`** 的前提下，用真实环境跑一次"经正式 artifact 文件装配"
   的端到端验收，证明启用后行为与 Task 6 验收一致。
4. **启动期自检**：当配置了 artifact 时，诚实回答"门禁口径是否真的是余弦绝对门禁"，
   口径不符即**启动失败**（而不是静默退回 TOP_K）。
5. **定义回滚路径**：一行 `.env` 修改即可回到无门禁状态，且回滚后行为与启用前逐字节一致。
6. **明确启用后的运维监控项**（尤其是"门禁被跳过"必须告警）。

### 3.2 非目标（本次明确不做）

| 非目标 | 原因 |
|---|---|
| **不重标定 `cosine_threshold`** | 2026-09-24 spec 已定论 0.50（`l2_inv` 空间）。本次只是**落盘与启用**，不改数值。 |
| **不做运行时 `space_type` 自省** | 已由 `score_space` 显式声明承担（2026-09-24 spec 阶段 2 结论）。 |
| **不改 `rrf_k` / `top_k` / `candidate_limit`** | 均已定论（`rrf_k=60`、`top_k=3`）。 |
| **不引入新配置项** | 复用既有 `CALIBRATION_ARTIFACT_PATH`。新增配置项会扩大部署面。 |
| **不做阶段 2 的版本命名规范** | D3 独立任务（需重建索引）。 |
| **不清理 `Settings.embedding_index_version` 死配置** | D2 独立任务。 |
| **不修 `deletion_residuals`/`review_required_leakage` 假绿** | D4 独立任务（需负样本语料）。 |
| **不在本 spec 内改 `.env`** | **`.env` 的修改需要用户在该 spec 批准之外，单独明确确认**（见 §9.1）。 |

---

## 4. 总体架构

### 4.1 变更位置

```text
         ┌────────────────────────────────────────────────────────────┐
启动期   │ api/app.py lifespan                                        │
         │  【新增】配置了 artifact 时做门禁口径自检：                  │
         │    gate_policy 必须是 COSINE_ABSOLUTE，否则启动失败          │
         │    位置：try/except **之外**（否则被非生产的吞异常分支绕过）  │
         └────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
         ┌────────────────────────────────────────────────────────────┐
配置     │ .env: CALIBRATION_ARTIFACT_PATH=config/calibration/*.json   │  <- 唯一"启用"动作
         │  （本次不在 spec 内执行；由用户单独批准后改）                 │
         └────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
         ┌────────────────────────────────────────────────────────────┐
监控     │ observability/metrics.py:_safe_diagnostics                 │
         │  【变更】有界枚举/内部标识键加入白名单（D1 修复）             │
         └────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
         ┌────────────────────────────────────────────────────────────┐
验收     │ scripts/cosine_gate_enable_e2e.py（新增）                   │
         │  经**正式 artifact 文件**装配（而非内存构造），断言 ①–⑥       │
         │  与 Task 6 一致；并验证"门禁跳过"场景可观测                  │
         └────────────────────────────────────────────────────────────┘
```

### 4.2 正式 artifact 文件

**路径**：`config/calibration/cosine-gate-2026-09-26.json`

**内容**（`CalibrationArtifact(**payload)` 的完整字段）：

```json
{
  "index_version": "artifact-2026-09-26",
  "embedding_model": "BAAI/bge-small-zh-v1.5",
  "rrf_k": 60,
  "threshold": 0.42,
  "eval_dataset_version": "cosine-gate-2026-09-26",
  "cosine_threshold": 0.50,
  "score_space": "l2_inv",
  "public_index_version": "public-v1",
  "private_index_version": "private-v1"
}
```

字段依据（全部来自已验收事实，非猜测）：

| 字段 | 取值 | 依据 |
|---|---|---|
| `embedding_model` | `BAAI/bge-small-zh-v1.5` | 本地 ONNX 模型身份；实测两索引记录与查询侧一致。**必须与 `resolve_embedding(settings).model_name` 完全相等**（`models/factory.py:54` 的 `EmbeddingComposition.model_name`），否则 `RetrievalService.__init__` 抛 `embedding model mismatch`。 |
| `rrf_k` | `60` | 必须与 `HybridRanker(rrf_k=...)` 相等，否则构造期抛错 |
| `threshold` | `0.42` | 余弦门禁生效时**不参与判定**，仅作 artifact 元数据（与 `scripts/retrieval_cosine_gate_eval.py` 的 `ARTIFACT_RRF_THRESHOLD` 一致） |
| `cosine_threshold` | `0.50` | 2026-09-24 spec §6.4 标定值（`l2_inv` 空间） |
| `score_space` | `"l2_inv"` | 索引 `knn_vector` **未声明 `space_type`** → OpenSearch 默认 `l2` → 分数是 `1/(1+L2²)` |
| `public_index_version` | `public-v1` | `KnowledgeIngestService` 默认；实测公共 5 条全为该值 |
| `private_index_version` | `private-v1` | `IndexJob.index_version` 默认；实测私有 7 条全为该值 |
| `index_version` | `artifact-2026-09-26` | 两字段成对提供后它退化为**标识元数据**（P0-1 修复的语义） |

> **不变量**：`rrf_k` 与 `HybridRanker` 必须相等；`embedding_model` 必须与解析出的模型名
> 逐字符相等；`cosine_threshold` 与 `score_space` 必须成对；两索引版本必须成对。
> 四者任一不符 → 构造期/装配期抛错（既有行为，本次不改）。

### 4.3 启动期门禁口径自检

**位置（重要，与直觉相反）**：**`api/app.py` lifespan 中 `try/except` 块的 `yield` 之前、
且位于 `except` 之外**——即接在 `except Exception: ... raise` 之后、`yield` 之前：

```python
        except Exception:
            logger.exception("failed to compose chat orchestrator at startup")
            if resolved_settings.is_production:
                raise

        # --- 门禁口径自检：必须在 try/except 之外 ---
        # 放在 try 内会被非生产环境的 log-and-continue 分支吞掉，
        # 于是"配了 artifact 却退回 TOP_K"的实例照常启动——正是本自检要拦的情况。
        _assert_configured_gate_is_active(app, resolved_settings)

        yield
```

新增模块级函数（同一文件）：

```python
def _assert_configured_gate_is_active(app: FastAPI, settings: Settings) -> None:
    """配置了标定产物时，必须真的装配出余弦绝对门禁，否则拒绝启动。

    未配置路径时不做任何检查（TOP_K 正是预期状态）——因此本函数对
    "从未启用门禁"的部署行为逐字节不变。
    """
    if not settings.calibration_artifact_path:
        return
    from medical_rag.retrieval.contracts import GatePolicy

    orchestrator = getattr(app.state, "chat_orchestrator", None)
    retriever = getattr(orchestrator, "retriever", None)
    if retriever is None:
        # 配置了 artifact 却没走到装配（例如非生产环境里装配失败被吞掉）
        # => 不能假装启动成功。
        raise RuntimeError(
            "已配置 CALIBRATION_ARTIFACT_PATH 但检索编排器未装配："
            f"{settings.calibration_artifact_path}"
        )
    artifact = getattr(retriever, "calibration_artifact", None)
    if artifact is None:
        raise RuntimeError(
            "已配置 CALIBRATION_ARTIFACT_PATH 但检索服务未持有标定产物"
            f"：{settings.calibration_artifact_path}"
        )
    if getattr(retriever, "gate_policy", None) is not GatePolicy.COSINE_ABSOLUTE:
        raise RuntimeError(
            "calibration artifact 未选出 COSINE_ABSOLUTE 门禁；"
            f"实际={getattr(retriever, 'gate_policy', None)}"
            "（配 artifact 的检索服务不得静默退回 TOP_K 截断）"
        )
    logger.info(
        "cosine gate enabled: threshold=%s score_space=%s public=%s private=%s",
        artifact.cosine_threshold, artifact.score_space,
        artifact.public_index_version, artifact.private_index_version,
    )
```

**为什么条件写成"路径非空"而不是"artifact 非 None"**：`artifact` 只有在
`RetrievalService` 构造成功后才存在。若以 `artifact is not None` 为条件，
那么"配了路径 + 装配失败 + 非生产环境"这一组合会**跳过自检**——恰好漏掉最该拦的情形。

**为什么放在 lifespan**：本项目已确立"启动必须诚实回答能否服务"的原则
（`api/app.py:2-7`）；配置了 artifact 却静默退回 TOP_K，等于**部署了一个看起来有门禁、
实际从不拒答的实例**——与 `/health/ready` 探测降级索引的设计意图一致。

> **依赖的既有事实（已核实）**：`ChatOrchestrator` **已暴露** `self.retriever`
> （`chat/orchestrator.py:111`），`RetrievalService` 已暴露
> `self.calibration_artifact` / `self.gate_policy`（`retrieval/service.py:112,138-165`）。
> 因此无需新增任何属性即可实现本自检。

---

## 5. 核心数据契约（D1 白名单变更）

### 5.1 `_safe_diagnostics` 白名单扩展

变更原则：**只放行"有界枚举值"与"内部标识符"，绝不放行任意自由文本**。

| 键 | 类型 | 处理（变更后） | 理由 |
|---|---|---|---|
| `gate_policy` | str | 走新 `_SAFE_GATE_POLICIES` 白名单；不在集合内 → `_hash` | 有界枚举（`GatePolicy` 的 5 个 value） |
| `gate_score_space` | str\|None | 走新 `_SAFE_SCORE_SPACES`（`{"l2_inv","cosinesimil"}`）；不在集合 → `_hash` | 有界枚举 |
| `gate_skipped_reason` | str\|None | 走新 `_SAFE_GATE_SKIP_REASONS`（`{"vector_path_unavailable"}`） | 有界枚举；**最需告警的信号** |
| `calibration_mismatch_count` | int | 已直通（int） | 无需改 |
| `calibration_mismatch_records` | tuple[dict] | **保持 `_hash`**（不在本次放行） | tuple-of-dict 无有界枚举形态；放行需新的逐字段脱敏逻辑，独立任务（见 §8 D12） |

新增集合（`observability/metrics.py`，与 `_SAFE_REASONS` 同级）：

```python
_SAFE_GATE_POLICIES = frozenset({"top_k", "calibrated", "explicit_threshold", "rank_gate", "cosine_absolute"})
_SAFE_SCORE_SPACES = frozenset({"l2_inv", "cosinesimil"})
_SAFE_GATE_SKIP_REASONS = frozenset({"vector_path_unavailable"})
```

> **取值来源必须锁定**：三个集合的成员必须与 `retrieval/contracts.py:GatePolicy`
> 以及 `service.py:530` 的实际取值逐字比对（测试守护，见 §7.1 T1）。
> 用**字面量集合**而非 `{p.value for p in GatePolicy}` 是为了让"新增枚举值但忘了加白名单"
> 变成**测试失败**，而不是**静默扩展指标面**。

### 5.2 放行后的诊断载荷

| 键 | 期望可读值 |
|---|---|
| `gate_policy` | `"cosine_absolute"`（启用后） |
| `gate_threshold` | `0.5` |
| `gate_score_space` | `"l2_inv"` |
| `gate_skipped_reason` | `null`（正常）或 `"vector_path_unavailable"`（降级，需告警） |
| `vector_candidates_gated_out` | int |
| `lexical_gated_out` | int |

---

## 6. 设计细节

### 6.1 启用动作 = 一行配置（且可回滚）

```bash
# .env（启用）
CALIBRATION_ARTIFACT_PATH=config/calibration/cosine-gate-2026-09-26.json

# .env（回滚）
# CALIBRATION_ARTIFACT_PATH=
```

**回滚不变量**：删除/注释该行后，`load_calibration_artifact` 返回 `None`
→ `gate_policy = TOP_K` → 行为与启用前**逐字节一致**（既有 `test_scheme_b_rollout.py` 守护）。
**该不变量必须有测试**（§7.1 T4：同一 artifact 文件存在与不存在两态对比）。

### 6.2 启用前的"干跑"（不碰 `.env`）

新增 `scripts/cosine_gate_enable_e2e.py`：

1. **直接读正式 artifact 文件**（`config/calibration/cosine-gate-2026-09-26.json`），
   经 `CalibrationArtifact(**payload)` 装配 `RetrievalService`（**不经过 `.env`**）；
2. 用真实 OpenSearch 双索引 + 本地 ONNX 跑断言 ①–⑥（与 Task 6 完全一致的口径）；
3. **额外断言 ⑦**：构造"向量路径不可用"（如让 embedding 抛错 / 传 `embedding=None` 且
   禁用 fallback），断言 `gate_skipped_reason == "vector_path_unavailable"`，
   且 `_safe_diagnostics` 后该值**可读**（D1 修复的端到端验证）；
4. 输出 `eval_out/cosine_gate_enable_e2e.json`。

**为什么"干跑"必须存在**：它把"启用后会怎样"变成**可复现的证据**，
而不依赖"改完 `.env` 再祈祷"。它也是申请用户批准 `.env` 修改的**依据**。

### 6.3 启用后的验证（用户批准改 `.env` 后）

```bash
# 1. 启动自检（应看到 "cosine gate enabled: ..." 日志，且不抛错）
python -m uvicorn medical_rag.api.app:create_app --factory --port 8000

# 2. 就绪探针
curl -s localhost:8000/health/ready

# 3. 干跑脚本（此时它读的是同一份文件；用于确认装配一致）
python scripts/cosine_gate_enable_e2e.py

# 4. 端到端行为（真实服务）
#    无关查询 -> 0 条 + insufficient_evidence
#    可答查询 -> 正常条数，且与 baseline 排序指标一致
```

### 6.4 "门禁被跳过"必须告警（不是可选）

门禁跳过（`vector_path_unavailable`）时系统恢复"永不拒答"——
即**静默失去拒答能力**。这是既有已接受的降级项，但启用后必须能被运营看见。

| 信号 | 来源 | 告警条件 |
|---|---|---|
| `gate_skipped_reason == "vector_path_unavailable"` 的**比例** | 诊断指标（D1 修复后变为可读） | 任意非零即告警（这是异常状态，不是常态） |
| `gate_policy != "cosine_absolute"` | 诊断指标 | 生产环境非零即告警（说明装配退化） |
| `calibration_artifact_mismatch` 为真 | 既有诊断 | 非零即告警（fail-closed，服务不可用） |

> **本次只定义告警条件与指标可读性**（D1 修复是前提）。
> 告警系统的接线（Prometheus 规则 / 报警通道）属运维配置，不在本仓库代码范围内；
> 交付说明需列出这三条作为**部署检查项**。

#### 6.4.1 边界修正（2026-10-05，偏差上报）

上一段的措辞把两件不同的事混为一谈，启用后实测证明其**不可实施**：

| 事项 | 归属 | 状态 |
|---|---|---|
| 定义告警条件（三行判据） | 本 spec | ✅ 已完成、判据可用 |
| **在仓库内提供可采集信号** | **本仓库** | ❌ 启用时**未完成** |
| 接线到监控系统（规则/渠道） | 运维配置（仓库外） | 待运维 |

实测（`docs/superpowers/notes/2026-10-05-d17-alert-wiring-findings.md`）：

1. 仓库内**无任何指标导出通道**——无 `/metrics`，无 prometheus/statsd/otel，
   `MetricsRecorder.record_retrieval()` **无生产调用方**；
2. 告警条件 1 的判据 `gate_skipped_reason` **不在 HTTP 出口白名单**
   （`chat/orchestrator.py:_ANSWER_DIAGNOSTIC_KEYS`）中——D1 修复只覆盖了
   `observability/metrics._safe_diagnostics`，而那是**另一道**、且**无生产调用方**的白名单。
   于是门禁跳过时，响应只能看到 `vector_error: true`，而它与"门禁被跳过"**不等价**
   （embedding 未配置时门禁同样跳过但 `vector_error=false`）→ **告警必然漏报**。

**结论**：运维要接线，仓库必须先有可接的出口。该缺口由
`docs/superpowers/specs/2026-10-05-d17-alert-signal-availability-design.md` 修复
（补齐 HTTP 白名单 4 键 + 两道白名单一致性契约测试 + HTTP 层端到端断言）。
本 spec 的三条判据定义**不变**。

---

## 7. 错误与降级

### 7.1 降级路径总表

| 场景 | 行为 | `gate_policy` / 诊断 | 患者看到 |
|---|---|---|---|
| artifact 未配置 | 无门禁（现状） | `top_k` | 正常回答（从不拒答） |
| artifact 配置正确 | 余弦绝对门禁 | `cosine_absolute`；`gate_skipped_reason=null` | 无关→拒答；可答→正常 |
| artifact 文件缺失/非法 JSON/字段不符 | **启动失败**（既有行为，`load_calibration_artifact` 直接抛错） | — | 部署不启动（正确的失败模式） |
| artifact 配置但未选出余弦口径（如缺 `cosine_threshold`） | **启动失败**（本次新增自检） | — | 部署不启动 |
| 向量路径不可用 | **跳过门禁**（既有已接受降级）→ 永不拒答 | `gate_skipped_reason="vector_path_unavailable"`（**可读+告警**） | 正常回答（可能含不相关） |
| 记录版本与其索引声明不符 | `items=[]`（fail-closed，P0-1 既有） | `calibration_artifact_mismatch=true` | 「服务暂不可用」 |
| 回滚（删除 `.env` 该行） | 回到无门禁 | `top_k` | 与启用前逐字节一致 |

### 7.2 为什么不做"半启用"（部分实例启用、部分不启用）

`gate_policy` 是**进程级**装配结果。若灰度期内部分实例启用了门禁、部分没有，
则同一查询在不同实例上得到不同结果（拒答 vs 不拒答）——这对**患者可见行为**是不一致的。
本项目当前是**单机试点**（SQLite + 本地 OpenSearch），不存在多实例灰度场景。
若未来需要灰度，必须用**同构**方式（如按租户分流），而不是"部分实例不配 artifact"，
那会让"从不拒答"的实例继续服务且无人察觉。**本次不做灰度，一次全量启用或回滚。**

---

## 8. 安全与合规不变量（逐条可断言）

| # | 不变量 | 断言方式 |
|---|---|---|
| G1 | 回滚后行为与启用前**逐字节一致** | 同一 artifact 文件"存在/不存在"两态对比，无门禁态的全部指标相等（T4） |
| G2 | 配置 artifact 却退回 TOP_K → **启动失败** | 构造缺 `cosine_threshold` 的 artifact 文件 → lifespan 抛错（T3） |
| G3 | 门禁口径在监控侧**可读** | `_safe_diagnostics({"gate_policy": "cosine_absolute"})["gate_policy"] == "cosine_absolute"`（T1） |
| G4 | 门禁跳过**可读** | `_safe_diagnostics({"gate_skipped_reason": "vector_path_unavailable"})` 值不被哈希（T1） |
| G5 | 白名单**不扩展**自由文本面 | 未知 `gate_policy` 值（如 `"hacked"`）→ 仍被 `_hash`（T1 负例） |
| G6 | 白名单集合与枚举**逐字对齐** | 断言三个集合 == `GatePolicy`/`score_space`/skip reason 的实际取值集合（T1） |
| G7 | 正式 artifact JSON 与真实索引**装配成功** | 干跑脚本构造期不抛错，且 `rrf_k`/`embedding_model` 匹配（T2） |
| G8 | 启用后 6 项行为断言与 Task 6 **一致** | 干跑脚本断言 ①–⑥ 全 PASS（T2） |
| G9 | 拒绝无关查询 | 混合作用域拒答用例 2/2，`reason=insufficient_evidence`（T2） |
| G10 | 排序无损失 | 可答用例 Hit/Recall/MRR/nDCG 与 baseline 逐值相等（T2） |
| G11 | 零容忍三项恒为 0 | 真实链路断言为 0（T2） |
| G12 | P95 劣化 ≤ 1.2× | 预热 2 轮 + 计时 25 轮（T2） |
| G13 | 诊断不含正文 | 放行后的诊断键全部为枚举/数值/标识符，无 text（T1 负例：注入含正文的自定义键仍被哈希） |
| G14 | 自检**不可被非生产的吞异常分支绕过** | 非生产环境 + 配了路径 + 装配缺失 → 仍抛 `RuntimeError`（§10.2 第 5 行；证明自检在 `try/except` 之外） |

---

## 9. 已发现但本次不修的相邻缺陷（显式上报）

| # | 缺陷 | 影响 | 处理 |
|---|---|---|---|
| D12 | `calibration_mismatch_records`（tuple-of-dict）仍被哈希 | 监控读不到 mismatch 明细 | 需逐字段脱敏逻辑，独立任务 |
| D13 | `Settings.embedding_index_version` 死配置 | 运维改它不影响入库，配 artifact 时易误以为可配 | 既有 D2，独立任务 |
| D14 | 两索引版本命名无规范 | 靠默认值巧合 | 既有 D3，阶段 2 |
| D15 | `deletion_residuals`/`review_required_leakage` 假绿 | 零容忍项恒 0 但无语料证明 | 既有 D4，独立任务 |
| D16 | 指标有 `stage_metrics` 与 `observability/metrics` 双份体系 | 门禁指标落哪一份不统一 | 既有 P2，独立任务 |
| D17 | 告警通道未接线（本仓库外） | 三条告警条件无人接收 | **仓库侧信号出口已于 2026-10-05 修复**（§6.4.1，HTTP 白名单补齐 4 键 + 一致性契约测试）；监控规则/报警渠道仍属运维配置，待接 |

---

## 10. 测试点

### 10.1 诊断白名单（`tests/unit/test_observability_metrics.py`，或既有 metrics 测试文件）

| 测试 | 断言 |
|---|---|
| `gate_policy` 可读 | `_safe_diagnostics({"gate_policy":"cosine_absolute"})["gate_policy"] == "cosine_absolute"`（G3） |
| `gate_score_space` 可读 | `"l2_inv"` 直通 |
| `gate_skipped_reason` 可读 | `"vector_path_unavailable"` 直通（G4） |
| 未知 `gate_policy` 仍被哈希（G5） | `"hacked"` → 以 `sha256:` 开头 |
| 三个白名单集合与枚举对齐（G6） | 逐字相等断言 |
| 含正文的自定义键仍被哈希（G13） | `{"note": "患者姓名张三"}` → 值为 `sha256:` |
| `calibration_mismatch_records` 仍被哈希（D12 现状锁定） | 值以 `sha256:` 开头 |

### 10.2 启动自检（`tests/integration/test_app_lifespan.py` 或既有启动测试）

| 测试 | 断言 |
|---|---|
| 无 `CALIBRATION_ARTIFACT_PATH` → 启动成功，`gate_policy == TOP_K` | 不抛错（**行为与改动前逐字节一致**） |
| 正确 artifact → 启动成功，`gate_policy == COSINE_ABSOLUTE` | 不抛错；`caplog` 含 `"cosine gate enabled"` |
| 缺 `cosine_threshold` 的 artifact → 启动失败（G2） | `pytest.raises(RuntimeError, match="COSINE_ABSOLUTE")` |
| artifact 文件不存在 → 启动失败 | `FileNotFoundError` |
| **非生产环境 + 配了路径 + 装配被吞掉 → 仍失败**（自检位置回归线） | 伪造 `app.state.chat_orchestrator` 不含 `retriever` → `pytest.raises(RuntimeError, match="未装配")`，**证明自检不在 `try/except` 内** |
| 非生产环境 + 未配路径 + 装配失败 → 只管不回退（既有行为） | 不抛错（开发期便利保留） |

### 10.3 正式 artifact 契约（`tests/contract/test_calibration_artifact_file.py`，新增）

| 测试 | 断言 |
|---|---|
| 仓库内正式 JSON 可被 `CalibrationArtifact(**payload)` 构造 | 不抛错（G7） |
| 其 `rrf_k == 60`、`score_space == "l2_inv"`、`cosine_threshold == 0.5` | 逐字段 |
| 其两索引版本成对且非空 | `public-v1` / `private-v1` |
| 其 `embedding_model` 与本地模型身份一致 | 与 `resolve_embedding(local settings).model_name` 相等（`models/factory.py:54`） |
| 文件路径被 `.gitignore` **不**排除 | 确保纳入版本管理 |

### 10.4 真实链路（`scripts/cosine_gate_enable_e2e.py`，T2）

断言 ①–⑦（①–⑥ 同 Task 6；⑦ 为门禁跳过可观测）。

### 10.5 回滚（T4）

| 测试 | 断言 |
|---|---|
| artifact 文件存在 vs 不存在两态 | 无门禁态指标逐值相等；有门禁态才出现拒答与 `gate_policy=cosine_absolute` |

---

## 11. 实施阶段（每阶段可独立验收）

### 阶段 A（本次，可立即执行，不需批准 `.env`）：可观测性 + 干跑 + 自检

1. **T1** `_safe_diagnostics` 白名单扩展（D1 修复）
2. **T2** 新增正式 artifact JSON `config/calibration/cosine-gate-2026-09-26.json`
3. **T3** 启动期门禁口径自检（`api/app.py` lifespan）
4. **T4** 新增 `scripts/cosine_gate_enable_e2e.py`（干跑 + ⑦ 断言）+ 契约测试 + 回滚测试

验收：§10 测试全绿；干跑断言 ①–⑦ 全 PASS；**`.env` 未被修改**。

### 阶段 B（**需用户单独批准**）：启用

5. **T5** 在 `.env` 写入 `CALIBRATION_ARTIFACT_PATH=config/calibration/cosine-gate-2026-09-26.json`
6. **T6** 启用后验证（§6.3）：启动自检日志 + 就绪探针 + 端到端行为
7. **T7** 部署检查项交付（三条告警条件、回滚步骤）

> ⚠️ **阶段 B 不得在未获单独批准时执行**。本 spec 的批准 ≠ 批准修改 `.env`。
> 用户需明确说"批准启用"或等价表述。

---

## 12. 验收标准（可观测、可复现）

```bash
# 1. 全量测试（记录通过/跳过数；基线：754 passed / 3 skipped）
python -m pytest tests/ -q

# 2. 干跑（不改 .env）
python scripts/cosine_gate_enable_e2e.py

# 预期输出：
#   装配自检: gate_policy=cosine_absolute cosine_threshold=0.5 score_space=l2_inv
#   拒答用例（含混合作用域） 2/2，reason=insufficient_evidence
#   混合作用域零返回用例数   0
#   calibration_artifact_mismatch 用例数   0
#   可答用例 Hit/Recall/MRR/nDCG 与 baseline 逐值相等
#   零容忍违规    0
#   P95 劣化      <= 1.2x
#   断言7 门禁跳过可观测：gate_skipped_reason 在诊断中可读（非哈希）
```

**完成判定**（对齐 `FEATURE-WORKFLOW` §5）：

- [ ] spec 已批准，全部目标实现，非目标确实没做
- [ ] 每个任务独立验收
- [ ] 全量测试通过（记录通过/跳过数）
- [ ] 真实链路端到端跑通，有输出证据（`eval_out/cosine_gate_enable_e2e.json`）
- [ ] §8 G1–G14 逐条断言通过（**尤其 G1 回滚一致、G3/G4 可观测、G14 自检不可绕过**）
- [ ] §7.1 降级路径验证过（含门禁跳过、fail-closed、回滚）
- [ ] 偏差与遗留项显式记录（§9 D12–D17）
- [ ] **`.env` 未被修改（阶段 B 前）**
- [ ] 临时探针文件已清理

---

## 13. spec 自检

| 检查项 | 结果 |
|---|---|
| **占位符扫描** | 无 TBD / TODO。artifact 九个字段全部给值并注明依据；白名单三个集合给出完整成员 |
| **一致性** | §4.2 字段表与 §10.3 契约测试一一对应；§4.3 自检（含"必须放在 `try/except` 之外"与"条件写成路径非空"）与 §7.1"配置但未选出余弦→启动失败"、§8 G14、§10.2 第 5 行一致；§5.1 白名单与 §10.1 测试对应；§6.4 告警条件与 §9 D17（通道不在仓库）边界清晰 |
| **范围** | 阶段 A（可观测性 + 干跑 + 自检）与阶段 B（改 `.env`）**物理分离**，B 需单独批准。阶段 A 是一个实施计划可完成的粒度 |
| **歧义** | ① `calibration_mismatch_records` 是否也放行 → §5.1 选定**不放行**（需新脱敏逻辑）并列为 D12。② 自检放 lifespan 还是 composition → §4.3 给定位置与备选。③ 是否灰度 → §7.2 明确**不灰度**并给理由。④ 白名单用字面量还是动态生成 → §5.1 选定**字面量**并说明"让遗漏变成测试失败" |
