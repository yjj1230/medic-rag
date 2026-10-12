# 评估指标的不确定性量化工具 — 设计文档

> 受理日期：2026-10-06
> 前置文档：`docs/superpowers/notes/2026-10-06-rag-eval-rigor-guide.md`（五环节方法论 §4 统计显著性）
> 关联既有实现：`src/medical_rag/observability/metrics.py:evaluate_retrieval`、`scripts/retrieval_eval_report.py`
> 关联缺陷：`eval_out/指标达标核对表.md` §B.2（P@K 口径不可达）、§D（零容忍假绿）

---

## 1. 背景

### 1.1 现状痛点

本项目的评估链路已能产出完整的分层指标（`eval_out/parameter_comparison.md`），但**所有结论都是点估计**：

| 现有结论 | 数字 | 缺失 |
|---|---|---|
| Recall@3 达标（≥0.90 稳定期） | 0.964 | 无置信区间；n=14 时 **t 区间下界仅 0.887 < 0.90** |
| 阈值 0.52 优于 0.50 | 20/22 vs 18/22 | 无配对检验，McNemar 精确 p=0.5000（不显著） |
| P@K 未达标（0.333 < 0.50） | 0.333 | **未识别这是 `min(1,\|gold\|/K)` 的数学上界** |

> ⚠️ **口径提醒**：Recall@3 是**连续量**（逐用例取值），须用 t 区间，下界为 **0.887**；
> `0.785` 是**命中率**（二值量）的 Wilson 下界，二者不可混用——本表初稿即误用了它，
> 已在实施阶段更正（详见方法论文档 §4.2.3）。

第三条已造成实际误判：`指标达标核对表.md` 把 P@K 列为"❌ 未达标"，而真实情况是"**评估集每条只标 1 个 gold、K=3，上界恰为 0.333，检索一条都没丢**"。这是**标注缺陷被误读为检索缺陷**。

### 1.2 为什么现在做

统计显著性被认定为五环节中**唯一完全缺失**的一环，且投入产出比最高（方法论文档 §0.2）。补齐它能同时修掉两类问题：

- **假阳性**：把噪声当结论（如 0.52 优于 0.50）；
- **假阴性**：把数学上界当缺陷（如 P@K）。

---

## 2. 目标与非目标

### 2.1 目标

- **G1** 提供比例的置信区间（Wilson），用于 Hit Rate / 拒答正确率等二值指标。
- **G2** 提供均值的置信区间（t 修正），用于 Recall@K / MRR / nDCG 等逐用例连续指标。
- **G3** 提供配对显著性检验（McNemar 精确检验），用于同一批 query 上两套配置的对比。
- **G4** 提供样本量核算（配对设计与独立两比例对照），用于规划评测集规模。
- **G5** 提供**分母可及性体检**：自动识别 `|gold| < K` 导致指标上界被锁死的用例。
- **G6** 把 G1/G2/G5 接入 `scripts/retrieval_eval_report.py` 的报告输出。

### 2.2 非目标（本次明确不做）

- **不做** 贝叶斯区间 / bootstrap 重采样（Wilson + t 已足够，且无需随机数、可复现）。
- **不做** 多重比较校正（Bonferroni 等）——当前对比配置数少，且校正会掩盖真信号，留作后续议题。
- **不做** 自动阻断门禁（`release_gate` 的统计缓冲改造）。本设计只做"量化与呈现"，**门禁逻辑改动需单独 spec**（涉及 `eval/golden.py` 的既有行为）。
- **不做** gold 标注补充与负样本集构造——前者需人工判定，后者需写库，均超出本设计范围。
- **不改** `evaluate_retrieval` 的既有返回值（新增模块，零行为变更）。

---

## 3. 总体架构

```
scripts/retrieval_eval_report.py          ← 报告层（消费方）
        │  读取 eval_out/retrieval_eval_results.json
        ▼
src/medical_rag/observability/significance.py   ← 本设计新增（纯计算，无 I/O）
        │  wilson_interval / mean_ci / mcnemar_exact / paired_sample_size / proportion_ceiling
        ▼
（无外部依赖，仅标准库 math / statistics / dataclasses）
```

**分层理由**：统计原语是纯函数，放在 `src/` 可被测试与复用；报告层在 `scripts/` 里组装呈现。两者解耦，未来加 `/metrics` 导出时可直接复用原语。

**依赖影响**：仅使用标准库，**不新增 `[project].dependencies` 条目**（`tests/contract/test_declared_dependencies.py` 不受影响）。

---

## 4. 核心数据契约

```python
@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float
    n: int              # 样本量，供调用方判断可靠性
    method: str         # "wilson" | "t" | "normal"

    @property
    def width(self) -> float: ...          # high - low
    def contains(self, value: float) -> bool: ...
    def overlaps(self, other: "Interval") -> bool: ...   # 两区间是否重叠
```

```python
def wilson_interval(successes: int, n: int, *, z: float = 1.959964) -> Interval
def mean_ci(values: Sequence[float], *, z: float = 1.959964) -> Interval
def mcnemar_exact(b: int, c: int) -> float
def paired_sample_size(delta: float, discordance: float, *, alpha=0.05, power=0.80) -> int
def independent_proportion_sample_size(p1: float, p2: float, *, alpha=0.05, power=0.80) -> int
def proportion_ceiling(gold_count: int, k: int) -> float
```

**契约细节**：

| 函数 | 边界行为 | 理由 |
|---|---|---|
| `wilson_interval` | `n == 0` → 抛 `ValueError` | 空样本无区间可言，静默返回 (0,1) 会掩盖问题 |
| `wilson_interval` | `successes` 非 `[0, n]` 整数 → `ValueError` | fail-closed，避免计数错误被平均掉 |
| `mean_ci` | 空序列 → `ValueError`；`len == 1` → 抛 `ValueError`（不可估方差） | 单点无法估不确定性 |
| `mean_ci` | `df <= 30` 用 **t 表**；否则用 z | 小样本必须 t 修正（n=14 时 t=2.16 vs z=1.96，差 10%） |
| `mean_ci` | `n < 30` 时 `method = "t"`，否则 `"normal"` | 调用方可据此决定是否下结论 |
| `mcnemar_exact` | 双尾精确检验，`b + c == 0` → `1.0` | 无不一致样本时无法拒绝原假设 |
| `paired_sample_size` | `delta <= 0` 或 `discordance <= delta²` → `ValueError`（或返回不可达标记） | 数学上无解，不应返回误导性数字 |
| `proportion_ceiling` | `gold_count >= k` → `1.0`；否则 `gold_count / k` | P@K 的数学上界 |

---

## 5. 设计细节

### 5.1 Wilson 区间（G1）

比例指标的正态近似在小样本下失效（`p=0.964, n=14` 时区间上界会超过 1）。Wilson score 区间在 `p → 0/1` 与小样本下都表现良好：

```
中心 c = (p̂ + z²/2n) / (1 + z²/n)
半宽 h = z·sqrt(p̂(1−p̂)/n + z²/4n²) / (1 + z²/n)
区间 = [max(0, c−h), min(1, c+h)]
```

**为什么不用 Clopper-Pearson（精确法）**：后者更保守，区间更宽，会在本来就小的样本上进一步掩盖信号。Wilson 是覆盖率与宽度的良好折中，也是业界评估工具（RAGAS / DeepEval 等的 CI 实现）的常见选择。

### 5.2 均值区间（G2）

逐用例指标（Recall@K 等）是**连续量**，用样本标准差估计：

```
区间 = x̄ ± t_{α/2, n−1} · s / sqrt(n)
```

t 临界值用**表查 + 线性插值**（避免引入 scipy）。表覆盖 `df ∈ {1..30, 40, 60, 120, ∞}`，`df > 120` 用 `z`。

**为什么需要 t 而不是直接用 z**：n=14 时 `t_{0.975,13} = 2.160` vs `z = 1.960`，区间宽 10%。用小样本的 z 会系统性低估不确定性——正是当前 0.964 被误读为"达标"的机制。

### 5.3 McNemar 精确检验（G3）

**配对设计**：同一批 query 上对比配置 A 与 B，只看不一致格：

| | A 通过 | A 不通过 |
|---|---|---|
| **B 通过** | a | **b** |
| **B 不通过** | **c** | d |

原假设：`b` 与 `c` 来自同一分布（`Binomial(b+c, 0.5)`）。双尾精确 p：

```
p = 2 · Σ_{k=b}^{b+c} C(b+c, k) · 0.5^(b+c)     （截断至 1.0）
```

**为什么不用卡方近似**：`b + c < 25` 时卡方近似不可靠，而本项目的不一致样本数正是 2~6 量级。必须用精确检验。

**本项目实测**（b=2, c=0）：`p = 0.5000` → **无法拒绝原假设**。达到 `p < 0.05` 需 `b ≥ 6`。

### 5.4 样本量核算（G4）

**配对设计**（Connor 1987）：

```
n = (z_{α/2}·sqrt(π_d) + z_β·sqrt(π_d − δ²))² / δ²
```

其中 `δ` = 待检出差异，`π_d` = 不一致率。

**独立两比例**（对照参考，展示配对设计的信息效率优势）：

```
n = (z_{α/2}·sqrt(2·p̄(1−p̄)) + z_β·sqrt(p1(1−p1) + p2(1−p2)))² / δ²
```

**实测对照**（检出 0.90 vs 0.95）：配对 `π_d=0.10` → **312**；独立两比例 → 每侧 **435**。配对设计省约 30%。

### 5.5 分母可及性体检（G5）

对每个 quality 用例计算：

```
上界 = min(1, |gold| / K)
```

若某用例的 `P@K` 实测值 `≥ 上界 − ε`，则该用例的查准率**被标注口径封顶**，不应用阈值判定。

> 🔴 **gold 必须按内容去重口径**（实施阶段发现，易错）：本项目所有排序指标都按
> `content_hash` 去重计算，故此处 `|gold|` 也须取**去重后的唯一内容数**。
> `medical-rag-private-local` 中同一 `blood_report.pdf` 的多条 chunk 文本完全相同、
> 共享一个 hash，标注时其 ID 全部列进 `gold`（原始 `len(gold)=4`）。若不去重，
> 会得到上界 `min(1, 4/3) = 1.0`（看似可达），而真实上界是 `min(1, 1/3) = 0.333`——
> "口径封顶"会再次被误读成"检索缺陷"，正是本设计要修的问题。
> 复现：`python -c "from scripts.retrieval_eval_report import load_hashes"` 见
> `eval_out/parameter_comparison.md` 「分母可及性体检」节。

**实测（2026-10-06，`retrieval_eval_results.json`）**：14 条 quality 用例去重后
`|gold| ∈ {1, 2}`，K=3 → **13 条上界 0.333、1 条（priv-red-cell）0.667**。
实测 P@K 均值 0.333：13 条 `capped`（含 9 条 pub + 4 条 priv，其召回均为 1.0）、
1 条 `fail`（priv-red-cell，召回 0.5、上界 0.667 未锁死）→ 结论应为
**"P@K 未达标绝大部分是口径不可达"**，而非检索缺陷；唯一真缺口 priv-red-cell 单独归因。

**输出三态**（而非二元达标/未达标）：

| 判定 | 条件 |
|---|---|
| `capped` | 实测 ≥ 上界 − ε **且** 上界 < 1.0 → 口径封顶，不可判定 |
| `pass` | 上界 = 1.0 且 实测 ≥ 阈值 |
| `fail` | 上界 = 1.0 且 实测 < 阈值 |

---

## 6. 实施阶段

| 阶段 | 内容 | 可独立验收 |
|---|---|---|
| **阶段 1** | `significance.py` 的统计原语（G1–G5）+ 单元测试 | 运行 `pytest tests/unit/test_significance.py` 全绿 |
| **阶段 2** | 接入 `retrieval_eval_report.py`：报告新增「统计显著性」与「分母可及性体检」两节 | 运行脚本，产出报告含 CI 与三态判定 |

---

## 7. 验收标准

### A. 统计原语正确性（对照解析解 / 已知值）

> ⚠️ 所有期望值均已用独立脚本（纯公式实现）复核，见下方「复核方式」列。
> 初稿曾把 `Wilson(14,14)` 的下界误用于 `Recall@3` 的区间（方法混淆），已更正。

| # | 断言 | 期望值 | 复核方式 |
|---|---|---|---|
| A1 | `wilson_interval(14, 14)` → `[0.7847, 1.0]` | 上界饱和至 1.0 | Wilson 公式独立计算 |
| A1b | `wilson_interval(13, 14)` → `[0.6853, 0.9873]` | 半宽 ±0.151 | 同上 |
| A1c | `wilson_interval(2, 2)` → `[0.3424, 1.0]` | 2/2 的下界仅 0.34 | 同上 |
| A2 | `wilson_interval(300, 300)` → `[0.9874, 1.0]` | — | 同上 |
| A3 | `mean_ci([1.0]*13 + [0.5])` → mean 0.9643, **半宽 0.0772**, `method == "t"` | n=14 时 t=2.1604 | `statistics.stdev` 独立复算 |
| A4 | `mcnemar_exact(2, 0) == 0.5` | 精确检验二项和 | `comb` 独立求和 |
| A5 | `mcnemar_exact(6, 0) < 0.05 <= mcnemar_exact(5, 0)` | 门槛落在 b=6 | 同上 |
| A6 | `mcnemar_exact(b, c) == mcnemar_exact(c, b)` | 检验对称性 | 公式结构 |
| A7 | `paired_sample_size(0.05, 0.10) == 312` | Connor 1987 | 公式独立计算 |
| A7b | `independent_proportion_sample_size(0.90, 0.95) == 435` | 对照口径 | 独立两比例公式 |
| A8 | `proportion_ceiling(1, 3) ≈ 0.3333`；`(3, 3) == 1.0` | 上界公式 | — |

### B. 边界与 fail-closed

| # | 断言 |
|---|---|
| B1 | `wilson_interval(0, 0)` 抛 `ValueError` |
| B2 | `wilson_interval(5, 3)` 抛 `ValueError`（successes > n） |
| B3 | `mean_ci([])` 与 `mean_ci([0.5])` 抛 `ValueError` |
| B4 | `paired_sample_size(0.0, 0.1)` 抛 `ValueError` |
| B5 | `mean_ci` 在 `n=14` 时 `method == "t"`，`n=50` 时 `method == "normal"` |

### C. 集成

| # | 断言 |
|---|---|
| C1 | 报告含「分母可及性体检」表，且 `pub-*` 用例被判 `capped` |
| C2 | 报告含 Wilson 区间列，且明确标注样本量 |
| C3 | 全部既有测试不回归（`tests/unit/test_retrieval_metrics.py` 34 passed） |

### D. 安全不变量

| # | 断言 |
|---|---|
| D1 | 新模块**不导入**任何第三方包（仅标准库）→ `test_declared_dependencies.py` 通过 |
| D2 | 新模块**不读取**患者文本、不写文件（纯计算） |
| D3 | **不修改** `evaluate_retrieval` / `release_gate` 的任何既有返回值 |

---

## 8. 显式缺口（不静默）

1. **区间不解决标注错误**：Wilson 区间量化的是"抽样不确定性"，无法发现"gold 标错了"。标注者间一致性（Cohen's kappa）是另一套独立机制。
2. **不做多重比较校正**：同时比较 20 个配置会放大假阳性率。当前报告展示全部配置的 CI，读者需自行注意。
3. **t 表为查表插值**：非精确逆 CDF，`df > 30` 的插值误差在 0.1% 量级，对评估结论无影响（已在 docstring 标注）。
4. **门禁未接统计缓冲**：`release_gate` 仍用点估计比较（§2.2 非目标）。这会导致真实能力 0.95、某次测出 0.94 时误报 blocking。
5. **零容忍项仍为假绿**：负样本集未构造，统计工具无法替代它。
