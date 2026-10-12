# 忠实度评估 Harness — 实施计划

> spec：`docs/superpowers/specs/2026-10-06-faithfulness-evaluation-harness-design.md`
> 目标：补齐 `CitationValidator` 四层校验栈中**唯一规则不可达**的第 ④ 层——「证据是否在语义上支持断言」。

## Global Constraints

- **禁止修改** `CitationValidator` 的任何既有成员与行为（`tests/unit/test_citations.py` /
  `test_citation_safety_boundaries.py` / `test_disclaimer_exemption.py` 必须逐条不变）。
  本设计**只是它的第二个消费者**，复用其成员时以 `is` 别名锁定（见 Task 1 Step 3）。
- **禁止修改** 生产请求路径的任何行为：不改 `_ANSWER_DIAGNOSTIC_KEYS`（两侧白名单）、
  不改 `.env`、不新增 `faithfulness_gate_enabled`、不改 `orchestrator.answer()` 的返回值。
  验收断言见 spec §6 S10。
- 新模块**只使用标准库**（`json` / `re` / `dataclasses` / `math`）。不得引入 scipy / numpy / ragas。
  理由：`tests/contract/test_declared_dependencies.py` 强制 `src/` 的第三方 import 必须声明在
  `[project].dependencies`；纯标准库完全绕开该约束。
- **`src/medical_rag/eval/faithfulness.py` 不得 import 任何联网/DB 模块**（`httpx` / `opensearchpy` /
  `sqlalchemy` / `boto3`）。判官与网关的接线一律留在 `scripts/`——否则单测导入即触发真连接。
  （前例：`scripts/retrieval_eval_report.py` 顶部 `import opensearchpy` 导致其逻辑无法被单测导入，
  已在显著性工具中记为 DEV-2。）
- 环境：一律用
  `C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`
  （managed python 无项目依赖）；PowerShell 编排 + 输出重定向临时文件；bash 残缺不可用。
- 测试进程须设 `CODEBUDDY_SAFE_DELETE_ENABLED=0`，避免 `.tmp_test/` 批量删除护栏误触发。
- 落盘默认遵循 spec §5.9 **方案 B**（哈希 + 本地缓存），`--allow-phi` 默认关闭。

## 已决前置决策（2026-10-07）

T3 起的真实链路任务受以下约束，**六项决策全部已定，不再是阻塞项**：

| 决策 | 结果 | 对实施的具体约束 |
|---|---|---|
| **D-1 · 判官模型** | **A：复用 `glm-5.3-flash`**（`.env` 既有 `MODEL_NAME`） | 判官适配器复用项目网关；`judge_model` 记录为 `glm-5.3-flash`。kappa 点估计 < 0.60 才换模型（换则重跑校准） |
| **D-2 · 门禁阈值** | 采纳 **0.90 / 0.95 / ≤25%**，**并新增「语料前提未满足 → 恒 `undecidable`」**（spec §5.6.4） | 阈值照写，但 P0-2/P0-4 未修前**不得据此判 `pass`/`fail`**；`undecidable` 须写明是哪条前提未满足 |
| **D-3 · PHI 落盘** | **B：哈希 + 本地缓存** | `eval_out/` 只存 `id`+`sha256`；正文进 gitignored 缓存；见下三条硬要求 |
| **D-4 · 零容忍范围** | **仅规则判官**：`judge == "rule" and code == "contradicted"` 必须 == 0 | 判据是 **`judge`（来源）不是 `kind`**；LLM 的 `contradicted` 只进 Wilson 区间；**并加空转防护**（见下） |
| **D-5 · kappa 门槛** | **0.70 不变，但强制报 95% 区间**；区间下界 < 0.60 → `undecidable` | `FaithfulnessCalibration.kappa_ci` 变**必填**；artifact 无 `kappa_ci` 视为无校准 |
| **D-6 · `required_evidence_ids`** | **本次不做**；触发条件 = **P0-2 换源 + P0-4 gold 真实化之后**，与 `gold_evidence_ids` 合并标注 | 报告**必须显式写明"完整性未测量"**，禁止让读者以为答案质量已全覆盖 |

### D-3 落地三条硬要求（spec §5.9.1，决策时补充发现）

1. **`answers[].text` 与 `evidence[].text` 必须同档脱敏** —— 原 S12 只覆盖后者，而答案会复述患者数值，只脱敏一项 = 没脱敏。
2. **`.gitignore` 当前未忽略 `eval_out/`**（实测 13 个文件明文在库）→ 须新增缓存目录条目，且不把"`eval_out/` 可入库"当既定事实。
3. **读缓存必须校验 `sha256`**，不匹配 → 该条 `judge_error`（新增不变量 **S13**），否则缓存漂移会静默失真。

### D-4 零容忍的两条实现要求（spec §5.6.5）

- **按来源划界**：只取 `judge == "rule"` 的 `contradicted`。按 `kind == "numeric"` 判会漏 ——
  若某条 `numeric` 被 LLM 判为 contradicted（规则判官弃权后转交），会被错误计入零容忍。
- **空转防护**：`numeric` 的**规则可判**样本（rule 的 entailed + contradicted 之和）**< 10** →
  零容忍状态记 **`样本不足`**，**不得记 `pass`**。否则"没有样本"会伪装成"零矛盾"（同 §4.3.1 原则）。

### D-1 附带风险（spec §5.7.0）

生成与判定同模型 → **self-preference bias**。不加改动，但校准时须按此解读：
kappa 偏低优先怀疑它；kappa 反常偏高（>0.85）优先怀疑标注被带偏。
**与 D-5 呼应**：若点估计偏高但区间很宽，先怀疑它或样本不足，勿直接采信点估计。

---

**对 spec §5.4 的一处细化（plan 决策，执行前声明）**：spec 给 `judge_llm` 的签名是
`gateway: ModelGateway`。plan 改为**窄 Protocol 接缝** `JudgeModel.complete(*, system_prompt, user_prompt) -> str`，
真实网关的适配器写在 `scripts/`。理由有二：①`ModelGateway.chat` 需要 `AccessContext` 与
`ConsentPurpose.EXTERNAL_MODEL`，在**单元测试**里伪造同意记录等于把 S1 的验证对象从链路里摘掉
（`casual_routing_live_acceptance.py` 已写明这一取舍）；②窄接缝让"D-1 换判官模型"只改一个适配器。

---

## Task 1: schema + 切分 + 规则判官（TDD，**零模型调用**）

### Step 1: 写失败测试 `tests/unit/test_faithfulness.py`

覆盖 spec §9 的 A1–A5、B1–B4、C1–C3、D5，以及 §6 的 S2/S3/S5/S6/S11。

关键断言：

```python
def test_decompose_splits_two_facts_in_one_sentence() -> None:
    """A1：一句含两个可独立证伪的事实 → 2 条，且逐句绑定 e1。"""
    claims = decompose("血红蛋白 105 g/L，低于参考区间 [e1]。", {"e1": "血红蛋白 105 g/L（参考区间 115-150）"})
    assert len(claims) == 2
    assert all(c.cited_ids == ("e1",) for c in claims)
    assert [c.kind for c in claims] == ["numeric", "trend"]

def test_disclaimer_sentence_produces_no_claim() -> None:
    """A2 + S2：免责句不进分母。"""
    assert decompose("请咨询医生。", {}) == ()

def test_structural_lines_produce_no_claim() -> None:
    """A3：Markdown 标题/列表是标签，不是断言。"""
    assert decompose("## 建议\n- 多喝水\n", {}) == ()

def test_pure_refusal_answer_has_none_value_not_one() -> None:
    """A4 + S3（本设计最易错处）：total==0 → None，绝不 1.0。"""
    result = evaluate_claims(decompose("请及时就医。", {}), {})
    assert result.total == 0
    assert result.value is None

def test_claim_ids_are_deterministic_and_readable() -> None:
    """A5 + S11：可重试的前提是同输入同 ID。"""
    text = "血红蛋白 105 g/L，低于参考区间 [e1]。"
    ev = {"e1": "血红蛋白 105 g/L（参考区间 115-150）"}
    assert [c.claim_id for c in decompose(text, ev)] == ["s0c0", "s0c1"]
    assert [c.claim_id for c in decompose(text, ev)] == ["s0c0", "s0c1"]

def test_rule_judge_marks_matching_number_entailed() -> None:
    """B1。"""
    claims = decompose("血红蛋白 105 g/L [e1]。", {"e1": "血红蛋白 105 g/L"})
    v = judge_rule(claims[0], {"e1": "血红蛋白 105 g/L"})
    assert v is not None and v.code == "entailed"

def test_rule_judge_flags_a_transposed_number_as_contradicted() -> None:
    """B2（本设计的核心价值：校验器放过的张冠李戴，忠实度能标出来）。"""
    v = judge_rule(decompose("血红蛋白 160 g/L [e1]。", {"e1": "血红蛋白 105 g/L"})[0],
                   {"e1": "血红蛋白 105 g/L"})
    assert v is not None and v.code == "contradicted"

def test_rule_judge_flags_unit_mismatch() -> None:
    """B3。"""
    v = judge_rule(decompose("血红蛋白 105 mg/dL [e1]。", {"e1": "血红蛋白 105 g/L"})[0],
                   {"e1": "血红蛋白 105 g/L"})
    assert v is not None and v.code == "contradicted"

def test_rule_judge_abstains_on_semantic_claims() -> None:
    """B4 + S5：规则判官无法判因果 → 必须弃权，不得猜。"""
    claims = decompose("这与缺铁有关 [e1]。", {"e1": "缺铁性贫血常见于育龄女性"})
    assert judge_rule(claims[0], {"e1": "缺铁性贫血常见于育龄女性"}) is None

def test_decompose_reuses_the_citation_validator_exemptions() -> None:
    """C1 + S6：防口径漂移（本仓库有 gate_skipped_reason 前例）。"""
    from medical_rag.chat.citations import CitationValidator
    from medical_rag.eval import faithfulness
    assert faithfulness._NON_CLAIM_SENTENCE is CitationValidator._NON_CLAIM_SENTENCE
    assert faithfulness._same_value is CitationValidator._same_value
```

**为什么必须钉住 `is` 而非"行为相似"**：若忠实度把豁免规则复制一份，两条路径会各自演化，
且**没有任何测试会失败**——直到有人发现"校验器拦了、忠实度没算"或反之。
`casual_screen.py:52` 与 `tests/unit/test_casual_output_screening.py:154` 已有同款 `is` 断言可照抄。

### Step 2: 运行测试，确认失败

```powershell
& $py -m pytest tests/unit/test_faithfulness.py -q
```

预期：`ModuleNotFoundError: No module named 'medical_rag.eval.faithfulness'`。

**这一步不可跳过**——否则无法证明测试有效。

### Step 3: 实现 `src/medical_rag/eval/faithfulness.py`

按 spec §4 的四个 dataclass + `decompose` / `judge_rule` / `evaluate_claims`。要点：

- **别名复用**（S6）：
  ```python
  _NON_CLAIM_SENTENCE = CitationValidator._NON_CLAIM_SENTENCE
  _same_value = CitationValidator._same_value
  _normalize_unit = CitationValidator._normalize_unit
  _QUANTITATIVE_JUDGEMENT = CitationValidator._QUANTITATIVE_JUDGEMENT   # kind 分类用
  _CITATION_PATTERN = CitationValidator.citation_pattern
  ```
  ⚠️ `_same_value` 是 `@classmethod`，取到的绑定方法可直接调用（`_same_value(a, b)`）。
- **切句一律走** `CitationValidator().sentences(text)`——不自己写切句正则（它已处理"纯标记片段归并前句"）。
- **逐句绑定**：`cited_ids` 只取**该句**的 `_CITATION_PATTERN.findall(sentence)`。
  **绝不**调用 `_sentences_with_evidence()`（它在无引用时用全部证据兜底，见 spec §5.1 的红线）。
- **`kind` 分类器**（spec §5.2）：数值 → `numeric`；`_QUANTITATIVE_JUDGEMENT` 命中但无数值 → `trend`；
  因果词 → `causal`；条件/适用词 → `applicability`；"是/指"定义式 → `definition`；兜底 `other`。
  **分类必须偏宽地判成医学断言**（宁可多算分母低估忠实度，不可把断言豁免出分母）。
- **`judge_rule` 返回 `ClaimVerdict | None`**：`None` = 弃权；只有 `kind == "numeric"` 才判。
  判据复用 `_same_value` + `_normalize_unit`，与 `_check_numeric_consistency` 同源。
- `total` / `value` 的口径按 spec §4.3 / §4.3.1：`total` **不含** `insufficient` 与 `judge_error`；
  `total == 0` → `value is None`。
- 每个函数 docstring 写清**公式来源**与**边界语义**（沿用本仓库风格：说明"为什么这样做"）。

### Step 4: 运行测试并确认既有不回归

```powershell
& $py -m pytest tests/unit/test_faithfulness.py tests/unit/test_citations.py tests/unit/test_casual_output_screening.py -q
```

**验收**：A1–A5 / B1–B4 / C1–C3 / D5 / S2 / S3 / S5 / S6 / S11 全部通过；既有引用与筛检测试**逐条不变**。

---

## Task 2: LLM 判官 + prompt（TDD，**假判官，零真实调用**）

### Step 1: 写失败测试（追加到 `tests/unit/test_faithfulness.py`）

覆盖 spec §9 的 B5–B7、D1，以及 §6 的 S1/S4/S8/S11。

```python
class _FakeJudge:
    """记录收到的 payload，供 S1 断言"未绑定证据没被传出去"。"""
    def __init__(self, reply: str) -> None:
        self.reply, self.calls = reply, []
    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.reply

def test_judge_payload_excludes_unbound_evidence() -> None:
    """S1（安全承重）：该句只引 e1 时，e2 的正文**不得**出现在判官输入里。"""
    judge = _FakeJudge('{"verdict":"entailed","reason":"ok"}')
    claim = decompose("血红蛋白 105 g/L [e1]。",
                      {"e1": "血红蛋白 105 g/L", "e2": "SECRET-E2-NOT-BOUND"})[0]
    judge_llm(claim, {"e1": "血红蛋白 105 g/L", "e2": "SECRET-E2-NOT-BOUND"},
              judge_model=judge, prompt_version="v1")
    assert "SECRET-E2-NOT-BOUND" not in judge.calls[0][1]

def test_judge_parses_fenced_json() -> None:
    """B5：模型高频输出 ```json 围栏。"""
    judge = _FakeJudge('```json\n{"verdict":"not_entailed","reason":"未提及"}\n```')
    v = judge_llm(_claim(), {"e1": "x"}, judge_model=judge, prompt_version="v1")
    assert v.code == "not_entailed"

def test_judge_rejects_out_of_whitelist_verdict_after_retries() -> None:
    """B6：非法值重试后 → judge_error，**不得**静默当成 not_entailed。"""
    judge = _FakeJudge('{"verdict":"maybe"}')
    v = judge_llm(_claim(), {"e1": "x"}, judge_model=judge, prompt_version="v1", max_attempts=2)
    assert v.code == "judge_error"
    assert len(judge.calls) == 2

def test_judge_raises_when_the_backend_is_unreachable() -> None:
    """B7 + S8（铁律 4）：不可达必须抛错终止，不产出"全绿"。"""
    class _Dead:
        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            raise ConnectionError("gateway down")
    with pytest.raises(RuntimeError):
        judge_llm(_claim(), {"e1": "x"}, judge_model=_Dead(), prompt_version="v1")

def test_abstention_is_not_counted_as_supported() -> None:
    """S4：全弃权 → value is None，且 abstained == n。"""
    ...
```

**为什么"网络错误"与"输出非法"必须走两条不同的路**：输出非法是**单条样本的故障**（重试后可记
`judge_error`，不影响其他条）；后端不可达是**整轮故障**（继续跑只会让忠实度趋近 0，且看起来像
"模型幻觉很多"）。后者必须抛错终止，对齐方法论文档 §2.1 铁律 4 与
`chat_e2e_acceptance.py:161` 的既有做法。

### Step 2: 运行测试，确认失败

```powershell
& $py -m pytest tests/unit/test_faithfulness.py -q
```

预期：`ImportError: cannot import name 'judge_llm'`。

### Step 3: 实现

**`src/medical_rag/eval/judge_prompt.py`**（prompt 与版本号**同文件**，防"改了 prompt 忘了动版本"）：

```python
PROMPT_VERSION = "faithfulness-judge-v1"

JUDGE_SYSTEM_PROMPT = """你是医学文本的事实一致性判定器。...
只依据给定证据判断给定陈述是否被支持。不要用你自己的医学知识补充。
不要因为陈述"在医学上普遍成立"就判为支持——只问证据是否支持它。
输出严格 JSON，不要任何其他文字：{"verdict": "...", "reason": "..."}
verdict 只能取四个值之一：entailed / not_entailed / contradicted / insufficient
...
证据不足或陈述含混、无法判定时，必须选 insufficient，不要猜测。"""

def build_judge_user_prompt(claim_text: str, evidence_texts: Sequence[str]) -> str: ...
```

**`src/medical_rag/eval/faithfulness.py` 追加**：

- `JudgeModel` Protocol：`complete(*, system_prompt: str, user_prompt: str) -> str`。
- `judge_llm(claim, evidence_texts, *, judge_model, prompt_version, max_attempts=2) -> ClaimVerdict`：
  - **内部按 `claim.cited_ids` 过滤证据**（不信任调用方）→ S1 可被单测穷尽；
  - 剥离 Markdown 围栏（```json ... ```）后 `json.loads`；
  - `verdict` 不在白名单 → 重试；耗尽 → `judge_error`；
  - 后端抛异常 → **立即**抛 `RuntimeError`（不重试到耗尽，因为整轮已不可用）；
  - `judge` 字段写 `f"llm:{model}@{prompt_version}"`（可追溯；模型名由调用方注入）。
- `evaluate_claims(claims, evidence_texts, *, judge_model=None, prompt_version=...) -> FaithfulnessResult`：
  无 `judge_model` 时只用规则判官（纯离线可跑，Task 1 的 A4 依赖此路径）。

### Step 4: 运行测试并确认既有不回归

```powershell
& $py -m pytest tests/unit/test_faithfulness.py tests/unit/test_citations.py -q
```

**验收**：B5–B7 / D1 / S1 / S4 / S8 通过；`judge_prompt.PROMPT_VERSION` 存在且非空。

---

## Task 3: 真实链路答案生成 `scripts/faithfulness_answers.py`

### Step 1: 写脚本（照抄 `casual_routing_live_acceptance.py` 的骨架）

必须逐条复用其已知正确的环境处置（**不可自行简化**）：

| 复用项 | 来源 | 为什么 |
|---|---|---|
| 开头 pop `*_PROXY` + `NO_PROXY=*` | 该脚本 L34-36 | 否则 OpenSearch 假阴性 |
| `sys.path` 插 `src` 与 `scripts` | 同上 | 复用 `retrieval_tuning_lab` 的索引常量与 embedding 工厂 |
| 真 `ModelGateway`（**不用裸 adapter**） | 同上 `_build_gateway` | 裸 adapter 绕过同意校验与 `EXTERNAL_MODEL_CALL` 审计 |
| 独立临时库 `tempfile.mkdtemp()` | 同上 | 生成会写 Tenant/Patient/Consent/AuditEvent，绝不污染 `medical_rag.db` |
| `total_timeout_seconds = settings.model_total_timeout_seconds` | 同上 | 默认 60s 会让整轮降级（真实模型 18–99s） |
| OpenSearch 不可达即抛错 | `chat_e2e_acceptance.py:161` | 铁律 4：不静默跳过 |
| 标定 artifact 缺失即抛错 | 同上 `_load_artifact` | 缺门禁会得到不同分布的证据 |

### Step 2: 真跑生成，产出答案集

```powershell
& $py scripts/faithfulness_answers.py --questions eval/faithfulness_questions.jsonl --limit 20
```

问题集：从既有 `evalset` 的 quality 用例取 20 条（**含 gold 的真实索引 ID 坐标**）。
落盘 `eval_out/faithfulness_answers.json`，schema 见 spec §5.8。

**验收**：文件中 `answers` 长度 = 20，且至少 15 条 `status == "answered"`；
每条 `answered` 的 `cited_evidence` 非空、`evidence` 覆盖其全部被引 ID。

> ⚠️ 真实模型非确定性（18–99 s）→ 本步骤耗时可达 30+ 分钟。**不设通过率断言**，
> 只记录 `answered` 比例（对齐 P0-5 的验收口径：比例 ≥80%）。

### Step 2b: 落盘脱敏（D-3 = B，**三条硬要求**）

默认模式（`--allow-phi` 关闭）下必须满足：

1. **`evidence[].text` 与 `answers[].text` 二者都不落盘**（`answers[].text` 是 D-3 决策时发现的缺漏，
   答案会复述患者数值 → 只脱敏证据 = 没脱敏）；
2. 二者改为存 `id` + `sha256`，正文写入 **本地 gitignored 缓存目录**（并在 `.gitignore` 新增该条目）；
3. 缓存读取处**校验 `sha256`**，不匹配 → 该条 `judge_error`（**S13**）。

自检命令（无正文才算通过）：

```powershell
& $py -c "import json;d=json.load(open('eval_out/faithfulness_answers.json',encoding='utf-8'));a=d['answers'][0];print('keys',sorted(a.keys()));print('has_evidence_text','text' in (a.get('evidence') or [{}])[0]);print('has_answer_text','text' in a)"
```

**验收**：`has_evidence_text=False` 且 `has_answer_text=False`，同时缓存目录存在于 `.gitignore`。

### Step 3: 确认生成未污染生产库

```powershell
& $py -c "import os;print(os.path.getmtime('medical_rag.db'))"
```

**验收**：mtime 早于本次运行（S7）。若变化 → 立即停止并排查（说明脚本误用了 `.env` 的库）。

---

## Task 4: 判定编排 `scripts/faithfulness_eval.py`

### Step 1: 写脚本（含真实判官适配器）

- `_GatewayJudgeModel`：把 `JudgeModel.complete` 接到 `ModelGateway.chat`。
  `judge_model` 取 `.env` 既有 `MODEL_NAME`（**D-1 = A：`glm-5.3-flash`**），并**原样写入**校准产物与报告。
  **必须与生成侧同样走真网关**（同意 + 白名单 + 审计），用独立临时库与合成租户。
  `purpose="faithfulness_judge"` 使审计可分辨（对齐 `casual_chat` 的既有做法）。
- `--source replay`（默认）读 `eval_out/faithfulness_answers.json`；`--source live` 内置生成（委托 Task 3）。
  **replay 模式下正文从本地缓存读，读前校验 `sha256`**（D-3 = B；不匹配 → `judge_error`，S13）。
- 逐答案调 `evaluate_claims(..., judge_model=..., prompt_version=judge_prompt.PROMPT_VERSION)`。
- 汇总：答案级 `mean_ci`（**连续 → t**）、claim 级 `wilson_interval`（**二值 → Wilson**）、
  矛盾率、弃权率 —— **四者方法名与 n 必须一并打印**（对齐显著性工具的既有要求）。
- **零容忍计数（D-4）**：只统计 `judge == "rule"` 的 `contradicted`；**另用 `llm_contradicted` 记录 LLM 来源的
  contradicted 走 Wilson 区间**。两者**分列、禁止合并**。可判样本 < 10 → 记 `样本不足`（不记 pass）。
- 三态判定按 spec §5.6.2：`n < 30` ∨ 区间跨阈值 ∨ 弃权率 > 25% ∨ 校准 artifact 缺失/mismatch
  **∨ 语料前提未满足（§5.6.4，须写明是哪一条）** → `undecidable`。
- 落盘 `eval_out/faithfulness_eval.json`。

### Step 2: 真跑判定

```powershell
& $py scripts/faithfulness_eval.py --source replay
```

**验收**：产出 JSON 含逐 claim 判定、区间、方法与 n、三态；`judge_error` 比例 ≤10%
（超 10% 按 spec §7 整轮标 `undecidable`）。

### Step 3: 断言"无校准 artifact → undecidable"

```powershell
& $py scripts/faithfulness_eval.py --source replay --calibration path/to/nonexistent.json
```

**验收**：三态 = `undecidable`，且**数值照常输出**（S9：测量有效、门禁无效）。

---

## Task 5: kappa 校准 `scripts/faithfulness_calibrate.py`

> ⚠️ **本任务有硬人工依赖**（spec §8）：需人工标注 ≥60 条陈述（6 类 `kind` × 每格 ≥5）。
> 未完成标注前，本任务**不得**产出"可门禁"的 artifact——`FaithfulnessCalibration` 不生成，
> Task 4 保持 `undecidable`。这是**设计的预期行为**，不是未完成项。

### Step 1: 写脚本

- `--sample`：从 `faithfulness_eval.json` 分层抽样（按 `kind`），导出待标注 CSV
  —— 列**只含** `claim_id` / `claim_text` / `evidence_texts`。
  **必须与判官输入逐字节同构**：多给一个字段（如所属问题、其他证据）都会让 kappa 失去意义。
- `--score`：读回人工标注，跑 judge，算 **Cohen's kappa（多分类）**：
  `kappa = (p_o − p_e) / (1 − p_e)`，输出混淆矩阵。
  弃权（`insufficient`）在 kappa 里**作为一个正常类别**参与（不是缺失值）——判官"says 不知道"
  与人工"says 不知道"一致本身就是一致性。
- 🔴 **必须同时算 95% 区间**（D-5 已决）：kappa 近似正态 → 用 `mean_ci` 的 t/正态口径。
  **只报点估计不合格。** n < 30 时只报区间、不判定达标（沿用既有规则）。
- 达标判据（D-5）：**点估计 ≥ 0.70 且区间下界 ≥ 0.60** 才写 artifact；
  `kappa_ci` 是 **`FaithfulnessCalibration` 的必填字段**（spec §5.7.2）。
- 写 `config/eval/faithfulness_calibration-<date>.json`，字段见 spec §5.7.2。

### Step 2: 校准核算（需人工完成后执行）

```powershell
& $py scripts/faithfulness_calibrate.py --sample --out eval_out/faithfulness_labels.csv
# （人工标注 CSV）
& $py scripts/faithfulness_calibrate.py --score --labels eval_out/faithfulness_labels.csv
```

**验收**：输出 **kappa 点估计 + 95% 区间 + 混淆矩阵**；达标（点估计 ≥0.70 且下界 ≥0.60）时产出
artifact，否则**不产出**并打印"判官无资格门禁（kappa 点估计 < 0.70 或区间下界 < 0.60）"。

### Step 3: 断言版本 mismatch → undecidable

把 artifact 的 `judge_prompt_version` 改成旧值，重跑 Task 4 → 三态必须 `undecidable`。

### Step 4: 断言 `kappa_ci` 缺失 → undecidable（S17）

删掉 artifact 的 `kappa_ci` 再重跑 Task 4 → 三态必须 `undecidable`（旧格式 artifact 视同无校准）。

**验收**：S9 的第二条分支（mismatch，非仅缺失）成立。

---

## Task 6: 报告接入 + 文档

### Step 1: 新增报告节 `docs/superpowers/notes/2026-10-06-faithfulness-eval-report.md`

至少含：答案级忠实度（t 区间 + n + 方法）、claim 级（Wilson）、**按 `kind` 分层**的忠实度、
**零容忍项与 `llm_contradicted` 分列**（D-4，禁止合并）、弃权率、三态判定、以及 spec §11 R7 要求的**并列呈现**
（忠实度必须与拒答正确率、答案相关性同屏，**禁止**单指标下结论）。

🔴 三条**硬性报告义务**（由本轮决策派生，缺任一即视为报告不合格）：

1. **`undecidable` 必须写明原因行**：是哪一条前提未满足（`n<30` / 区间跨阈值 / 弃权率 / 校准缺失或 mismatch
   / **语料前提 P0-2 或 P0-4**）。否则会被误读为"功能未完成"。
2. **必须显式写明"完整性未测量"**（D-6 已决不做），否则会让人误以为答案质量已全面覆盖。
3. **高危类型（`causal`/`numeric`）≥0.95 在 n 小时恒为 `undecidable`** —— 报告必须如实标注它是
   **长期目标而非当前门禁**，不得呈现为 `fail`。

### Step 2: 更新 `docs/superpowers/notes/2026-10-06-rag-eval-rigor-guide.md`

- §7 执行清单新增一行（忠实度 harness 已实施）；
- 附录 A 的「生成 · 忠实度 / 幻觉率」一行标注落地物路径。

### Step 3: 全量回归

```powershell
$env:CODEBUDDY_SAFE_DELETE_ENABLED="0"; & $py -m pytest tests/ -q
& $py -m ruff check src/medical_rag/eval/faithfulness.py src/medical_rag/eval/judge_prompt.py tests/unit/test_faithfulness.py
```

**验收**：全量通过数 ≥ 976（不减少）；ruff clean；`test_declared_dependencies.py` 通过（D5）。

---

## Plan Self-Review

| 检查项 | 结论 |
|---|---|
| 每个任务可独立验收？ | ✅ T1/T2 靠单测；T3 靠答案集产出；T4 靠判定 JSON + 三态；T5 靠 kappa；T6 靠报告 |
| TDD 顺序完整？ | ✅ 每个 Task 都有"写失败测试 → 确认真失败 → 实现 → 验证"四步 |
| 是否改动既有行为？ | ✅ 否。`CitationValidator` 只被**读取**（`is` 别名），生产路径零改动 |
| 是否引入新依赖？ | ✅ 否。纯标准库 |
| 是否有隐藏 I/O？ | ✅ T1/T2 零 I/O 零联网（判官为注入的假对象）；T3/T4 的真 I/O 全在 `scripts/` |
| 安全不变量可穷尽验证？ | ✅ S1 由"过滤在函数内部 + 假判官记录 payload"穷尽；S3/S4/S6/S9 均有直接单测 |
| 是否滥用零容忍？ | ✅ 零容忍仅限**规则判官**给出的 `contradicted`（判据是 `judge` 来源，非 `kind`）；LLM 判定的只进区间（spec §5.6.5） |
| 是否把"测不出"当"没问题"？ | ✅ `total==0 → None`（S3）、弃权率门禁、无 artifact → undecidable（S9）、**零容忍空转防护（S15）**、**语料前提未满足 → undecidable（S16）** |
| 已知缺口是否显式记录？ | ✅ spec §11 R1–R7（R7 已补"必须写明完整性未测量"）；T5 硬人工依赖已前置声明 |
| 是否留下待决策项？ | ✅ spec §10 **D-1~D-6 已全部定案（2026-10-07）**，无遗留决策；**T1/T2 不依赖任何决策项** |

**依赖与风险**：

- **T1/T2 已完成，38 单测绿。**
- ✅ **T3/T4 阻塞已解除**：**D-1~D-6 六项全部定案**（见"已决前置决策"）。
  真跑时须满足 D-3 三条硬要求、D-2 语料前提门禁、D-4 零容忍按来源分列。
- **T5 仍阻塞于人工标注**（≥60 条陈述）。**在标注完成前 `faithfulness_eval.json` 恒为 `undecidable`** ——
  需在 T6 报告中显式标注该状态，避免被误读为"功能未完成"。**另需人工产出 kappa 的 95% 区间**（D-5）。
- 语料仅 12 chunk（P0-2）：`not_entailed` 中的"证据未提"型会被高估。报告必须按 `kind` 分层并
  显式标注该偏差来源，不得只报总均值。**且 P0-2/P0-4 未修前，三态恒 `undecidable`**（D-2 / §5.6.4）——
  即**当前阶段忠实度只报告、不判达标**。

---

## 实施偏差记录（2026-10-07，T3–T5）

### DEV-T3-1 🔴 `AnswerResponse` 不暴露「被引证据 ID → 正文」（plan 未预见）

Plan Task 3 Step 1 只说"复用 casual 脚本骨架"，但**未说明如何拿回被引证据**。
实测：`AnswerResponse.citations` 是 `Citation`，其 `source_id` 是**源文档 ID**
（`PrivateIndexRecord.document_id`），**不是证据 UUID**；`cited_evidence` 也**不**出现在响应里
（它只写进 `ChatMessage.metadata_json`，需查库）。忠实度的判定对象是"该句绑定的**证据正文**"，
因此必须另找接缝。

**解法**：`_CapturingGateway(ModelGateway)` 子类，在 `chat()` 成功后捕获
`request.evidence` —— 这正是 `orchestrator` 交给 `CitationValidator` 的同一份
`evidence_for_model`，故与答案里的 `[id]` 严格同源。子类化（而非裸 adapter）保住
同意校验 / 白名单 / 审计。**生产代码零改动。**

### DEV-T3-2 🔴 `_has_private_documents` 查的是**数据库表**，不是 OpenSearch

`orchestrator._has_private_documents` 用 `Document` 表判断"该患者有无私有资料"。
临时库若**没有**该患者的 `READY` 文档，编排器会认为"没有私有资料"→ 私有问题的检索
范围被**静默关闭**，只会拿到公共证据（private 问题会退化成"证据不足"）。
**解法**：T3 必须为验收患者造一条合成 `Document`（`storage_key` 有唯一约束 → 用随机名）。
这是一个**纯环境依赖**，不修则 T3 的私有用例全部失真且**不会报错**（静默）。

### DEV-T3-3 已跑生成时观察到的事实

- **B 档脱敏核实通过**：默认模式下答案集**既无** `evidence[].text` **也无** `answers[].text`
  （改存 `text_sha256` / `sha256`）；正文进 `.faithfulness_phi_cache/`（`git check-ignore`
  确认第 47 行命中）。**S12 成立。**
- 单条真实延迟实测 **24.7s**（与 P0-5 记录的 18–99s 区间一致）。

### DEV-T4-1 `--no-judge` 离线口径演练结果（真实数据）

用冒烟答案跑 `--source replay --no-judge`：8 条 claim 中 **1 entailed / 7 insufficient**。
这是**预期行为**：规则判官只判 `numeric`，其余一律弃权。同时验证了三态与两个降级触发：

- `verdict_state = undecidable`；原因行同时列出 `n<30`、弃权率、**两条语料前提**、
  **零容忍空转防护**（规则可判样本 1 < 10 → `样本不足`）。**S15/S16 成立。**
- 区间方法名与 n 均已打印（`wilson | n=8`），符合显著性工具既有要求。

### DEV-T4-2 `faithfulness_eval.py` 被判官接入的**真网关**要求

判官若走真网关，同样需要 `AccessContext` + `EXTERNAL_MODEL` 同意 → T4 也须造
独立临时库与合成租户（`purpose="faithfulness_judge"` 使审计可分辨）。
`--no-judge` 时**不建库**（纯离线，零 I/O）。

### DEV-T5-1 kappa 区间是**近似**，必须公开标注（不假装精确）

Plan Task 5 Step 1 只写"用 `mean_ci` 的 t/正态口径"。但 **kappa 不是简单均值**，
其精确方差需 **Fleiss 公式**（含类别边际的多项结构）。本次实现用"一致性比例的
**二项标准误** ÷ (1 − p_e)"作**保守近似**，并在 artifact `notes` 与报告里显式标注
"非 Fleiss 精确方差，仅用于暴露'点估计达标但区间跨 0.60'的风险"。
**宁可公开方法学局限，不可把它包装成精确区间**（否则就是"用噪声门禁"的另一种形式）。

### DEV-T5-2 `--score` 不重放模型，直接对齐 T4 已产出的判官码

Plan 未指定评分的数据来源。若按字面"跑 judge_llm"，会把**同一批 claim 再喂一遍真模型**
（昂贵且引入新的非确定性）。本实现改为：**判官码取 `faithfulness_eval.json`
（由 T4 用真网关产出）**，`--score` 只做人工 vs 判官的一致性核算。
`judge_error` 与 `no_evidence_bound` 的 claim **不进 kappa**：前者是工程故障、
后者是结构性判定，都**不代表判官的语义判断能力**。

### DEV-T5-3 抽样脚本发现的真实事实

冒烟答案（1 条）切出 8 条 claim：`other=5 / numeric=1 / trend=1 / applicability=1`。
即 **`other` 占多数** —— 提示领域语料贫瘠时 claim 类型分布会明显偏向 `other`。
这正是 P0-2 的具体表现，T6 报告须如实反映（不可只报总均值）。

### DEV-T3-4 🔴 T3 首跑 `answered=8/20` 的**真实归因**（原写"待查"，现已定位）

首个完整生成（`--limit 20`，13m59s，exit 0）产出 **8 answered / 12 degraded / 0 failures**，
degraded 统一文案为 `回答未通过医学证据和引用校验`（`orchestrator.py` L816-820 的
`UnsupportedClaim` 分支）。**该文案吞掉了具体违规原因**，故必须**在网关处捕获校验前的裸草稿**、
逐项重放 `CitationValidator` 才能定位。本记录即该重放（`.diag_why.py`）的结论。

**先排除的假设**：
- ❌ **不是私有库依赖问题**：12 条 degraded 中 public 占 7 条（`pub-stop-meds`/`pub-missed-dose`/
  `pub-weight-loss`/`pub-home-bp-value`/`pub-salt-why`/`pub-meds-regular`），与 DEV-T3-2 的
  `Document` 依赖无关。
- ❌ **不是引用格式问题**：裸草稿的引用均为合法的 `[<uuid>]`（无 `evidence:` 前缀），
  `cited_ids` 非空且全部落在候选集内（`citation_meta=ok`）。

**定位到的四类真实成因**（逐条均有裸草稿+重放为证）：

| 类别 | 案例 | 触发的检查 | 机制 |
|---|---|---|---|
| **A 表格证据误伤** | `priv-hemoglobin`、`priv-red-cell` | `numeric_consistency` + `quantitative_claims` | 见下（本轮最关键） |
| **B 合法弃权句未被词表覆盖** | `pub-missed-dose` | `sentence_citations` | `'…我无法可靠回答。'`（无句末引用）不在 `_DISCLAIMER_SENTENCE` 词表 → 被误判为"未标注的医学断言" |
| **C 列举引导句超出有界窗口** | `pub-meds-regular` | `sentence_citations` | `'药物治疗之外，以下生活方式措施也有助于血压控制：'` 距"以下"14 字 > `_ENUMERATION_LEAD_IN` 的 `{0,10}` 界 |
| **D 前置结论句缺句末引用** | `priv-red-cell` | `sentence_citations` | `'是的，您本次报告中的红细胞计数偏低。'` 是断言句却无 `[id]` |

另有 `pub-stop-meds`(123.3s)、`pub-salt-why`(120.1s) 命中 **120s 请求超时**；
且 `pub-weight-loss`、`pub-home-bp-value` 在**本次重放中变为 answered** —— 证实存在
**采样非确定性**（`http_adapters.py` 的 chat payload 只发 model/messages/max_completion_tokens，
**不含 temperature/top_p/seed**，即网关默认温度）。

**A 类（表格证据误伤）是"压垮骆驼"的主因，机理已精确复现**：

- **证据是"列对齐"的血常规表**：`'血红蛋白          105    g/L    130-175'`。
- 关键：`_NUMBER_WITH_UNIT = r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[A-Za-zµμ%]…)"`
  里 `unit` 组**是必需的（非可选）** —— 正则**要求数值紧邻一个字母/单位记号**，否则**整条不匹配**。
  因此 `_numbers()` 实际只返回"**带相邻单位**"的数值，孤立数值被**整条丢弃**。
- 表格证据里各数值的后继字符各不相同：
  - `105` 后接 `g/L` → 抽到 `(105, 'g/L')`（命中的唯一一个）；
  - `130-175` 里 `130` 后是 `-`、`175` 后是换行/空格 → **非字母 → 两条都不匹配 → 被丢弃**；
  - `3.85 × 10^12/L` 里 `3.85` 后是空格+`×` → 非字母 → 丢弃；`4.3-5.8` 同理。
- 于是该证据的 `observed` **只有 `[(105, 'g/L')]`**（探针实证）。
- 而模型在**自有行文**里把参考范围写成 `'130-175 g/L'` / `'105 g/L…130–175 g/L'` ——
  这里 `175` **紧邻 ` g/L`**，于是被抽成 `(175, 'g/L')`。两套抽取口径不一致 → 判为幻觉。
- 逐句探针（`.probe_num2.py`）**精确复现**：对 `priv-hemoglobin`，
  `claimed=[(105,'g/L'),(175,'g/L')]`，`observed=[(105,'g/L')]`，`MISSING=[(175,'g/L')]`；
  对 `priv-red-cell` 同型（missing 同为 `(175,'g/L')`）。
  **即：模型忠实复述了参考范围，却因"证据侧只抽到带单位数值 / 回答侧把范围写成范围+单位"
  的口径差异被规则判为数值幻觉。**

**结论**：A 类不是模型的错，而是 `CitationValidator` 对**表格型医学证据**的**假阳性**——
它假设证据是自然语言散文（数值与单位相邻），而真实检验报告是**列对齐表**（范围端点后接连字符/换行，
单位另列）。这既解释了 12/20 的多数，也是**必须回填给主线**的发现（见下）。

**处理决定（不在本 harness 内改生产代码）**：
1. **不修改 `CitationValidator`**（plan Task 1 硬约束：既有成员与行为零改动；本设计只是它的
   第二个消费者）。A 类缺陷登记为**独立缺陷项**，移交主线的引用校验修复排期。
2. **T3 验收口径按 plan L317-318 的注记执行**：plan Step 2 正文写"≥15 answered"，
   但紧随其后的注记写明"**不设通过率断言**，只记录 `answered` 比例（对齐 P0-5 口径：比例 ≥80%）"。
   二者冲突，**以注记为准**（它显式声明了本条验收的性质）。当前 8/20=40% 记为
   **未达 80%**，**构成实施偏差**，原因即上述 A–D 四类（其中 A 类为环境/校验器缺陷，非 harness 缺陷）。
3. **重试一次的正当性**：因存在采样非确定性（D 类及其他），同一问题集重跑可提高 `answered`。
   但由于 **A 类是确定性的表格证据误伤**（与采样无关，`priv-*` 5 条必然复发），重跑**无法**
   把 answered 抬到 16/20 以上而不换问题集。故本 harness **不通过换问题集来"刷"达标率**
   （那会掩盖真实的校验器缺陷）。**如实报告 8/20 + 四类归因**才是正确交付。
4. **给主线的最小建议（不改代码，仅登记）**：`_NUMBER_WITH_UNIT` 对**表格/空格分列**证据
   会漏抽范围上限值与单位；修法方向是"证据侧数值抽取"与"回答侧数值抽取"**分别建模**
   （证据侧需容忍 `值 … 单位` 跨列），或在数值核对里对**同句的区间端点**放宽为"只要区间整体
   出现在证据文本中即可"。此项**须单独 spec + 单测**，不得在忠实度 harness 内顺手改。

### DEV-T4-3 ✅ T4 真判官整轮验收通过（2026-10-07，真网关 glm-5.3-flash）

`faithfulness_eval.py --source replay`（真判官，66 claims，**4m31s / exit 0**）与
`--no-judge`（规则判官）**双档均通过**。同一份答案集的两档对照，直接量化了"规则判官的覆盖率局限"：

| 指标 | `--no-judge`（规则） | 真判官（GLM） |
|---|---|---|
| 答案级忠实度 t 区间 | `[0, 0.0848] | n=8` | `[0.7564, 1.0000] | n=8` |
| claim 级 Wilson | `[0.0238, 0.1457] | n=66` | `[0.8155, 0.9577] | n=66` |
| 弃权率 Wilson | `[0.8543, 0.9762]` | `[0.0238, 0.1457]` |
| 零容忍 | rule=0 / llm=0 / `样本不足` | **rule=0 / llm=1** / `样本不足` |

**关键解读（必须写进 T6 报告，防止误读）**：
1. **14.6%→81.6% 不是"答案质量变化"**，而是**规则判官只看 `numeric`、对其余 94% 一律 `insufficient`**
   造成的**系统性低估**。规则档的 66 claims 里 62 条 `insufficient`、仅 4 条可判。
   → **规则档的忠实度数值不可单独解读**，只能作为"规则覆盖范围内"的下界。
2. 真判官 66 claims = 60 `entailed` / 4 `insufficient` / 1 `not_entailed` / 1 `contradicted`；
   `by_kind` 五类齐全（`other=46` 仍占多数 → P0-2 语料贫瘠的印记）。
3. **S14 实测成立**：`contradicted=1` 全部计入 `llm_contradicted`，`rule_contradicted=0`
   → 零容忍**不**被这条 LLM 判定触发（D-4 按来源分列已被真实数据验证）。
4. **S15/S16 实测成立**：`state=样本不足`（规则可判 4<10）；`verdict_state=undecidable`
   且 6 条原因（n<30 / 答案级 t 跨 0.90 / **校准 artifact 缺失** / P0-2 / P0-4 / 空转防护）。
   → 校准 artifact 未产出时的门禁失效行为（S9）也在真跑中一并覆盖。
5. 产物 `eval_out/faithfulness_eval.json`（真判官）；另存 `faithfulness_eval.offline.json`
   （规则档）以便对照。**判官 `judge_error=0`（≤10% 达标）。**
