# chat 端到端可用性修复 —— TDD 实施计划

> 关联 spec：`docs/superpowers/specs/2026-10-05-chat-e2e-availability-design.md`（**待批准**）
> 证据：`docs/superpowers/notes/2026-10-05-p0-5-reproduction-and-rootcause.md`
> 日期：2026-10-05

## Global Constraints

- **执行环境**：`C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`。
  bash 残缺（`dirname`/`ls`/`cat`/`head` 缺失，退出码 127）→ 命令编排一律用 **PowerShell**；
  输出 `| Out-File <tmp> -Encoding utf8` 再 `Read`。
- **不要用 managed python**（无项目依赖）。
- **全量测试**须设 `$env:CODEBUDDY_SAFE_DELETE_ENABLED="0"`，否则 `.tmp_test/` 累积删除触发
  本机 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`（~17 个假 error）。基线 **786 passed / 3 skipped**。
- **探针清代理**：任何真实链路脚本开头必须 pop 全部 `*_PROXY` + `NO_PROXY=*`，
  否则 OpenSearch 走外部代理 16s 超时 → 假阴性。
- **不可破坏的既有契约**：
  - I4：`_check_high_risk_claims` / `_check_numeric_consistency` **一行不许改**。
  - I5：`test_diagnostic_whitelist_parity.py` 必须全绿。
  - I6：`test_scheme_b_rollout.py` / `test_app_lifespan.py` 必须全绿。
- 改 `.py` 后**必须 Grep 复核落盘**（本仓库出现过 Edit 报成功但丢行）。
- 编辑后跑 `ruff check src/medical_rag tests scripts`。
- **医学安全红线**：负样本 S1–S10 每条都必须有对应测试；**任何一条变绿即视为失败**。

---

## Task 1 —— 回归测试固化"超时未接线"（先红）

**目的**：把"配置必须真正生效"变成会失败的测试。

**新建** `tests/contract/test_chat_timeout_wiring.py`：

```python
def test_build_chat_request_honours_explicit_timeout() -> None:
    """显式传入的 timeout 必须写进 ChatRequest。"""

def test_build_chat_request_default_unchanged() -> None:
    """不传时行为逐字节不变（沿用 ChatRequest 默认 30.0）。"""

def test_settings_total_timeout_ge_request_timeout() -> None:
    """启动不变量：total > request > 0；违反必须抛错。"""
```

**验证**：`pytest tests/contract/test_chat_timeout_wiring.py -q` → **前两条应 FAIL/ERROR**
（`build_chat_request` 尚无该参数），第三条按当前默认值可能 PASS（记录实际）。

---

## Task 2 —— 超时接线（转绿）

1. `chat/prompts.py`：`build_chat_request()` 增 `timeout_seconds: float | None = None`，
   非 None 时覆盖 `ChatRequest.timeout_seconds`。
2. `core/config.py`：新增 `model_total_timeout_seconds: float = Field(default=300.0, gt=0)`；
   在 `validate_runtime()` 加不变量 `model_total_timeout_seconds > model_request_timeout_seconds`。
3. `api/composition.py`：向 `ChatOrchestrator` 传
   `total_timeout_seconds=settings.model_total_timeout_seconds`；向 `build_chat_request`
   的调用点（`chat/orchestrator.py:489`）传入 `timeout_seconds=self.model_request_timeout_seconds`
   （`ChatOrchestrator` 需新增该属性并在组合根注入）。
4. `.env` 补注释说明两个超时的关系（**不改值**：request 保持 120）。

**验证**：
- Task 1 测试全绿；
- `Grep "timeout_seconds" src/medical_rag/chat/prompts.py` 确认落盘；
- 既有 `tests/unit/test_chat_prompts.py` 全绿。

---

## Task 3 —— 引用标记解析稳健化（先红 → 转绿）

**测试** `tests/unit/test_citation_marker_forms.py`（新建）：

| 用例 | 输入 | 期望 |
|---|---|---|
| T3-1 | `…判断 [7498fde6-...]。` | 解析出该 id |
| T3-2 | `…判断 [evidence: 7498fde6-...]。` | 解析出该 id |
| T3-3 | `[患者姓名：张三]` | **不产生** id |
| T3-4 | `[evidence:]`（无 id） | **不产生** id |
| T3-5 | `[note]` | 语法匹配但不等于任何候选 → `validate` 报"引用了不存在的证据" |

**实现**：`citations.py:16` 正则改为
`r"\[(?:\s*evidence\s*[:：]?\s*)?([A-Za-z0-9_:\-]+)\]"`。

**验证**：T3-1–T3-5 全绿；既有 `test_citation*` 全绿（I3）。

---

## Task 4 —— 结构行分类（先红 → 转绿）

**测试** `tests/unit/test_structural_line_exemption.py`（新建）：

| 用例 | 输入 | 期望 |
|---|---|---|
| T4-1 | `'## 在家自测血压的注意事项'` | 结构行 → **不**触发句级失败 |
| T4-2 | `'- 减少钠盐摄入…'`（列表符号行，无实质句） | 结构行 → 豁免 |
| T4-3 | `'**关于您报告的血压读数**'` | 结构行 → 豁免 |
| T4-4 | `'## 生活方式方面（以下为公共健康教育知识，并非针对您个人的处方）'` 单独成段 | 段落级**不**判缺失（D-C3 内容长度） |
| **T4-5** | `'**您必须立即停药。**'` | **仍失败**（D-C4 带结论不豁免） |
| **T4-6** | `'## 标题\n\n正文超过30字但无任何引用标记……'` | **仍失败**（S4：豁免不得成为绕过通道） |

**实现**：`citations.py` 新增 `_is_structural_line()` 与 `_content_length()`；
`_check_sentence_citations` 与 `_check_paragraph_coverage` 接入。

**验证**：T4-1–T4-6 全绿（**T4-5/T4-6 必须是"仍然失败"**）。

---

## Task 5 —— 免责句豁免扩展（先红 → 转绿）

**测试** `tests/unit/test_disclaimer_exemption.py`（新建）：

| 用例 | 输入 | 期望 |
|---|---|---|
| T5-1 | `'需要先说明：单次家庭测量的数值如何解读，我无法据此作出判断或诊断。'` | **豁免**（实测样本） |
| T5-2 | `'不能仅凭一次读数确诊。'` | 豁免（既有能力回归） |
| **T5-3** | `'无法排除恶性可能。'` | **仍失败**（D-D2：有实质医学含义） |
| **T5-4** | `'无法确定不是贫血。'` | **仍失败** |

**实现**：`citations.py` 的 `_DISCLAIMER_SENTENCE` 增补 §4.4 D-D1 的模式，
**仅**匹配"无法/不能 + 判断|诊断|结论|确诊"的自我指涉形态。

**验证**：T5-1/T5-2 绿，**T5-3/T5-4 必须仍然失败**。

---

## Task 6 —— 提示词强化 + 契约测试

**测试** `tests/unit/test_chat_prompts.py`（追加，不改既有用例）：

```python
def test_system_prompt_states_every_sentence_and_headers() -> None:
    """提示词必须明确"每一句"与"标题行无需引用"，否则契约会静默失效。"""
    assert "每一句" in SYSTEM_PROMPT
    assert "标题" in SYSTEM_PROMPT
    assert "evidence:" in SYSTEM_PROMPT   # 必须显式禁止该前缀
```

**实现**：按 spec §4.2 D-B2 重写 `SYSTEM_PROMPT` 的引用段。

**验证**：新契约测试绿；`test_system_prompt_demands_citation_on_every_paragraph` 仍绿
（不得删掉既有断言）。

---

## Task 7 —— 安全负样本总表（**验收边界**）

**测试** `tests/unit/test_citation_safety_boundaries.py`（新建）：
把 spec §5 的 **S1–S10 逐条**写成参数化测试，**全部断言 `pytest.raises(UnsupportedClaim)`**。

> 该文件是本次改动的**护栏**。若它有任何一条变绿（不再抛错），说明修正打破了安全边界。

**验证**：10 条全绿（即全部仍被拒绝）。

---

## Task 8 —— 端到端验收脚本（真实链路）

**新建** `scripts/chat_e2e_acceptance.py`，按 spec §8 断言 ①–⑦（N≥5 轮）。

要点：
- 开头清代理；
- OpenSearch 不可达 → **显式失败**；
- 复用 `retrieval_tuning_lab.py` 的常量与 `make_embedding_model(use_cache=True, prefix=None)`；
- `RetrievalService` 构造参数名是 `index=`（**不是** `store=`），且需 `embedding_dimensions=512`；
- `OpenSearchStore(client, public_index=..., private_index=...)` 只需两个参数；
- `SearchQuery` 必须传 `patient_id`；
- 适配器方法是 `generate()`（**不是** `chat()`）；
- 真实模型单次 18–99s → 脚本总时长可能 5–10 分钟，用后台运行。

**验证**：断言 ①–⑦ 全 PASS，输出 JSON 到 `eval_out/chat_e2e_acceptance.json`。

---

## Task 9 —— 全量回归 + 文档

1. 全量：`$env:CODEBUDDY_SAFE_DELETE_ENABLED="0"; pytest tests/ -q`
   → 期望 **≥786 passed + 新增 / 0 failed**。
2. `ruff check src/medical_rag tests scripts` → All checks passed。
3. 更新 `docs/chat链路启动诊断报告-2026-09-29.md`：标注根因③已撤销、②已重写、
   ①目标值已修正，指向本 spec 与证据报告。
4. 工作记忆：追加 Task 1–9 结论与实测数据。

---

## 验收清单（逐条可勾）

- [ ] T1 超时接线契约测试先红后绿
- [ ] T2 `ChatRequest.timeout_seconds == settings 值`；总预算来自配置；启动不变量生效
- [ ] T3 `[evidence: <uuid>]` 可解析；`[患者姓名：张三]` 不可解析
- [ ] T4 结构行豁免；**T4-5/T4-6 仍失败**
- [ ] T5 免责豁免扩展；**T5-3/T5-4 仍失败**
- [ ] T6 提示词强化 + 契约测试
- [ ] T7 **S1–S10 全部仍被拒绝**
- [ ] T8 端到端真实链路 ①–⑦ PASS（`validate` 不再 FAIL）
- [ ] T9 全量 0 failed，ruff 干净
- [ ] I4 数值/高风险检查**一行未改**；I5/I6 白名单与门禁测试全绿

## 明确不做

- `/metrics` 端点
- 证据 ID 缩短 / 前端引用渲染（N1/N2）
- 语义级事实一致性校验（P0-4 评估体系）
- `max_output_tokens` 调整（根因③已证伪）
- 模型网关延迟治理（属网关侧，独立任务）
