# D17 告警信号可采集性修复 —— TDD 实施计划

> 关联 spec：`docs/superpowers/specs/2026-10-05-d17-alert-signal-availability-design.md`（**待批准**）
> 调研：`docs/superpowers/notes/2026-10-05-d17-alert-wiring-findings.md`
> 日期：2026-10-05

## Global Constraints

- **执行环境**：`C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`。
  bash 残缺（`dirname`/`ls`/`cat`/`head` 缺失，退出码 127），命令编排一律用 **PowerShell 工具**；
  输出 `| Out-File <tmp> -Encoding utf8` 再 `Read`。打印中文前 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`。
- **不要用 managed python**（无项目依赖）。
- **全量测试**须加 `$env:CODEBUDDY_SAFE_DELETE_ENABLED = "0"`，否则
  `tests/conftest.py:37` / `test_migrations.py:37` 的 `.tmp_test/` 累积删除会触发
  本机 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`（~17 个假 error）。基线 **777 passed / 3 skipped**。
- **不可破坏的既有契约（逐条有测试守护）**：
  - H4：未知诊断键仍被丢弃（白名单**不扩自由文本面**）。
  - H5：`candidate_ids` 等候选文档 id **绝不外泄**（不得因本次改动放宽）。
  - H6：`tests/integration/test_chat_orchestration.py` **一行都不许改**。
- 改 `orchestrator.py` 后**必须 Grep 复核落盘**（该仓库出现过 Edit 报成功但丢行）。
- 编辑 `.py` 后用 `ruff check` 确认零新增告警（当前基线：All checks passed）。

---

## Task W1 —— 契约测试：两道白名单 gate 键一致性（先红）

**目标**：把"新增 gate 诊断键必须两侧可见"变成会失败的测试。

**新建** `tests/contract/test_diagnostic_whitelist_parity.py`：

```python
"""契约：gate 相关诊断键必须在 metrics 侧与 HTTP 侧**同时**可见。

背景（2026-10-05 调研）：仓库有两道独立白名单——`observability.metrics._safe_diagnostics`
与 `chat.orchestrator._answer_diagnostics`。A1（阶段 3A）只修了前者，而后者才是唯一
生产出口，导致告警条件 1 的判据 `gate_skipped_reason` 在 HTTP 响应中缺失。
本测试防止该类缺陷复发。
"""
```

**断言内容**：

1. `gate_policy` / `gate_score_space` / `gate_skipped_reason` 三键**均**在
   `_ANSWER_DIAGNOSTIC_KEYS` 中（字面量断言，`inspect.getsource` 校验键名存在）。
2. 上述三键在 `_safe_diagnostics` 中**放行**（不哈希）——回归 A1 成果。
3. **一致性推导**：从 metrics 侧的 gate 枚举集合（`_SAFE_GATE_POLICIES` /
   `_SAFE_SCORE_SPACES` / `_SAFE_GATE_SKIP_REASONS`）映射到对应诊断键名，
   断言这些键名 ⊆ `_ANSWER_DIAGNOSTIC_KEYS`。

**注意（避免测试本身成为噪音）**：映射表用**显式字面量**，不做字符串拼接猜测。
若未来新增 gate 诊断键，**必须**同时更新该映射表——这是有意的"摩擦"，
失败信息中须写明"请同时更新 metrics 与 orchestrator 两处白名单"。

**验证**：`pytest tests/contract/test_diagnostic_whitelist_parity.py -q` → **必须 FAIL**
（当前 `_ANSWER_DIAGNOSTIC_KEYS` 缺这三键）。

---

## Task W2 —— HTTP 白名单增补 4 键（转绿）

**文件**：`src/medical_rag/chat/orchestrator.py`，修改 `_ANSWER_DIAGNOSTIC_KEYS`（`:37-60`）。

**增补**：

```python
    "gate_score_space",
    "gate_skipped_reason",
    "vector_candidates_gated_out",
    "lexical_gated_out",
```

**注释**：在元组上方 docstring 补一句，说明这些键是告警条件 1/2 的判据来源，
且与 `observability.metrics` 的白名单**必须同步**（指向 W1 的契约测试）。

**验证**：
1. `pytest tests/contract/test_diagnostic_whitelist_parity.py -q` → **PASS**。
2. `Grep "gate_skipped_reason" src/medical_rag/chat/orchestrator.py` → 确认落盘。
3. `python -c "from medical_rag.chat.orchestrator import _ANSWER_DIAGNOSTIC_KEYS; print(len(_ANSWER_DIAGNOSTIC_KEYS))"` → 26。

---

## Task W3 —— HTTP 层端到端断言：门禁跳过可读（新覆盖）

**文件**：`tests/integration/test_chat_orchestration.py` 或新建
`tests/integration/test_gate_skip_observable_via_http.py`（**建议新建**，
理由：`test_chat_orchestration.py` 受 H6 保护，新增该文件更清晰）。

**测试内容**（复用 `FakeRetriever` 模式，`test_chat_orchestration.py:29-38`）：

```python
def test_gate_skipped_reason_is_readable_in_http_response() -> None:
    """告警条件 1 的判据必须能穿过编排层到达响应。

    这是完整链路的最后一环：
      服务层 .diagnostics (test_retrieval_service.py)
        → metrics 层 _safe_diagnostics (test_safe_diagnostics_gate_keys.py)
        → HTTP 层 AnswerResponse.diagnostics (本测试)
    """
    result = RetrievalResult(
        items=[], reason="insufficient_evidence",
        diagnostics={
            "gate_policy": "cosine_absolute",
            "gate_score_space": "l2_inv",
            "gate_skipped_reason": "vector_path_unavailable",
            "vector_candidates_gated_out": 0,
            "lexical_gated_out": 0,
        },
    )
    orchestrator = ChatOrchestrator(retriever=FakeRetriever(result), model_gateway=...)
    response = orchestrator.answer(context(), "请解释我的报告")
    assert response.diagnostics["gate_skipped_reason"] == "vector_path_unavailable"
    assert response.diagnostics["gate_score_space"] == "l2_inv"
```

**另加 H2 断言**：正常（未跳过）时该键为 `None`，**不是** `sha256:...`
（防止 `None` 被误哈希——白名单是 key 过滤，`None` 应原样透传）。

**验证**：`pytest tests/integration/test_gate_skip_observable_via_http.py -q` → PASS。

---

## Task W4 —— 负例：白名单未放宽（H4 / H5）

**同文件**，2 条测试：

```python
def test_unknown_diagnostic_key_is_still_dropped() -> None:
    """H4：白名单不扩自由文本面。"""
    from medical_rag.chat.orchestrator import _answer_diagnostics
    assert "note" not in _answer_diagnostics({"note": "患者姓名张三"})

def test_candidate_ids_never_leak() -> None:
    """H5：候选文档 id 绝不外泄（回归线，本次不得放宽）。"""
    from medical_rag.chat.orchestrator import _answer_diagnostics
    out = _answer_diagnostics({"candidate_ids": ["c1", "c2"], "ranks": {"c1": 1}})
    assert "candidate_ids" not in out and "ranks" not in out
```

**验证**：两条均 PASS。

---

## Task W5 —— 全量回归 + 文档 + 交付清单

1. **全量**：`$env:CODEBUDDY_SAFE_DELETE_ENABLED="0"; pytest tests/ -q`
   → 期望 **≥780 passed / 3 skipped / 0 failed**（基线 777 + 新增 ≥5）。
2. **ruff**：`ruff check src/medical_rag tests` → All checks passed。
3. **原 spec 增补**：在 `2026-09-28-cosine-gate-enablement-design.md` §6.4 后加一段
   **边界修正**，内容 = 本次 spec §2 的偏差表（说明"仓库内可采集性"是前置条件，
   由 `2026-10-05-d17-...-design.md` 补齐），并在 §9 D17 行标注"仓库侧已修，通道待运维接"。
4. **交付告警采集清单**：`docs/superpowers/notes/2026-10-05-gate-alert-collection-checklist.md`，
   逐条给出：判据键名 / 取值 / 触发条件 / 采集来源（`POST /answers` 响应 `diagnostics`）/
   建议阈值。**必须写明"本次未提供 `/metrics`，采集需从响应体或日志做"**。

---

## 验收清单（逐条可勾）

- [ ] W1 契约测试先红后绿（失败信息含"两处白名单"指引）
- [ ] W2 `_ANSWER_DIAGNOSTIC_KEYS` 含 4 新键（落盘已 Grep 复核）
- [ ] W3 HTTP 端到端：跳过场景可读 + 正常场景为 `None` 非哈希
- [ ] W4 负例：未知键被丢弃、`candidate_ids`/`ranks` 不外泄
- [ ] W5 全量 ≥780 passed / 0 failed，ruff 干净
- [ ] H6 `test_chat_orchestration.py` **一行未改**（`git diff --stat` 或人工核对）
- [ ] 原 spec §6.4 增补偏差说明；D17 行更新
- [ ] 告警采集清单交付（含"无 `/metrics`"的显式说明）

## 明确不做

- `/metrics` 端点（用户已决策；`MetricsRecorder` 改进程级累计属独立重构）
- 统一两道白名单为单一实现（用契约测试替代）
- 告警通道本身（Prometheus 规则 / 报警渠道，仓库外）
- D12 `calibration_mismatch_records` 脱敏（独立任务）
