# 评估指标的不确定性量化工具 — 实施计划

> spec：`docs/superpowers/specs/2026-10-06-evaluation-significance-tooling-design.md`
> 目标：补齐评估链路中**唯一完全缺失**的一环——统计显著性。

## Global Constraints

- **禁止修改** `evaluate_retrieval` / `release_gate` / `CalibrationArtifact` 的任何既有行为（既有 34 个指标测试必须逐值不变）。
- 新模块**只使用标准库**（`math` / `statistics` / `dataclasses`）。不得引入 scipy / numpy。
  理由：`tests/contract/test_declared_dependencies.py` 强制 `src/` 的第三方 import 必须声明在
  `[project].dependencies`；纯标准库可完全绕开该约束，且避免了新依赖的环境风险。
- 新模块**纯计算、无 I/O**：不读写文件、不读患者文本、不访问网络。
- 环境：一律用
  `C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`
  （managed python 无项目依赖）；PowerShell 编排 + 输出重定向到临时文件；bash 残缺不可用。
- 测试进程须设 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 以避免 `.tmp_test/` 批量删除护栏误触发。

---

## Task 1: 统计原语模块 + 单元测试（TDD）

### Step 1: 写失败测试 `tests/unit/test_significance.py`

覆盖 spec §7 的 A1–A8（正确性）、B1–B5（边界）、D1（无第三方依赖）。

关键断言（对照 spec §5 的独立解析计算）：

```python
def test_wilson_matches_hand_computed_interval() -> None:
    # n=14, 13 成功；Wilson 上界饱和到 1.0
    iv = wilson_interval(13, 14)
    assert iv.low == pytest.approx(0.785, abs=0.002)
    assert iv.high == 1.0
    assert iv.method == "wilson"

def test_mean_ci_uses_t_correction_for_small_samples() -> None:
    # 关键：n=14 时半宽 0.108（t=2.160），而非 z 的 0.097
    iv = mean_ci([1.0] * 13 + [0.5])
    assert (iv.high - iv.low) / 2 == pytest.approx(0.108, abs=0.003)
    assert iv.method == "t"

def test_mcnemar_threshold_lands_at_six_discordant_pairs() -> None:
    assert mcnemar_exact(5, 0) > 0.05
    assert mcnemar_exact(6, 0) < 0.05

def test_mcnemar_is_symmetric() -> None:
    assert mcnemar_exact(3, 7) == pytest.approx(mcnemar_exact(7, 3))
```

**为什么必须断言"t 修正后的半宽"**：这是本设计的核心价值。若实现里误用 z，
半宽会是 0.097 而非 0.108 —— 数值接近，容易被忽略，但正是"小样本低估不确定性"的
同一个错误在重演。必须有测试钉死。

### Step 2: 运行测试，确认失败

```powershell
& $py -m pytest tests/unit/test_significance.py -q
```
预期：`ModuleNotFoundError: No module named 'medical_rag.observability.significance'`。

**这一步不可跳过**——否则无法证明测试有效。

### Step 3: 实现 `src/medical_rag/observability/significance.py`

内容按 spec §4 的六个函数 + `Interval` dataclass。要点：

- `wilson_interval`：`n==0` / `successes∉[0,n]` / 非整数类型 → `ValueError`（fail-closed）。
- `_t_critical(df)`：查表 `{1..30, 40, 60, 120}` + 线性插值；`df > 120` 回落 `z`。
- `mean_ci`：`len < 2` → `ValueError`；`n < 30` → `method="t"`，否则 `"normal"`。
- `mcnemar_exact`：`b+c==0` → `1.0`；双尾和截断至 `1.0`。
- `paired_sample_size`：`delta <= 0` → `ValueError`；`discordance <= delta**2` → `ValueError`（无解）。
- `proportion_ceiling`：`k <= 0` → `ValueError`。

每个函数 docstring 写清**公式来源**与**边界语义**（沿用本仓库既有 docstring 风格：
说明"为什么这样做"而非"做了什么"）。

### Step 4: 运行测试并跑既有指标测试确认不回归

```powershell
& $py -m pytest tests/unit/test_significance.py tests/unit/test_retrieval_metrics.py -q
```
预期：新增全绿 + 既有 **34 passed**。

**验收**：A1–A8、B1–B5、D1 全部通过。

---

## Task 2: 接入报告层

### Step 1: 在 `scripts/retrieval_eval_report.py` 新增两节

1. **「统计显著性」节**：
   - 对 `quality` 用例的逐用例 `recall` 列表算 `mean_ci` → 输出区间；
   - 对 `rejection` / safety 正确率算 `wilson_interval`；
   - 显式打印 `n`，并在 `n < 30` 时追加提示"样本量不足，不判定达标"。

2. **「分母可及性体检」节**：
   - 对每个 quality 用例取 `|gold|`（从 `data["cases"]` 的 `gold` 字段读）与 `top_k`，
     算 `proportion_ceiling`；
   - 三态判定 `capped` / `pass` / `fail`（spec §5.5）；
   - 汇总：`capped` 用例数、占比，以及一句话结论。

### Step 2: 更新 `derive_findings` 的结论

- 现有结论"**top_k 影响召回上限与查准率**"中关于查准率的部分，
  补上"**该值受 `|gold|/K` 上界约束**"的说明；
- 新增结论条目：总结样本量是否足够下结论。

### Step 3: 重新生成报告并人工核对

```powershell
& $py scripts/retrieval_eval_report.py
```
前提：`eval_out/retrieval_eval_results.json` 已存在（无需重跑实验台）。

**验收**：C1（`capped` 用例被正确识别）、C2（报告含 CI 与样本量）、C3（无回归）。

---

## Plan Self-Review

| 检查项 | 结论 |
|---|---|
| 每个任务可独立验收？ | ✅ Task 1 靠单测；Task 2 靠报告产出 |
| TDD 顺序完整？ | ✅ 先写失败测试 → 确认真失败 → 实现 → 验证 |
| 是否改动既有行为？ | ✅ 否。新模块 + 报告层加法，`evaluate_retrieval` 一字未动 |
| 是否引入新依赖？ | ✅ 否。纯标准库 |
| 有无隐藏的 I/O 或环境依赖？ | ✅ 无。Task 2 读的是既有 JSON，可离线跑 |
| 数值是否可独立复核？ | ✅ spec §7 A1–A8 全部给了期望值与来源 |
| 已知缺口是否显式记录？ | ✅ spec §8 五条 |

**风险**：Task 2 依赖 `eval_out/retrieval_eval_results.json` 中存在 `gold` 字段。
已确认该文件由 `retrieval_tuning_lab.py` 写入且含 `cases[].gold`（`report["cases"]`），
无需重跑实验台。

---

## 实施偏差记录（2026-10-06 执行后补记）

> 遵循 FEATURE-WORKFLOW 规则 4：与 plan 不一致处必须显式记录。

| # | 偏差 | 原因 | 影响 |
|---|---|---|---|
| **DEV-1** | Task 1 的测试期望值有 3 处需修正（非实现错误）：`wilson(14,14).high` 用 `approx`（浮点残差 1e-17）；`wilson(150,300).low` 期望 0.4498 → 实际 0.4438（断言算错）；`mean_ci` 的 n=30 边界用 `[0.5]*28+[1.0]`（长度 29）而非 `*29`（长度 30） | plan Step 1 的示例值系手算，未复核 | 仅测试文件，实现未变 |
| **DEV-2** | 新增 `ceiling_status(gold_count, k, measured, threshold) -> "capped"/"pass"/"fail"` 与 `Interval.center` | spec §5.5 的三态判定逻辑原计划写在报告脚本里，但脚本顶部 `import opensearchpy` 使其无法被单测导入（会真连 OpenSearch）。抽到 `significance.py` 才能覆盖 | **改善**：三态判定纳入单测；AGENTS 无第二条实现 |
| **DEV-3** | gold 计数按**内容去重口径**（`dedup_gold_counts`），非 `len(case["gold"])` | 排序指标按 hash 去重计算；私有用例原始 `len(gold)=4` 实为 1 组重复内容。不去重会把上界误算为 1.0 | **关键**：不去重则分母可及性体检失效（已写进 spec §5.5） |
| **DEV-4** | 真实数据 13 条 `capped` / 1 条 `fail`（priv-red-cell），spec §5.5 原估"12 条上界 0.333" | 真实 `gold` 分布与 spec 手估不同 | spec §5.5 已更正 |
| **DEV-5** | `findings.json` 新增机器可读 `significance` 块（20 配置 × recall/hit_rate 区间） | 便于后续程序消费 | 纯增量 |

**验证结果**：全量 `976 passed / 3 skipped / 0 failed`；ruff 3 文件 clean；
`parameter_comparison.md` 含「统计显著性」与「分母可及性体检」两节；
C1（pub-* 判 `capped`）/ C2（含 CI 与样本量）/ C3（无回归）全部满足。
