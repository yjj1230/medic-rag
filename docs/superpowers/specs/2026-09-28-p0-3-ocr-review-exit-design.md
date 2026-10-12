# P0-3：扫描件复核黑洞与 OCR 无置信度处置（OCR Substitutive Quality Signals & Review Exit）设计

## 1. 文档信息

- 日期：2026-09-28
- 状态：**待用户批准**（未批准不写代码）
- 适用项目：`medical-rag`
- 关联评审：`docs/RAG项目专业评审报告-2026-09-28.md`（P0-3 条目）
- 关联 workflow：`docs/superpowers/FEATURE-WORKFLOW.md` 规则 1（需求可验证）、规则 2（隐性不变量必须写进文档）、规则 4（偏差显式上报）
- 关联 spec：`2026-09-24-cosine-relevance-gate-design.md`（同为"门禁/资格"族，判决语义必须彼此一致）

**用户已批准的三个方向决策（本 spec 的前提）：**

| # | 决策点 | 用户选择 |
|---|---|---|
| D-A | 扫描件（OCR 无置信度）如何进入可检索语料 | **A. 替代信号 + 复核出口** |
| D-B | 复核通过接口的身份模型 | **新增 `role` 声明**（JWT claims + `AccessContext`） |
| D-C | 公共路径 fail-open 处理范围 | **硬编码 fail-closed + 公共入库门禁**（两者都做） |

---

## 2. 背景

### 2.1 触发场景

临床上绝大多数检查报告是**扫描件或照片**（无文本层 PDF / 图片）。上传后链路为：

```text
上传 -> PDF 无原生文本 -> 逐页 OCR（远程 PaddleOCR-VL）-> 得到文本行
     -> EvidenceUnit.confidence = None
     -> QualityGate 判 NEEDS_REVIEW + retrieval_eligible=False
     -> 文档接口仍显示「上传成功」，但检索永远返回空
```

2026-09-28 只读探针（`.workbuddy/_p03_probe.py`，已清理）在真实 `QualityGate` 上实测：

```text
构建块数: 3    block='p1-ocr-0' page=1 anchor='p1' confidence=None
  => 全部 confidence is None: True
  => status=NEEDS_REVIEW  retrieval_eligible=False
  => reasons=('confidence_unavailable',)
  => confidence_unavailable_ratio=1.0
  => ocr_confidence=0.0  (占位 0.0，非真实测量)
  => anchor_coverage=1.0  text_coverage=1.0     <-- 除置信度外，其他指标都合格
```

**关键观察**：除了"置信度不可得"这一项，`anchor_coverage=1.0`、`text_coverage=1.0`，
即内容**看起来是好的**——只是没有"识别得准不准"这个分数。

### 2.2 根因（三条，互相独立）

**根因 1：远程 OCR 服务不返回任何置信度。**

`parsing/remote_ocr.py` 的 docstring 记录了实测 schema：

```json
{"result": {"result": "<newline-delimited transcript>"}}
```

`post_ocr` 解析 `data["result"]["result"]` → `str` → 按 `\n` 切分，返回 `list[str]`。
**响应体里即使有 score/boxes/坐标，也不会被读取**（`_RemoteOcrHttpClient.post_ocr` 的返回类型就是 `list[str]`）。

因此"把置信度透传上来"**在本服务上不可实现**——这是环境事实，不是代码偷懒。
`per_page_ocr._ocr_blocks_for_page` 把 `confidence=None` 是**诚实**的（不伪造 0.95），
这一点本身是对的。

> **配置口径核实（2026-09-29 补）**：本机 `.env` **已配置**远程 OCR
> （`OCR_SERVICE_URL` 非空，`build_remote_ocr` 在 `remote_ocr.py:345-347` 因此返回
> 真实的 parser 与 backend factory，经 `adapters.build_parser_adapters:199-204` 接入
> `ParsingService`），故"扫描件走远程 OCR"确实发生。
> 若某次探针报"未配置"，几乎必然是它用了项目测试约定
> `Settings(_env_file=None, ...)`（不读开发者 `.env`）——该约定用于**测试自洽**，
> 不是用于读运行配置。实测对照：`Settings()` → `len=27`；`Settings(_env_file=None)` → `len=0`。
> （服务端返回的 schema 不含 score，因此无论是否配置远程 OCR，
> "OCR 无置信度"这一前提都成立。）

**根因 2：门禁把"不可判定"直接当"不合格"。**

`quality/gate.py:325-330`：

```python
# 不可判定 ≠ 低置信度：来源没给置信度时报 confidence_unavailable，
# 并用占位 0.0 去报 low_ocr_confidence 会把"没测出来"说成"测出来很低"。
if metrics["confidence_unavailable_ratio"] > 0.0:
    reasons.append(REASON_CONFIDENCE_UNAVAILABLE)
elif metrics["ocr_confidence"] < policy.min_ocr_confidence:
    reasons.append(REASON_LOW_OCR_CONFIDENCE)
```

注释**正确识别了**"不可判定 ≠ 低置信度"，但结论方向反了：它是「按不可判定**报**」，
而不是「按不可判定**另寻判据**」。`reasons` 非空 → `gate.py:349-352` 落 `NEEDS_REVIEW`。

**根因 3：`NEEDS_REVIEW` 是只进不出的黑洞。**

- `DocumentStatus.READY` 只在 `parsing/service.py:857-861` 的 PASS 分支写入；
- `api/routes/documents.py` 只有 upload / delete / list / download —— **没有复核通过路由**；
- `QualityDecision.review()`（`quality/contracts.py:53-66`）**从未被任何 API 或服务调用**；
- 全库唯一的状态迁移是 PASS→REVIEW、FAIL→REVIEW，**没有任何 REVIEW→READY 路径**。

> 三者叠加的后果：**扫描件永久不可检索，且没有任何人工干预手段能救回它。**

### 2.3 附带缺陷（同一族，本次一并处理：D-C 决策）

**(a) 公共路径 fail-open（已实测为不可达死代码，但仍是隐患）。**

`indexing/orchestrator.py:245-274` `_public_record`：
`eligible` 判定（`:246-252`）**接受 `NEEDS_REVIEW`**，且 `:271` **硬编码 `retrieval_eligible=True`**、
**不读** `evidence.retrieval_eligible`。探针确认：

```text
_classify_visibility: knowledge_chunk+无patient -> public;
                      report_text/report_table+有patient -> private; 其余 -> ValueError
=> 'public' 分支只在 kind=='knowledge_chunk' 时成立，而公共知识走
   KnowledgeIngestService（不经 orchestrator）=> 该硬编码**实际不可达**（死代码）。
=> 但它仍是 fail-open 隐患：一旦让 report_* 无 patient_id，就会绕过
   evidence.retrieval_eligible 被标为可检索。
```

**(b) 公共知识入库完全不过 QualityGate。**

`knowledge/ingest_service.py:270` 与 `:357` 硬编码 `retrieval_eligible=True`，
不引用 `QualityGate`（探针确认 `引用 QualityGate: False`）；内容评审只在**来源级**
（`source.reviewer`，`ingest_service.py:327`）。

**(c) 独立发现：既有 `abnormal_character_ratio` 对"方块垃圾"完全无效。**

已知私有库两份 PDF 解析出 102 个 `U+25A0`（无中文字形）。实测 `U+25A0` 的
`unicodedata.category` 是 **`So`**，而 `gate.py:41` 的
`_SUSPICIOUS_CATEGORIES = frozenset({"Cc", "Cf"})` **不含 `So`**。
→ 现有异常字符指标对这类垃圾**恒为 0**。这意味着"看起来有内容"的垃圾文本能通过
`abnormal_character_ratio` 检查。（本次替代信号的设计必须覆盖它。）

### 2.4 为什么"替代信号"是正确的方向，而不是放宽标准

原判据 `confidence_unavailable → 复核` 的问题不是"太严"，而是**用一个不可获取的
观测量做判决**。当观测量在本环境恒不可得时，该判据退化为"扫描件一律不合格"——
它不区分"识别得很好的扫描件"与"识别成一团方块的扫描件"。

替代信号（S1/S2/S3，见 §4.3）是**可测量、确定性、可测试**的：
它们在标定集上把好文本与垃圾文本**分开**（§4.5 实测），因此比"恒不可得"更有判别力。
这不是放宽——垃圾文本在新判据下**同样进复核**，而且是被**真正识别出来**的（旧判据根本看不见它）。

---

## 3. 目标与非目标

### 3.1 目标

1. **扫描件可在无人工干预下进入可检索语料**，当且仅当替代质量信号表明文本质量合格。
2. **垃圾/低质 OCR 文本确实被拦下**（含 `U+25A0` 方块串这类旧指标看不见的形态）。
3. **补上人工复核出口**：`NEEDS_REVIEW → READY` 的状态迁移路径，带角色鉴权与审计。
4. **闭合公共路径 fail-open**：`_public_record` 改读 `evidence.retrieval_eligible`（fail-closed）。
5. **公共知识入库接入质量门禁**，使公共分块与私有证据使用同一套质量判据。
6. **所有新判据可断言、可复现**：替代信号的指标与阈值集中在 `QualityPolicy`，纯函数、可单测。

### 3.2 非目标（本次明确不做）

| 非目标 | 原因 |
|---|---|
| **不追求"OCR 置信度"本身** | 远程服务不提供；推测一个数字等于伪造测量值，违反 `per_page_ocr.py:61` 的既有诚实原则。若未来服务返回 score，另立 spec 接入（§9 D6）。 |
| **不做 OCR 引擎替换/本地化** | 依赖与服务选型不在本次范围。 |
| **不重标定余弦门禁阈值** | 属阶段 3（另一 spec）。本次不碰 `cosine_threshold`/`score_space`。 |
| **不做复核的任期/多人会签/工作流引擎** | 本次只做"单角色单次放行"的最小可用出口；会签与队列属于产品化范畴。 |
| **不改 `QualityDecision.__post_init__` 的 PASS↔eligible 不变量** | 该不变量是承重的（"不可检索内容绝不作为证据浮现"）。复核通过走**重新评估**（§5.4/§6.1），而不是给 NEEDS_REVIEW 打 eligible=True。 |
| **不做全量历史扫描件回填迁移** | 本次只让**新上传**走对；存量修复需一次性运维脚本，独立验收（§9 D7）。 |
| **不给"公共入库门禁"做语料清洗** | 现公共库 5 条全部来自自述"未审核"的样例文件；门禁会让它们被判复核——这是正确的，但清库/换源是独立任务。 |

---

## 4. 总体架构

### 4.1 变更位置

```text
                       ┌──────────────────────────────────────────────┐
解析（OCR）            │ per_page_ocr._ocr_blocks_for_page            │
  confidence=None ────►│  （不变：仍然诚实置 None）                     │
                       └──────────────────────────────────────────────┘
                                        │
                                        ▼
                       ┌──────────────────────────────────────────────┐
QualityGate            │ 【新增】OCR 替代信号 S1/S2/S3                 │
  (quality/gate.py)    │ 【变更】confidence_unavailable 由"否决"改为     │
                       │        "触发替代判据"：                       │
                       │          替代信号合格 -> 不加入 reasons        │
                       │          替代信号不合格 -> 加入 reasons        │
                       └──────────────────────────────────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
        PASS → READY             NEEDS_REVIEW                 FAIL
        eligible=True            eligible=False             eligible=False
                                        │
                                        ▼
                       ┌──────────────────────────────────────────────┐
复核出口（新增）        │ POST /v1/documents/{id}/review                │
  ReviewService        │  role ∈ {reviewer, admin} 必备                 │
                       │  NEEDS_REVIEW -> 重新评估 -> READY             │
                       │  重新触发 IndexJob（写 eligible=True）          │
                       │  写 AuditEvent(REVIEW_APPROVED)                │
                       └──────────────────────────────────────────────┘
```

### 4.2 门禁判决矩阵（变更后）

| `confidence_unavailable_ratio` | 替代信号 | 其他 reasons | 判决 |
|---|---|---|---|
| 0.0（有真实置信度） | 不适用 | 无 | PASS（**行为不变**） |
| 0.0 | 不适用 | 有 | NEEDS_REVIEW（**行为不变**） |
| >0.0 | **合格** | 无 | **PASS**（本次新增：扫描件放行） |
| >0.0 | **合格** | 有 | NEEDS_REVIEW（其他原因仍然算数） |
| >0.0 | **不合格** | 任意 | NEEDS_REVIEW + `ocr_substitute_quality_insufficient` |

> **不变量**：替代信号只能**减少** reasons，绝不新增"跳过其他判据"的分支。
> 原 `confidence_unavailable` reason 在新判据下可能不再出现——但为**可追溯**，
> 合格时仍写入 `decision.advisory_reasons`（仅观测，不进 `reasons`、不影响 status）。

### 4.3 替代质量信号（核心数据契约）

三个**纯函数**，与既有 `_text_coverage` 等同级、可独立单测，全部定义在 `quality/gate.py`：

```python
def _meaningful_character_ratio(document, evidence) -> float:
    """有意义字符占非空白字符的比例。

    有意义 = 空白以外且 (Unicode 类别 ∈ {Lo,Lu,Ll,Lt,Lm,Nd,Nl,No}
             或 属于内置常用标点集)。
    刻意覆盖 U+25A0（类别 So）与 U+FFFD（类别 So）：两者都**不是**有意义字符，
    因此方块垃圾/替换符污染会被这个指标直接扣分（既有 abnormal_character_ratio
    只认 {Cc,Cf}，对这两者完全无感——见 §2.3(c)）。
    """

def _medical_term_hit_rate(document, evidence) -> float:
    """命中内置医学词典的**行**占比。

    词典来源（单一事实来源，写在模块常量里）：
      medical_rag.quality.lexicon.MEDICAL_TERMS（本次新增，最小可用词表）。
    无内容行时返回 1.0（fail-open 到"不判定"，与 page_coverage 的既有口径一致），
    由 S1 负责拦下"整篇无意义字符"的情形。
    """

def _ocr_line_length_plausibility(document, evidence) -> float:
    """内容行长度落在 [min,max] 的行占比。

    垃圾 OCR 常产出极短（单字符/方块）或极长（未断行）的行。
    """
```

新增 `QualityPolicy` 字段（默认值来自 §4.5 实测，冷启动保守）：

```python
# --- OCR 无置信度时的替代质量判据（仅当 confidence 不可得时启用）---
min_meaningful_character_ratio: float = 0.60
min_medical_term_hit_rate: float = 0.02
min_ocr_line_length: int = 2
max_ocr_line_length: int = 500
min_ocr_line_length_pass_ratio: float = 0.80
```

新增原因码（`gate.py` 常量 + `quality/codes.py` 枚举 + 中文标签）：

```python
REASON_OCR_SUBSTITUTE_QUALITY_INSUFFICIENT = "ocr_substitute_quality_insufficient"
# QualityFailureCode.OCR_SUBSTITUTE_QUALITY_INSUFFICIENT
# 标签："OCR 文本质量信号不足（无置信度且替代指标不合格），需人工确认"
```

**`QualityDecision` 新增只读字段**（`quality/contracts.py`）：

```python
advisory_reasons: tuple[str, ...] = ()
```

语义：**仅用于观测/追溯，绝不参与 status / retrieval_eligible 判定**。
`__post_init__` 只归一化为 tuple，**不**参与既有不变量校验。
用途：OCR 替代信号合格而放行时，把 `confidence_unavailable` 记入 advisory，
使"这份扫描件是无置信度放行的"在诊断与审计里可见。

### 4.4 复核出口（新增组件）

**4.4.1 角色模型（D-B 决策：新增 `role` 声明）**

`auth/policy.py`：

```python
class UserRole(StrEnum):
    PATIENT = "patient"
    REVIEWER = "reviewer"
    ADMIN = "admin"

@dataclass(frozen=True, slots=True)
class AccessContext:
    tenant_id: UUID
    patient_id: UUID
    user_id: UUID
    role: UserRole = UserRole.PATIENT     # <-- 新增，默认最小权限
```

- **默认 `PATIENT`**（最小权限）：所有既有调用点不传 role → 行为**不变**，
  且默认**无权**复核。这是 fail-closed 的默认值。
- `auth/service.py` 从 JWT claims 读 `role`；**缺失时回落 `PATIENT`**
  （不猜测、不继承其他字段）。
- **角色不得由请求头提供**：`allow_insecure_identity_headers` 分支**固定给 `PATIENT`**，
  即使请求头带 role 也忽略——否则开发开关会变成提权后门。这是**必须写进文档的不变量**。

**4.4.2 `ReviewService`（新增 `src/medical_rag/review/service.py`）**

```python
class ReviewService:
    def approve_document(
        self, context: AccessContext, document_id: UUID, confirmed_reasons: list[str]
    ) -> ReviewReceipt: ...
```

行为（全部在一个事务内，失败即回滚）：

1. **鉴权**：`context.role not in {REVIEWER, ADMIN}` → `AccessDenied`（403）。
2. **ACL**：文档必须属于 `(context.tenant_id, context.patient_id)`；否则 `AccessDenied`。
   （复核人**只能**放行自己作用域内的文档；跨租户/跨患者一律拒绝。）
3. **状态前置**：文档/版本当前必须为 `NEEDS_REVIEW`；否则 `InvalidStateTransition`（409）。
4. **重新评估（而不是打标放行）**：
   - 重新跑 `QualityGate.evaluate`，但**以"人工复核已确认"为前提**；
   - 实现方式：对每个 `NEEDS_REVIEW` 的 reason，**逐条**要求人工确认
     （请求体带 `confirmed_reasons: list[str]`，必须**精确覆盖**当前 reasons 集合；
     少一条 → 400；多一条 → 400）。
   - 覆盖后 → 构造 `QualityDecision(status=PASS, reasons=(), metrics=<原 metrics>)`，
     并写 `advisory_reasons = 原 reasons`（可追溯"人工放行了哪些原因"）。
   - **不**修改 `QualityDecision.__post_init__` 不变量（PASS 天然对应 eligible=True）。
5. **持久化**：文档/版本/ParseJob 状态 → `READY`；对应 `EvidenceUnit.retrieval_eligible`
   → `True`；追加 `quality_flags` 记录人工放行（**不覆盖**原 flags）。
6. **重新入索引**：为该版本重新创建 IndexJob（复用 `parsing/service.py` 建单逻辑，
   抽成可复用方法），使新记录以 `eligible=True` 写入索引。
   > 私有路径 `IndexWorker` 判定 `retrieval_eligible = evidence.retrieval_eligible and ... READY`，
   > 因此**必须**同时把 DB 列与文档状态都置为就绪，否则写入侧仍会降级（这是既有正确行为，不改）。
7. **审计**：`AuditService.record(context, AuditAction.REVIEW_APPROVED, resource_type="document", resource_id=document_id)`。
   新增 `AuditAction.REVIEW_APPROVED` 枚举成员。
8. **返回** `ReviewReceipt(document_id, status, index_job_ids, approved_reasons)`。

**4.4.3 API 路由（`api/routes/documents.py`）**

```text
POST /v1/documents/{document_id}/review
  body: {"confirmed_reasons": ["confidence_unavailable"], "note": "..."}
  -> 200 ReviewResponse{document_id, status, index_job_count, approved_reasons}
  403 角色不足 / 404 文档不可见 / 409 状态不允许 / 400 confirmed_reasons 不匹配
```

`note` 为可选的复核备注（**不进**索引、**不进**诊断；仅写审计）。

### 4.5 替代信号标定（实测，非估值）

只读脚本（`.workbuddy/_p03_signal_calib.py`，已清理）在四类样本上实测：

| 样本 | S1 meaningful_ratio | S2 term_hit_rate | S3 line_len_median | 期望 |
|---|---|---|---|---|
| S1 真实中文医学样本正文（561 字） | **1.0000** | 0.6471 | 36.0 | 放行 |
| S2 已知垃圾 `U+25A0` 方块串（120 字） | **0.0000** | 0.0000 | 12.0 | **拦下** |
| S3 替换符污染 `U+FFFD` 中文（52 字） | 0.7692 | 1.0000 | 16.0 | 放行（轻度） |
| S4 真实文本混入零宽字符（600 字） | 0.9850 | 0.7000 | 33.0 | 放行 |

**判据选取（写入 spec 并冻结）**：

- **S1 阈值 `min_meaningful_character_ratio = 0.60`**：好文本 0.985–1.0，垃圾 0.0。
  0.60 落在两者之间且离两端都有裕度（S3 的 0.7692 仍然放行，这正确——它是**真文本**
  只是混了 12 个替换符）。
- **S2 阈值 `min_medical_term_hit_rate = 0.02`**：**仅作辅助**，因为词表小、
  且非医学文本（如口语化段落）可能命中率低。取极低阈值只用于拦"整篇零命中"
  的极端情形（S2 = 0.0）。
- **S3**：`min_ocr_line_length_pass_ratio = 0.80`，行长落在 `[2, 500]` 的行占比 ≥ 0.80。
- **替代判据为"与"关系**：三项**全部**合格才算合格（保守）。

> **诚实声明**：本标定集只有 4 个样本，**不足以宣称阈值已优化**。用途是证明
> "信号有判别力"并给出**可复现的初值**（对齐 `QualityPolicy` 既有冷启动约定：
> 金标准集之前刻意保守，人工复核 50 份真实文档后再收紧）。阈值收紧条件见 §11 阶段 2。

---

## 5. 核心数据契约（汇总）

| 契约 | 文件 | 变更 |
|---|---|---|
| `QualityPolicy` | `quality/gate.py` | 新增 5 个替代判据阈值字段（全部有默认值） |
| `_meaningful_character_ratio` / `_medical_term_hit_rate` / `_ocr_line_length_plausibility` | `quality/gate.py` | 新增纯函数 |
| `REASON_OCR_SUBSTITUTE_QUALITY_INSUFFICIENT` | `quality/gate.py` | 新增常量 |
| `QualityFailureCode.OCR_SUBSTITUTE_QUALITY_INSUFFICIENT` | `quality/codes.py` | 新增枚举 + 标签 |
| `MEDICAL_TERMS` | `quality/lexicon.py`（新文件） | 最小医学词表 + 来源声明 |
| `QualityDecision.advisory_reasons` | `quality/contracts.py` | 新增只读字段（不参与判定） |
| `UserRole` | `auth/policy.py` | 新增 StrEnum |
| `AccessContext.role` | `auth/policy.py` | 新增字段，默认 `PATIENT` |
| role claims 解析 | `auth/service.py` | 从 JWT 读 role（缺失 → PATIENT；请求头一律 PATIENT） |
| `ReviewService.approve_document` | `review/service.py`（新文件） | 新增 |
| `ReviewReceipt` | `review/contracts.py`（新文件） | 新增 |
| `POST /v1/documents/{id}/review` | `api/routes/documents.py` | 新增路由 |
| `AuditAction.REVIEW_APPROVED` | `domain/enums.py` | 新增枚举成员 |
| `_public_record` 资格 | `indexing/orchestrator.py:245-274` | 改读 `evidence.retrieval_eligible`（fail-closed） |
| 公共入库门禁 | `knowledge/ingest_service.py:230-309` | 接入 `QualityGate`（D-C） |

---

## 6. 设计细节

### 6.1 为什么"重新评估"而不是"直接置 eligible=True"

若允许 `NEEDS_REVIEW` 状态下直接把 `retrieval_eligible` 改成 True，就会**破坏
"PASS ↔ eligible" 这个唯一的资格不变量**（`QualityDecision.__post_init__` 强制它）。
后果是所有下游（`IndexWorker`、对账、`derive_document_quality`）都要新增
"eligible 但 status 不是 READY"的第三种组合——一个没有人能推理的状态空间。

因此复核通过的语义被定义为：**在人工确认了每条 reason 之后，重新生成一个 PASS 判决**。
`confirmed_reasons` 必须**精确覆盖**原 reasons，是这条语义的强制形式化——
它防止"点一下全部放行"式的橡皮图章（复核人必须逐条看到并确认原因）。

### 6.2 `confirmed_reasons` 精确匹配的边界

| 情形 | 行为 |
|---|---|
| 与当前 reasons 集合完全相等（忽略顺序） | 允许 |
| 缺少任一条 | 400 `review_reasons_incomplete`（列出缺失项） |
| 含未知/多余原因 | 400 `review_reasons_unknown`（列出多余项） |
| 空列表且当前 reasons 非空 | 400（缺全部） |
| 当前 reasons 为空（并发下已被别人改） | 409（状态前置失败） |

### 6.3 并发与幂等

- 文档状态前置（必须为 `NEEDS_REVIEW`）在**同一事务**内读取并锁定
  （SQLite 下用 `BEGIN IMMEDIATE` 效果等价；实现按既有仓储风格）。
- 两次并发复核同一文档：后者拿到 409（状态已非 NEEDS_REVIEW）。**不做**幂等成功，
  因为"复核"不是可重放的操作（第二次没有原因可确认）。
- 重新创建的 IndexJob 使用确定性复合键（沿用 `parsing/service.py` 既有 idempotency
  约定：`{version}:{evidence}:{bundle...}`），因此重试不会重复入索引。

### 6.4 替代信号与既有 `abnormal_character_ratio` 的关系

**并存，不替换。** `abnormal_character_ratio`（`{Cc,Cf}`）继续拦控制符/零宽/双向字符；
`_meaningful_character_ratio` 拦"非字符类的符号垃圾"（`So`，如 `U+25A0`/`U+FFFD`）。
两者互补，且新指标**只在 `confidence_unavailable_ratio > 0` 时**参与判决——
有真实置信度时行为**逐字节不变**（重要的回归护栏 P1）。

### 6.5 公共入库门禁的接入方式（D-C）

`knowledge/ingest_service.py` 在构造 `PublicIndexRecord` 前，对每个 chunk 文本调用
`QualityGate.evaluate`（以单块为 `CleanedDocument`、单条 `EvidenceUnitDraft`，
`confidence=1.0`——公共知识来自结构化文本，不存在 OCR 置信度问题）：

- `PASS` → `retrieval_eligible=True`（现状）；
- `NEEDS_REVIEW` / `FAIL` → `retrieval_eligible=False`，并把 `reasons` 写进
  **新增列 `KnowledgeChunk.quality_flags`**（与 `EvidenceUnit.quality_flags` 对称）。

> ⚠️ **这是本次影响面最大的一处**：现公共库 5 条全部来自自述"未经医学审核"的样例文件，
> 接入门禁后它们会在下次 reindex 时被判复核 → 公共检索返回空。
> 这是**正确行为**（该内容本就不该作为医学证据），但必须在**交付说明**中显式提示，
> 并在 §11 阶段 3 给出"运营商换正式语料"的操作路径。**不做**自动豁免。

### 6.6 `_public_record` fail-closed 的具体改法

```python
# 变更前（orchestrator.py:246-271）
eligible = (version.status in {READY, NEEDS_REVIEW} and ... )
if not eligible: raise RuntimeError(...)
return IndexRecord(..., retrieval_eligible=True, ...)   # 硬编码

# 变更后
eligible = (
    evidence.retrieval_eligible                       # 新增：读持久化资格
    and version.status == DocumentStatus.READY        # 收窄：NEEDS_REVIEW 不再放行
    and (document is None or document.status == DocumentStatus.READY)
    and evidence.deleted_at is None
    and version.deleted_at is None
    and (document is None or document.deleted_at is None)
)
if not eligible:
    raise RuntimeError("public source is not active and retrieval-eligible")
return IndexRecord(..., retrieval_eligible=eligible, ...)  # 用判定值
```

> 收窄 `NEEDS_REVIEW` 是与私有路径（`IndexWorker` 要求 `== READY`）**对齐**，
> 不是收紧私有语义。该分支实测不可达，因此**对现网行为零变化**——
> 这是"修隐患不改行为"的理想形态。

---

## 7. 错误与降级

### 7.1 降级路径总表

| 场景 | 行为 | 患者/运营看到 |
|---|---|---|
| OCR 无置信度 + 替代信号合格 | PASS → 可检索；`advisory_reasons=("confidence_unavailable",)` | 正常回答 |
| OCR 无置信度 + 替代信号不合格 | NEEDS_REVIEW + `ocr_substitute_quality_insufficient` | 「需人工确认」+ 可复核 |
| 有真实置信度（非 OCR） | **行为逐字节不变** | 同现状 |
| 复核角色缺失/不足 | 403 `AccessDenied` | 「无权复核」 |
| `confirmed_reasons` 不匹配 | 400，列出缺失/多余项 | 前端提示逐条确认 |
| 文档状态非 NEEDS_REVIEW | 409 | 「该文档无需复核」 |
| 复核后重新入索引失败 | 事务回滚，状态**不**变更（保持 NEEDS_REVIEW） | 可重试（不产生"已放行但检索不到"） |
| 医学词表为空/未加载 | S2 恒为 1.0（fail-open 到"不判定"），由 S1/S3 兜底 | — |
| 公共入库门禁判复核 | 该 chunk `retrieval_eligible=False` | 公共检索不返回该内容（正确） |

**复核放行必须是原子的**（倒数第三行）：若允许"状态已置 READY 但 IndexJob 创建失败"，
就会出现"接口说已放行、检索却返回空"——正是 P0-3 的原始症状。因此状态迁移与入索引建单
**必须在同一事务**内，失败整体回滚。

### 7.2 为什么不做"复核通过即全量放行"

因为 `NEEDS_REVIEW` 的原因可能是 `missing_pages`（内容确实缺失）或
`low_text_coverage`（内容确实丢了）。对这类原因"一键盘放行"等于允许残缺内容进入
证据链。`confirmed_reasons` 的精确覆盖要求把这条判断显式交给复核人，并留下审计痕迹。

### 7.3 存量扫描件

现有 `NEEDS_REVIEW` 文档中间有多少是"仅因 confidence_unavailable"而卡住的，
**需要真实数据确认，不做假设**。§11 阶段 4 提供一个只读盘点脚本，
输出按 reason 分布的分类计数；回填策略待盘点结果驱动。**本次不自动回填**。

---

## 8. 安全与合规不变量（逐条可断言）

| # | 不变量 | 断言方式 |
|---|---|---|
| P1 | 有真实置信度时门禁行为**逐字节不变** | 既有 `test_quality_gate.py` 全部用例**不修改**仍通过 |
| P2 | 替代信号**只减不增** reasons | 替代合格时 `set(new.reasons) ⊆ set(old.reasons)`；不合格时 `⊇`（单测逐例比对） |
| P3 | `advisory_reasons` **不参与** status/eligible | 构造 `advisory_reasons` 非空的 PASS 与 NEEDS_REVIEW 各一例，断言 status 只由 reasons 决定 |
| P4 | 角色**不得**来自请求头 | `allow_insecure_identity_headers=True` + 请求头带 `role=admin` → `context.role == PATIENT` |
| P5 | 默认最小权限 | 不传 role 构造 `AccessContext` → `role == PATIENT`；`PATIENT` 调复核 → 403 |
| P6 | 复核受 ACL 限制 | `REVIEWER` 复核**其他租户/患者**文档 → `AccessDenied`，且无状态变更 |
| P7 | 复核必须逐条确认 | `confirmed_reasons` 少一条/多一条 → 400，**无状态变更** |
| P8 | 复核是原子的 | 注入入索引失败 → 文档仍 `NEEDS_REVIEW`，`retrieval_eligible` 仍 False |
| P9 | 复核写审计 | 成功放行后必有一条 `AuditAction.REVIEW_APPROVED`，含 `document_id` 与 `user_id` |
| P10 | 越权/删除残留/复核泄漏恒为 0 | 沿用既有零容忍口径（真实链路断言为 0） |
| P11 | `_public_record` fail-closed | 构造 `evidence.retrieval_eligible=False` 的公共记录 → 抛错/不可检索 |
| P12 | 公共入库门禁生效 | 垃圾文本 chunk → `retrieval_eligible=False` 且不出现在公共检索结果 |
| P13 | 无置信度放行可追溯 | 放行文档的 `quality_flags`/advisory 含 `confidence_unavailable`，可从 DB 查出 |

> **P1 是本次最重要的回归护栏**。若替代信号把"有置信度"的路径也改了，它会立刻失败。

---

## 9. 已发现但本次不修的相邻缺陷（显式上报）

| # | 缺陷 | 影响 | 处理 |
|---|---|---|---|
| D6 | 远程 OCR 服务 schema 不含置信度 | 无法做真正的 OCR 质量判分 | 若未来服务升级返回 score，另立 spec；本次用替代信号 |
| D7 | 存量 `NEEDS_REVIEW` 扫描件未回填 | 已上传的扫描件仍不可检索 | §11 阶段 4 只读盘点 + 独立回填任务 |
| D8 | `MIXED_PDF_OCR_DEFERRED` 路径未纳入本次 | 混合 PDF 仍抛错（不改） | 既有行为，独立任务 |
| D9 | 公共语料无真实医学内容 | 接入门禁后公共检索可能返回空 | 属数据问题（P0-2），非本次范围 |
| D10 | `derive_document_quality` 以 version 状态反推 | 复核通过后读时状态正确，但"复核人/时间"不在此函数暴露 | 若产品需展示复核信息，另立任务 |
| D11 | 全站无速率限制（仅登录有） | 复核接口可被暴力调用 | 既有 P2，独立任务；本次复核接口**不加**新限流（保持与全站一致） |

---

## 10. 测试点

### 10.1 质量门禁（`tests/unit/test_quality_gate.py`）

| 测试 | 断言 |
|---|---|
| **OCR 无置信度 + 替代信号合格 → PASS**（核心修复） | status=PASS，`retrieval_eligible=True`，`confidence_unavailable` ∈ `advisory_reasons`，**不** ∈ `reasons` |
| **OCR 无置信度 + 方块垃圾（U+25A0）→ NEEDS_REVIEW** | reasons 含 `ocr_substitute_quality_insufficient`，**不**含 `confidence_unavailable` |
| **OCR 无置信度 + U+FFFD 污染 → PASS** | 0.7692 > 0.60，放行 |
| **有真实置信度 0.62 → 行为不变**（P1） | reasons 含 `low_ocr_confidence`，`confidence_unavailable` 不出现 |
| **有真实置信度 0.95 + 其他全合格 → PASS**（P1） | 与变更前逐字段相等 |
| 替代合格但另有 `low_text_coverage` → 仍 NEEDS_REVIEW（P2） | reasons 恰为 `{low_text_coverage}` |
| `advisory_reasons` 不影响 status（P3） | 两例断言 |
| 空内容/页缺失等结构性失败**不被替代信号 mask** | FAIL 路径不受影响 |
| `MEDICAL_TERMS` 非空且去重 | 词表契约测试 |
| `ocr_substitute_quality_insufficient` 已在 codes 注册 | `QualityFailureCode.OCR_SUBSTITUTE_QUALITY_INSUFFICIENT == "..."`，`failure_reason_label(...)` 非空 |

> **注意（本项目已知坑）**：`QualityPolicy` 的新字段必须在 `QualityPolicy()` 默认构造下
> 就能让"好文本 PASS"，否则既有测试会集体变红。§4.5 的阈值选取已按此校验。

### 10.2 角色与鉴权（`tests/unit/test_auth_policy.py` + 新 `test_review_auth.py`）

| 测试 | 断言 |
|---|---|
| `AccessContext` 默认 role | `== UserRole.PATIENT`（P5） |
| JWT 带 `role=reviewer` → REVIEWER | `context.role is UserRole.REVIEWER` |
| JWT 缺 role → PATIENT | 回落默认，不抛错 |
| JWT `role` 非法值 → 拒 | `AuthenticationRequired`（见下注） |
| 请求头带 role 被忽略（P4） | `allow_insecure_identity_headers=True` 下 role 恒为 PATIENT |

> **需要显式决策的边界**：非法 `role` 值是"拒"还是"降级为 PATIENT"。
> 本 spec 选定 **拒**（`AuthenticationRequired`）：令牌内容自相矛盾时，
> 失败即拒绝优于猜测调用方意图（与 `_context_from_claims` 既有口径一致）。

### 10.3 复核服务与路由（`tests/unit/test_review_service.py` + `tests/integration/test_review_routes.py`）

| 测试 | 断言 |
|---|---|
| REVIEWER 复核自己作用域内 NEEDS_REVIEW 文档 → READY | 状态翻转；`retrieval_eligible` 列翻 True；IndexJob 已创建 |
| 复核后索引可见 | 用假索引断言写入记录 `retrieval_eligible=True` |
| PATIENT 复核 → 403（P5） | `AccessDenied`；无状态变更 |
| REVIEWER 复核他患者文档 → 403（P6） | `AccessDenied`；无状态变更 |
| 状态非 NEEDS_REVIEW → 409 | `InvalidStateTransition` |
| 缺一条 confirmed_reason → 400（P7） | 错误体列出缺失项 |
| 多一条 → 400（P7） | 错误体列出多余项 |
| 入索引失败 → 全回滚（P8） | 注入失败的 writer；文档仍 NEEDS_REVIEW |
| 审计写入（P9） | 存在 `REVIEW_APPROVED` 事件 |
| 路由端到端（TestClient） | 200 / 403 / 404 / 409 / 400 五种码 |

### 10.4 公共路径（`tests/unit/test_index_orchestrator.py` + `tests/unit/test_knowledge_ingest.py`）

| 测试 | 断言 |
|---|---|
| `_public_record` 尊重 `evidence.retrieval_eligible=False`（P11） | 抛 `RuntimeError` / 不产出可检索记录 |
| `_public_record` 在 `NEEDS_REVIEW` 版本下拒绝 | 抛错（收窄后） |
| `_public_record` 在 READY + eligible 下正常 | 记录 `retrieval_eligible=True` |
| 公共入库垃圾文本 → 不可检索（P12） | chunk `retrieval_eligible=False`，公共检索不返回 |
| 公共入库好文本 → 可检索 | 现状保持 |
| 既有公共入库测试**不修改**仍通过 | 若失败说明改动越界 |

### 10.5 真实链路端到端（`scripts/`，新增）

新增 `scripts/p0_3_ocr_review_e2e.py`，复用真实 OpenSearch + 真实 `QualityGate`：

| 断言 | 期望 |
|---|---|
| ① 构造真实扫描件形态的 OCR 证据（无置信度、内容正常）→ 门禁 PASS | `retrieval_eligible=True`，advisory 含 `confidence_unavailable` |
| ② 构造 U+25A0 垃圾 OCR 证据 → 门禁 NEEDS_REVIEW | reason 恰为 `{ocr_substitute_quality_insufficient}` |
| ③ 复核放行后该证据可被检索 | 真实 `RetrievalService.retrieve` 命中该 id |
| ④ 复核前不可检索（负样本） | 同一查询返回 0 条（该 id 不在结果内） |
| ⑤ 零容忍三项 == 0 | 越权/删除残留/复核泄漏均为 0 |
| ⑥ 幂等：复核两次 → 第二次 409，索引无重复 | 索引该 id 计数不变 |

---

## 11. 实施阶段（每阶段可独立验收）

### 阶段 1（本次）：替代质量信号 + 复核出口 + 公共路径（对应 D-A/D-B/D-C）

- `quality/lexicon.py`（词表）+ `QualityPolicy` 5 个新字段 + 3 个纯函数
- `gate.py` 判决改造 + `codes.py` 新原因码 + `QualityDecision.advisory_reasons`
- `auth/policy.py` `UserRole` + `AccessContext.role`；`auth/service.py` claims 解析
- `review/` 包（service + contracts）+ `POST /v1/documents/{id}/review` + 审计枚举
- `indexing/orchestrator.py:_public_record` fail-closed
- `knowledge/ingest_service.py` 接入门禁 + `KnowledgeChunk.quality_flags` 列（alembic 迁移）
- 全部测试 + `scripts/p0_3_ocr_review_e2e.py` 真实链路验证
- **不改** `cosine_threshold`/`score_space`/`.env`

验收：§10 全部测试通过 + §10.5 六项断言成立 + §8 十三条不变量逐条断言。

### 阶段 2（后续）：阈值收紧 + 存量盘点

- 用真实语料（≥50 份人工复核文档）收紧 S1/S2/S3 阈值，更新 `QualityPolicy` 默认值
- 关闭 D7：存量 `NEEDS_REVIEW` 分类盘点与一次性回填脚本

### 阶段 3（后续）：公共语料替换

- 用正式医学指南替换自述"未审核"的样例文件，使公共库在门禁下重新可用（关闭 D9）

---

## 12. 验收标准（可观测、可复现）

```bash
# 1. 全量测试（记录通过/跳过数，基线：754 passed / 3 skipped）
python -m pytest tests/ -q

# 2. 真实链路：OCR 替代信号 + 复核出口
python scripts/p0_3_ocr_review_e2e.py

# 预期输出：
#   扫描件（无置信度，内容正常）门禁 PASS，advisory 含 confidence_unavailable
#   U+25A0 垃圾门禁 NEEDS_REVIEW（reason=ocr_substitute_quality_insufficient）
#   复核前不可检索 / 复核后可检索（同一查询对比）
#   零容忍违规 == 0
#   幂等：第二次复核 409，索引无重复
```

**完成判定**（对齐 `FEATURE-WORKFLOW` §5）：

- [ ] spec 已批准，全部目标实现，非目标确实没做
- [ ] 每个任务独立验收
- [ ] 全量测试通过（记录通过/跳过数，并说明相对基线的增减）
- [ ] 真实链路端到端跑通，有输出证据（`eval_out/p0_3_e2e.json`）
- [ ] §8 十三条安全不变量逐条断言通过（**尤其 P1：有置信度路径行为不变**）
- [ ] §7 降级路径验证过（替代不合格 / 角色不足 / 状态冲突 / 入索引失败回滚）
- [ ] 偏差与遗留项显式记录（§9 D6–D11）
- [ ] 临时探针文件已清理（`.workbuddy/` 仅剩 `memory/`）

---

## 13. spec 自检

| 检查项 | 结果 |
|---|---|
| **占位符扫描** | 无 TBD / TODO。字段默认值、阈值、错误码关键词、验收数值均为实测定值或显式标注为初值 |
| **一致性** | §4.2 判决矩阵与 §4.3 阈值一一对应；§4.4.2 的"重新评估"与 §6.1 理由一致；§6.5 公共门禁与 §10.4 测试对应；§8 P1–P13 每条有对应测试（§10） |
| **范围** | 阶段 1 含 4 个子系统（门禁/鉴权/复核/公共路径），但每块可独立测试；阶段 2/3 明确拆出。**若评审认为阶段 1 过大，可拆为「1a 门禁+复核」与「1b 公共路径」两轮**（见下方说明） |
| **歧义** | ① 非法 role 是拒还是降级 → §10.2 选定**拒**并给理由。② 复核是打标还是重评估 → §6.1 选定**重评估**并给理由（保护唯一资格不变量）。③ 公共门禁的 reasons 落哪 → §6.5 选定**新增 `KnowledgeChunk.quality_flags` 列**（与证据侧对称）。④ `confirmed_reasons` 匹配精度 → §6.2 给出完整边界表 |

**关于范围的额外说明**：阶段 1 是四个相互独立的子改动，合并的原因是它们同属
"使扫描件可用"这一条链路（门禁放行 → 复核兜底 → 公共路径不再有旁路）。
`1a`（门禁 + 复核）与 `1b`（公共路径）之间**没有依赖**，因此若你想分两轮，
实现计划已按此分组（Task 1–5 为 1a，Task 6–7 为 1b）。

