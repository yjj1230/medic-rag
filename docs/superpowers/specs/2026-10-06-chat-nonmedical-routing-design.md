# 非医学问题分流（受限生成 + 双闸）— 设计文档

- 受理日期：2026-10-06
- 提出人：用户
- 原始诉求（原话，不改写）：
  > 会话分流这一块是不是可以用jev模型？如果是非医学的问题 就由llm来直接回答
- 方案选择（用户确认）：**B（受限生成 + 双闸）**
  > 在方案 A（固定模板回复）与方案 B（受限 LLM 生成 + 双重安全闸）之间，用户选择 B。
- 状态：**待批准**（批准前不写生产代码，见 FEATURE-WORKFLOW 规则 3）

---

## 1. 背景

### 1.1 现状

患者端问答目前是**单一路径**：任何通过安全闸（`SafetyService.classify` → `ALLOW_RETRIEVAL`）的输入，
一律走检索增强生成（RAG）：

```
POST /v1/chat/answers
  └─ SafetyService.classify            ← 第一道闸（既有）
       ├─ URGENT              → urgent_escalation
       ├─ MEDICATION_CHANGE   → safe_template
       ├─ DIAGNOSIS_REQUEST   → safe_template
       └─ ALLOW_RETRIEVAL     → QueryPipeline.process（意图分类）
                                  └─ 检索 → 引用校验 → 落库 → answered
```

### 1.2 痛点（实测证据）

**非医学问题被硬塞进医学检索链路，返回"没有医学证据"。**

对 `chat_messages` 真实落库记录与 `EXTERNAL_MODEL_CALL` 审计的盘点（真实 DB，一次性探针，输出见下）：

```
EXTERNAL_MODEL_CALL rows: 63
PURPOSE:  health_education 45 | intent_classify 18
ANSWER_DELIVERED: 15
ASSISTANT INTENT (persisted): lab_indicator 6 | health_education 5 | report_explanation 4
```

**关键观察：`ordinary` 意图的回答从未产生过一条。** 也就是说，一句「你好」「谢谢」当前必然走到
检索 →（公共库只有 5 条高血压样例，P0-2）→ `insufficient_evidence` → 回一句
「当前没有足够且可定位的医学证据」。这对用户是**明显的错误反馈**。

### 1.3 为什么现在做

- `intent_llm_classify_enabled` 默认为 `True`，LLM 意图兜底**已上线且在真实链路中被调用**
  （`intent_classify` 18 次真实外呼），分流所需的分类基础设施**已经存在**，增量成本低。
- 会诊分流（医学 / 非医学）是"拒答策略"与"引用门禁"之后的最后一块**对话层**拼图：
  当前系统对"不相关医学问题"已能拒答（余弦门禁），但对"根本不是医学问题"仍无正确出口。

### 1.4 承重事实核验（写 spec 前已实测，避免把设计建在错误假设上）

探针直接调用既有实现（不写生产代码），**可复现证据**：
`docs/superpowers/notes/evidence/2026-10-06-casual-routing-fact-probe.py` / `.txt`。实际输出：

| 输入 | SafetyService | 意图 | 置信度 | 备注 |
|---|---|---|---|---|
| `你好` / `在吗` / `谢谢` / `1+1等于几` / `给我讲个笑话` | allow_retrieval | **ordinary** | 0.5 | 规则未命中 |
| `今天天气怎么样` | allow_retrieval | **health_education** | 0.75 | ⚠️ **规则缺陷**：`怎么` 命中 `_EDUCATION`，天气被当成医学科普 |
| `血红蛋白是什么` | allow_retrieval | **ordinary** | 0.5 | 🔴 **红线样本**：真实医学问题，正则未命中 |
| `自杀` / `我想自杀` | urgent_escalation | ordinary | 0.5 | 第一人称症状，不豁免 |
| `网上说自杀怎么回事` | allow_retrieval | health_education | 0.75 | 被 `is_general_knowledge_enquiry` 豁免升级 |
| `我停药了可以吗` | **safe_template** | ordinary | 0.5 | 用药变更闸先行 |
| `帮我看看我的报告` | allow_retrieval | report_explanation | 1.0 | |
| `高血压怎么预防` | allow_retrieval | health_education | 0.75 | |

### 1.5 🔴 红线：`ORDINARY ≠ 非医学`

上表第 3 行是整个设计的**承重事实**：

> **`Intent.ORDINARY` 的语义是"没有正则命中"，不是"与医学无关"。**
> 「血红蛋白是什么」是一个**货真价实的医学问题**，却与「你好」同样落在 `ORDINARY`。

因此，**最朴素的方案——"把 ORDINARY 路由到 LLM 直接回答"——会制造一个严重的安全缺陷**：
真实医学问题被判为非医学 → 由 LLM 自由生成医学内容 → **完全绕过 `CitationValidator`**
（引用存在性、句末锚定、数值一致性、高危断言拦截全部失效）→ 患者收到**无任何证据支撑、
且未经校验**的医学回答。

**这个不对称性决定了整个设计取向**：

| 误判方向 | 后果 | 严重性 |
|---|---|---|
| 医学问题 → 当成闲聊 | 无证据、无校验的医学回答直接投递 | **不可接受（红线）** |
| 闲聊问题 → 当成医学 | 检索无果 → 「没有医学证据」，用户略感困惑 | 可接受（降级为体验问题） |

→ **凡不确定，一律 fail-closed 到 RAG。** 非医学路径必须被**正向判定**，绝不作为兜底。

---

## 2. 目标与非目标

### 2.1 目标

| # | 目标 | 优先级 |
|---|---|---|
| G1 | 明确与医学无关的闲聊（问候/感谢/算术/天气/玩笑等）由 LLM **直接生成**答复，不再返回「没有医学证据」 | P0 |
| G2 | 该路径**受限生成**：独立的约束提示词，显式禁止输出任何医学建议/诊断/指标解释；输出**二次筛检**后方可投递 | P0（安全红线） |
| G3 | 分流判定 **fail-closed**：`ORDINARY` 一律走 RAG；含医学词汇的输入**绝不**判为非医学；分类器故障/超时/非法标签 → 回 RAG，不进入闲聊路径 | P0（安全红线） |
| G4 | 闲聊答复**不得被误认为有证据支撑**：`citations` 恒为空、前端有明确"非医学"标注、不产生引用徽章 | P0（侧信道防护） |
| G5 | 闲聊路径**不得旁路既有任何合规闸**：仍必需 `EXTERNAL_MODEL` 同意、仍写审计、仍受模型白名单与零留存约束 | P0 |
| G6 | 分流结果**可观测可审计**：审计可区分闲聊与医学轮次，诊断键两侧白名单一致 | P1 |
| G7 | 开关关闭时，全链路行为**逐字节不变**（可回滚） | P0 |
| G8 | 修正 §1.4 的天气规则缺陷（`今天天气怎么样` 不应被判为医学科普） | P1 |

### 2.2 非目标（本次明确不做，及其原因）

| 不做 | 原因 |
|---|---|
| 修 `POST /v1/chat/answers` 的 **103.4s 延迟** | 已单独立项（见 §11 遗留项）。与本分流功能正交，且属检索/模型超时预算问题 |
| 多轮闲聊上下文记忆的**增强** | 复用既有 `history_size`（4 条）历史，不新增会话记忆机制 |
| 流式输出 / SSE | 架构级改动，超范围 |
| 闲聊路径接入工具调用（天气 API、计算器…） | 会让 LLM 获得外部副作用；本次仅允许纯文本生成 |
| 修改或放宽 `CitationValidator` | **明令禁止**。医学路径的校验强度一点不动 |
| 修复 P0-2（零真实医学语料）/ P0-3（复核态只进不出）/ P0-4（评估空转） | 均为独立在办项（见项目记忆），本次不触碰 |
| 把 `purpose` 透传到上游模型 HTTP 载荷 | `purpose` 在本仓库是**审计/策略**概念，不是模型侧控制参数（§3.4）。保持现状 |
| 新增 `ConsentPurpose` 枚举值 | 与医学路径保持一致（仅需 `EXTERNAL_MODEL`），避免同意模型分叉（§5 D-4） |

---

## 3. 总体架构

### 3.1 双闸结构

**"双闸"= 输入闸 + 输出闸**，二者缺一不可：

```
                         ┌─── 闸 1：输入安全（既有，位置不变，最先执行）────────────┐
用户提问 ───────────────►│ SafetyService.classify                                       │
                         │   URGENT / MEDICATION_CHANGE / DIAGNOSIS_REQUEST → 拦截出口   │
                         └──────────────────────────────────────────────────────────────┘
                                                  │ ALLOW_RETRIEVAL
                                                  ▼
                         ┌─── 闸 2：分流判定（新增，fail-closed）────────────────────────┐
                         │ QueryPipeline.process → Intent                                │
                         │   CASUAL（正向判定 + 医学词表否决 + 开关打开）  ──┐            │
                         │   REPORT / HEALTH / LAB                        │  │            │
                         │   ORDINARY（兜底，含 LLM 故障）                │  │            │
                         └───────────────────────────────────────────────┼──┴────────────┘
                                                     ▼                   ▼
                                            ┌──────────────┐    ┌──────────────────┐
                                            │ RAG 路径      │    │ 闲聊受限生成路径  │
                                            │ （完全不变）  │    │ 独立约束提示词    │
                                            │              │    │ 无证据、无引用    │
                                            └──────────────┘    └────────┬─────────┘
                                                     ▲                    │
                                                     │        ┌───────────▼──────────────┐
                                                     │        │ 闸 2b：输出二次筛检（新增）│
                                                     │        │  医学断言/建议/高危词命中？ │
                                                     └────────┤  是 → 丢弃，改走 RAG       │
                                                              │  否 → 投递 status="casual" │
                                                              └────────────────────────────┘
```

### 3.2 路由真值表（本设计的核心契约）

| 闸 1（SafetyService） | 闸 2 意图 | 医学词表命中 | 闲聊开关 | 最终路径 |
|---|---|---|---|---|
| URGENT | — | — | — | `urgent_escalation`（**不变**） |
| MEDICATION_CHANGE | — | — | — | `safe_template`（**不变**） |
| DIAGNOSIS_REQUEST | — | — | — | `safe_template`（**不变**） |
| ALLOW_RETRIEVAL | `CASUAL` | 无 | 开 | **闲聊受限生成** → 闸 2b |
| ALLOW_RETRIEVAL | `CASUAL` | **有** | — | **RAG**（否决） |
| ALLOW_RETRIEVAL | `CASUAL` | 无 | 关 | **RAG** |
| ALLOW_RETRIEVAL | `REPORT_EXPLANATION` / `HEALTH_EDUCATION` / `LAB_INDICATOR` | — | — | **RAG**（**不变**） |
| ALLOW_RETRIEVAL | `ORDINARY` | — | — | **RAG**（**fail-closed，不变**） |

> 最后一行是本 spec 最重要的设计决定：**`ORDINARY` 仍然走 RAG，一个字都不改。**
> 闲聊路径只对**被正向判定为 CASUAL** 的输入开放。

### 3.3 CASUAL 的正向判定（三道必要条件，全部满足才成立）

```
CASUAL  ⟺  ① 命中非医学规则集（问候/感谢/算术/天气/时间/玩笑/写作请求…）
        ∧  ② 未命中医学词表（身体/疾病/症状/指标/药物/检查/医院…）
        ∧  ③ 未被闸 1 拦截
```

- **①规则层**：新增 `_CASUAL` 规则集，语义优先级**高于 `HEALTH_EDUCATION`**
  （否则「今天天气怎么样」会继续被 `怎么` 拉去做医学科普——§1.4 缺陷）。
- **②词表否决**：**唯一在规则层与路由层各执行一次**的守卫（防御纵深），见 §3.5。
- 规则未命中（`ORDINARY`）时，既有 LLM 意图兜底照常工作：LLM 若判 `casual` → 进入候选；
  若判医学意图 → 医学路径；**LLM 失败/超时/非法标签 → `ORDINARY` → RAG**。

### 3.4 关于 `purpose` 的澄清（避免误判为可绕过点）

- `ChatRequest.purpose` 只进入 `ModelGateway.chat()` 的**审计 metadata**
  （`AuditAction.EXTERNAL_MODEL_CALL` 的 `payload_metadata["purpose"]`）。
- `OpenAIChatCompletionsAdapter.generate()` **不把 `purpose` 写进上游 HTTP 载荷**
  （实测 `models/http_adapters.py:88-97` 只发 `model` / `messages` / `max_completion_tokens`）。
- 结论：`purpose="casual_chat"` 是**可观测性**手段（让审计区分闲聊轮次），
  **不是**安全控制。安全控制全部由闸 1 / 闸 2 / 闸 2b 承担。本次**不改**适配器。

### 3.5 医学词表否决：为什么必须在两层各执行一次

| 层 | 位置 | 拦截对象 |
|---|---|---|
| 规则层 | `intent_rules.score_intent` | 防止 `你好，我想问下血红蛋白` 被 `你好` 判成 CASUAL |
| 路由层 | `IntentRouter.process`（意图最终确定后） | 防止 **LLM 误判**：LLM 把 `血红蛋白是什么` 判成 `casual` 时必须被否决 |

两层共用**同一个纯函数**（`query/casual.py:has_medical_term`），不得各写一套正则——
本项目已有"两处白名单漂移"的前科（`gate_score_space` 只改了 metrics 侧），
故此处显式要求单一实现 + 契约测试（S3）。

---

## 4. 核心数据契约

### 4.1 `Intent` 枚举扩展

```python
class Intent(StrEnum):
    REPORT_EXPLANATION = "report_explanation"
    HEALTH_EDUCATION   = "health_education"
    LAB_INDICATOR      = "lab_indicator"
    ORDINARY           = "ordinary"
    CASUAL             = "casual"          # 新增：正向判定的非医学输入
```

- **兼容性**：`StrEnum` 新增成员不改变既有成员取值，`ordinary` 的语义**不变**。
- `derive_strategy()`：新增显式分支 `if intent == Intent.CASUAL: raise`（或返回哨兵），
  **不允许** CASUAL 悄悄落入 `return SearchStrategy(include_public=True, include_private=True)`
  的兜底——那样"闲聊也被检索"会成为隐式行为。（见 §6.4）

### 4.2 新增模块 `src/medical_rag/query/casual.py`

```python
#: 明确的非医学闲聊规则（优先级高于所有医学意图）。
#: 原则：宁可漏判（漏判 → RAG，安全），不可误判（误判 → 无证据医学回答，红线）。
CASUAL_PATTERNS: tuple[re.Pattern[str], ...] = (...)

#: 医学词表：命中任一即**否决**闲聊判定（fail-closed）。
#: 覆盖 身体部位 / 疾病 / 症状 / 检验指标 / 药物 / 检查 / 就医 六类。
MEDICAL_LEXICON: frozenset[str] = frozenset({...})

def has_medical_term(text: str) -> bool:
    """文本是否含任何医学词汇。纯函数，两层共用的唯一实现。"""
```

`MEDICAL_LEXICON` 必须覆盖（最小集，可增不可删）：

| 类别 | 例（节选） |
|---|---|
| 身体部位 | 心脏 肝 肾 肺 胃 肠 甲状腺 子宫 前列腺 血液 血管 关节 骨 眼 耳 鼻 咽喉 |
| 疾病 | 高血压 糖尿病 冠心病 心梗 中风 癌症 肿瘤 肝炎 肾炎 贫血 感染 炎症 哮喘 甲亢 |
| 症状 | 胸痛 头痛 头晕 发热 咳嗽 腹泻 呕吐 恶心 乏力 失眠 疼痛 出血 水肿 心慌 |
| 检验指标 | 血红蛋白 白细胞 血小板 血糖 血压 血脂 转氨酶 肌酐 尿酸 胆固醇 甘油三酯 参考范围 正常值 |
| 药物/治疗 | 药 剂量 停药 换药 处方 手术 化疗 放疗 疫苗 抗生素 副作用 禁忌 说明书 治疗 诊断 预防 |
| 检查/就医 | 检查 化验 报告 体检 心电图 B超 CT 核磁 胃镜 活检 医院 医生 挂号 科室 门诊 急诊 |

> **设计取舍（显式记录）**：词表**故意偏宽**。例如「谢谢你，医生」会因 `医生` 被否决 → 走 RAG →
> 返回「没有医学证据」。这是**刻意选择的可接受劣化**：宽词表把"医学词 + 闲聊混合句"一律推向
> 安全侧，代价是少量寒暄句体验下降。**不得**为提升体验而收窄词表——收窄即扩大红线暴露面。

### 4.3 新增配置（`core/config.py`）

```python
#: 非医学闲聊分流总开关。
#: 默认 False —— 患者侧生成路径必须显式启用，不允许"默认打开"的隐式行为变更（G7）。
casual_chat_enabled: bool = False
#: 闲聊答复的输出长度上限（防止闲聊路径变成通用大模型入口）。
casual_chat_max_output_tokens: int = Field(default=300, gt=0, le=600)
```

启动期自检（**放 lifespan 的 `try/except` 之外**，否则被非生产环境的 log-and-continue 吞掉）：

```
若 casual_chat_enabled == True：
  必须能解析出模型网关与 MODEL_NAME（非空），否则 raise（fail-closed，进程不起）
```

### 4.4 响应契约

```jsonc
{
  "status": "casual",                       // 新增状态值
  "sections": [{ "title": "非医学回答", "text": "..." }],
  "citations": [],                          // 恒为空（G4）
  "safety_notice": "以下为非医学问题的直接回答，未引用医学资料，不作为任何医疗建议。",
  "trace_id": "...",
  "session_id": "...",
  "diagnostics": {
    "answer_path": "casual",                // 新增（两侧白名单，§4.5）
    "casual_gate_reason": "positive_non_medical"   // 新增（有界枚举）
  }
}
```

### 4.5 诊断键（**必须两侧白名单同时可见**）

既有硬约定：`chat/orchestrator._ANSWER_DIAGNOSTIC_KEYS`（HTTP 出口，**生产唯一可见出口**）
与 `observability/metrics._safe_diagnostics`（监控侧）必须同步，由
`tests/contract/test_diagnostic_whitelist_parity.py` 钉死。

| 新键 | 取值（有界枚举） | 语义 |
|---|---|---|
| `answer_path` | `rag` / `casual` / `safety_template` / `none` | 本轮走的是哪条路径（可观测"分流是否真的生效"） |
| `casual_gate_reason` | `positive_non_medical` / `medical_term_veto` / `intent_not_casual` / `llm_unavailable` / `disabled` / `output_screen_tripped` | 分流判定/否决/回退的原因 |

落地要求：
- `orchestrator._ANSWER_DIAGNOSTIC_KEYS` 加入 `answer_path`、`casual_gate_reason`；
- `metrics` 侧新增 `_SAFE_ANSWER_PATHS` / `_SAFE_CASUAL_GATE_REASONS` 两个 frozenset，
  并在 `_safe_diagnostics` 中按键放行（未知值哈希）；
- 扩展 `test_diagnostic_whitelist_parity.py` 的 `_METRICS_GATE_WHITELIST_TO_KEY` 映射表
  （该表刻意用显式映射，新增枚举必须同步登记，摩擦即设计意图）；
- `answer_path` 值 `casual` **不得**加入 `_SAFE_STATUSES`（那是 `status` 字段的枚举，二者不同键）。

---

## 5. 设计细节

### 5.1 规则层：新增 `_CASUAL` 并置于最高优先级

```python
_CASUAL = [
    re.compile(r"^(你好|您好|hi|hello|在吗|在不在|早上好|晚上好|嗨)([，,。!！~ ]|$)"),
    re.compile(r"谢谢|感谢|多谢|辛苦|麻烦了"),
    re.compile(r"再见|拜拜|下次聊"),
    re.compile(r"讲(个|一个)?(笑话|故事)|逗我|唱歌|聊天"),
    re.compile(r"今天|明天|昨天|现在几点|几点了|日期|星期几"),
    re.compile(r"天气|气温|下雨|晴天|冷不冷|热不热|穿什么衣服"),
    re.compile(r"帮我写(一|首|个|篇)(诗|文案|对联|祝福语|邮件)|翻译一下"),
    re.compile(r"你(叫|是)什么|你是谁|你是(什么|哪个)?(模型|机器人|AI)"),
]
PRIORITY = {
    Intent.CASUAL: 40,                 # 最高：非医学信号必须先于医学信号被看到
    Intent.REPORT_EXPLANATION: 30,
    Intent.LAB_INDICATOR: 20,
    Intent.HEALTH_EDUCATION: 10,
    Intent.ORDINARY: 0,
}
```

`score_intent` 的排序键 `(PRIORITY, count)` **不变**，因此规则层的改动面最小。

**为什么 `CASUAL` 优先级最高不会伤及医学问题**：优先级只在"已经命中"的意图之间做排序，
而 `CASUAL` 的成立还需要**词表否决**（§3.5）。`你好，我想问下血红蛋白` 同时命中
`CASUAL` 与 `ORDINARY`，但因 `血红蛋白` 被否决 → 落回 `ORDINARY` → RAG。

### 5.2 LLM 兜底层

- `llm_intent.LLMIntentClassifier`：`KNOWN_LABELS` 加入 `"casual"`；`_INTENT_SYSTEM_PROMPT` 增补
  一句强约束（原文必须写死，避免模型把医学问题往 casual 塞）：

  > `casual 仅用于与健康、身体、疾病、用药、检查、报告完全无关的日常闲聊；`
  > `只要问题涉及任何身体、症状、指标、疾病、用药或就医内容，一律不得归为 casual。`

- `IntentRouter.process` 的触发条件**不变**（规则未命中 或 置信度 < 0.6）。
- `IntentRouter.process` 的既有关键不变量**不变**：
  「LLM 故障不得把已识别的医学意图降级」——该规则同样保证 LLM 故障**不会**把医学问题送进闲聊路径。

### 5.3 路由层：`IntentRouter` 内置词表否决

在 `process()` 与 `classify()` 的**返回前**统一插入（两处共用私有方法 `_apply_medical_veto`）：

```python
def _apply_medical_veto(self, text: str, intent: Intent, confidence: float):
    if intent is Intent.CASUAL and has_medical_term(text):
        # 含医学词汇 → 否决闲聊。降级为 ORDINARY 置信度归零残余信号，
        # 由编排器按 fail-closed 规则走 RAG。
        return Intent.ORDINARY, _NO_SIGNAL_CONFIDENCE
    return intent, confidence
```

> 否决后的落点是 `ORDINARY`（→ RAG）而**不是**某个医学意图：我们不假装知道它属于哪类医学问题，
> 只知道"它不是闲聊"。这保持了最小惊讶原则。

### 5.4 编排层：`ChatOrchestrator.answer()` 的最小插入

插入点：`processed` 已算出、二次安全复检之后，**构造 `SearchQuery` 之前**。

```python
processed = self.query_pipeline.process(...)
if processed.text != question:
    recheck = self.safety.classify(processed.text)
    if recheck.action != SafetyAction.ALLOW_RETRIEVAL:
        return self._safe_response(context, recheck, db=db, diagnostics=...)

# --- 新增：非医学分流（闸 2 出口）---
if (
    self.casual_enabled
    and processed.intent is Intent.CASUAL
    and not has_medical_term(question)          # 路由层否决的兜底复核（纵深）
):
    casual = self._casual_answer(context, question, history=history, db=db, ...)
    if casual is not None:                      # None = 闸 2b 触发 → 落回 RAG
        return casual
```

**为什么放在二次复检之后**：归一化/上下文补全可能引入新的风险表述（既有逻辑），
分流必须看到与检索**同一份**文本，否则会出现"安全闸看到 A、分流看到 B"的错位。

### 5.5 闲聊受限生成（G2）

新增 `chat/prompts.py:CASUAL_SYSTEM_PROMPT`（要点，逐条可测）：

```
你是患者端应用的助手，当前用户的消息被判定为**与医学无关的日常闲聊**。
你只做日常寒暄与常识性闲聊。严格遵守：
1. 绝对不得输出任何医学信息、健康建议、疾病解释、指标含义、用药意见。
2. 绝对不得给出诊断、不得建议用药/停药/换药、不得解释化验单或报告。
3. 若用户消息实际上涉及健康或医疗内容，**只回复**：
   「这个问题涉及医疗健康，请重新描述为医学相关问题，我会依据医学资料回答。」
   不要尝试自己回答。
4. 不得编造任何数字、事实或引用标记（不要使用 [xxx] 形式）。
5. 保持简短（不超过 120 字）、友好、不使用绝对化断言。
```

请求构造：复用 `ChatRequest`，但 `evidence=[]`、`purpose="casual_chat"`、
`max_output_tokens=settings.casual_chat_max_output_tokens`（**不得**沿用 1200）。

### 5.6 闸 2b：输出二次筛检（**本设计的安全承重件**）

对模型输出做**纯本地、无模型调用**的筛检；任一命中即**丢弃输出并回退 RAG**：

| 检查 | 复用/新增 | 命中含义 |
|---|---|---|
| `CitationValidator._high_risk_claims` | **复用既有正则** | 诊断性/绝对化安慰结论 |
| `SafetyService.classify(output)` | **复用既有实现** | 输出中出现急症/用药/诊断表述 |
| `has_medical_term(output)` | 复用新词表 | 输出夹带医学词汇（说明它其实在答医学问题） |
| 引用标记 `[...]` 出现 | 复用 `CitationValidator.citation_pattern` | 模型伪造引用（幻觉引用，必须拦） |

```python
def _screen_casual_output(self, text: str) -> str | None:
    """返回 None 表示通过；返回原因字符串表示被拦（用于诊断键）。"""
```

命中后的行为：**回退 RAG**（不用固定话术）。理由：被拦说明该输入大概率**就是**医学问题，
正确归宿是医学路径；直接回固定话术会让用户第二次收到无效回复。诊断键记
`casual_gate_reason="output_screen_tripped"`。

### 5.7 落库与审计

- 闲聊轮次**照常落库**（否则会话历史恢复 G3/A4 会出现空白轮次）：
  `user` 消息 + `assistant` 消息，`metadata_json = {"trace_id":…, "intent":"casual", "cited_evidence":[]}`。
- 落库**不得**经过 `CitationValidator`：闲聊没有证据，强行构造 `ValidatedAnswer` 会污染
  "已校验回答"的语义。改为新增独立的 `_persist_casual_answer()`（复用 `ChatSession` 创建与
  幂等 `client_request_id` 判定逻辑，抽成共享私有方法避免复制）。
- 审计：`ANSWER_DELIVERED`（动作枚举**不新增**）+ `EXTERNAL_MODEL_CALL`
  （由 `ModelGateway` 自动写，`purpose="casual_chat"`）。
- `_record_audit` 的 payload 中加入 `answer_path`，使审计可按路径筛选。

### 5.8 前端

| 文件 | 改动 |
|---|---|
| `types/index.ts` | `ChatAnswer.status` 注释补充 `casual`；`diagnostics?: Record<string, unknown>` 补上（当前类型缺该字段） |
| `stores/chat.ts` `send()` | `status === "casual"` 时：`role` 用 `assistant`（**不是** `system`）、`citations` 保持空、`content` 取 `sections[0].text`、`casual: true` 标记 |
| `components/MessageBubble.vue` | `casual` 时渲染色调中性的「非医学回答」标签；**不渲染**引用徽章与 `citedEvidenceCount` 降级提示（二者都暗示"有依据"） |
| `components/SafetyNoticeBar.vue` | `status === "casual"` → `type="info"`，`title="非医学回答"` |

**G4 的具体断言**：前端**不存在**任何能让 `status="casual"` 的消息渲染出引用徽章的代码路径。

### 5.9 复用而非新造

| 能力 | 复用对象 |
|---|---|
| 安全闸 1 | `SafetyService`（不动） |
| 意图规则/置信度/LLM 兜底 | `intent_rules` / `intent.py` / `llm_intent.py`（增补，不重构） |
| 模型合规外发 | `ModelGateway`（**必须**经过，无旁路） |
| 高危断言正则 | `CitationValidator._high_risk_claims`（只读复用，不改校验强度） |
| 审计 | `AuditService`（只加 metadata 字段） |

---

## 6. 安全 / 合规不变量

| # | 不变量 | 断言方式（测试） |
|---|---|---|
| **S1** | 闸 1 先于分流：任何被安全闸拦截的输入**永不**进入闲聊路径 | `我想自杀` → `urgent_escalation`；`我停药了可以吗` → `safe_template`；两者 `status != "casual"` |
| **S2** | 医学词表否决：输入含任一医学词 → **绝不**判 CASUAL | `你好，血红蛋白多少算正常` → `intent != CASUAL`；`今天天气怎么样，我血压高吗` → 否决 |
| **S3** | 词表否决是**单一实现**且两层生效（防御纵深） | 契约测试：`has_medical_term` 是唯一词表来源；伪造 LLM 返回 `casual` + 医学输入 → 仍落 RAG |
| **S4** | `ORDINARY` 一律 RAG（fail-closed） | `血红蛋白是什么` → 走 RAG（非 casual），断言 `answer_path == "rag"` |
| **S5** | 分类器故障 → RAG，不得因故障进入闲聊 | LLM 分类器抛异常 / 返回非法标签 → `intent == ORDINARY` 且 `answer_path == "rag"` |
| **S6** | 闸 2b 输出筛检：闲聊输出含医学内容/高危断言/引用标记 → **丢弃**并回退 RAG | 桩模型返回 `你的血压偏高，建议停药。` → 响应 `status != "casual"`，且最终由 RAG 路径产出 |
| **S7** | 闲聊响应 `citations` 恒为空，且**不经** `CitationValidator` | 断言 `citations == []`；断言 `CitationValidator.validate` 未被调用（spy） |
| **S8** | 闲聊路径**不旁路** `EXTERNAL_MODEL` 同意 | 无同意时请求闲聊 → 降级（非 `casual` 生成），**不得**免费生成 |
| **S9** | 模型不可用/超时 → 闲聊路径降级，不得编造 | 模型抛 `ModelTimeout` → `status == "degraded"`，正文为固定提示 |
| **S10** | 关闭开关时全链路行为逐字节不变 | `casual_chat_enabled=False`：`你好` 仍返回 `insufficient_evidence`；全量既有测试无变化 |
| **S11** | 诊断键两侧白名单一致 | `test_diagnostic_whitelist_parity.py` 扩展后通过 |
| **S12** | 医学路径的校验强度**零变化** | 全量既有测试（859 passed / 3 skipped）逐字节不变 |
| **S13** | 审计可区分路径 | 闲聊轮次 `EXTERNAL_MODEL_CALL.purpose == "casual_chat"`；`ANSWER_DELIVERED.payload_metadata.answer_path == "casual"` |

> 既有"安全三项零容忍"（越权 / 删除残留 / 复核泄漏）不受影响：本次不涉及写越权、删除与复核态。

---

## 7. 错误与降级

| 场景 | 行为 |
|---|---|
| LLM 意图分类器失败/超时 | → `ORDINARY` → **RAG**（`casual_gate_reason="llm_unavailable"`） |
| LLM 返回非法标签 | → `ordinary`（既有逻辑）→ **RAG** |
| 闲聊生成模型超时/报错 | → `degraded`，固定提示「助手暂时无法回答，请稍后再试。」（**不编造内容**） |
| 闸 2b 命中 | → **回退 RAG**（`casual_gate_reason="output_screen_tripped"`） |
| 无 `EXTERNAL_MODEL` 同意 | → 走既有同意失败路径（`AccessDenied` → HTTP 层映射），闲聊无豁免 |
| 闲聊落库失败 | 与医学路径一致：记日志 + `rollback`，**不阻断**已生成答复的投递 |
| 开关关闭 | 与现状逐字节一致 |
| 总预算不足（`remaining <= 10s`） | 沿用既有 `_MIN_MODEL_BUDGET_SECONDS` 判定 → `degraded` |

---

## 8. 实施阶段

| 阶段 | 内容 | 可独立验收 | 是否需要单独批准 |
|---|---|---|---|
| **A** | 判定层：`Intent.CASUAL` + `query/casual.py`（词表/规则）+ 两层否决 + LLM 标签扩展 + 路由（CASUAL → **固定非医学模板**，即方案 A 的行为）+ 开关默认关 | S1–S5、S10、S11 | 否（本 spec 批准即可） |
| **B** | 受限生成：`CASUAL_SYSTEM_PROMPT` + `_casual_answer()` + 闸 2b 输出筛检 | S6–S9、S13 | 否 |
| **C** | 落库与前端：`_persist_casual_answer()` + 类型/Store/组件 + 「非医学回答」标注 | S7、G4 前端断言 | 否 |
| **D** | 真实验收（浏览器 + 真实后端 + 真实模型网关） | §10 A1–A8 | 否 |
| **E** | 开关打开（`casual_chat_enabled=True`） | 灰度与回滚验证 | **是**（改 `.env` 需单独批准） |

**阶段 A 先用方案 A（固定模板）跑通判定层**，是刻意的风险切分：
分流判定是红线所在，先用零生成风险的方式把判定与否决钉死，再在阶段 B 引入生成。
这样若阶段 A 的判定有偏差，暴露在"模板回复"而非"未经校验的医学回答"上。

---

## 9. 实施计划（待 spec 批准后另出 plan）

预期拆分为 5 个可独立验收任务（每任务 TDD：先写失败测试 → 确认失败 → 实现 → 验证）：

1. **T1** `query/casual.py` + 词表否决（纯函数 + 单元测试）
2. **T2** `Intent.CASUAL` 接入规则层与 LLM 标签（含天气规则缺陷修复）
3. **T3** 路由与编排层接线（CASUAL → 模板；含 fail-closed 与 `derive_strategy` 显式分支）
4. **T4** 受限生成 + 闸 2b + 落库（含审计与诊断键）
5. **T5** 后端契约/安全测试 + 前端改动 + 真实验收

---

## 10. 验收标准（可观测、可复现）

以 `13800138001 / 123456` 登录 http://localhost:5173，`casual_chat_enabled=True`：

| # | 步骤 | 期望 |
|---|---|---|
| A1 | 提问「你好」 | 收到**非医学直接回答**（`status=casual`），无引用徽章，带「非医学回答」标注 |
| A2 | 提问「今天天气怎么样」 | 同样得到闲聊回答（**不再**返回「没有医学证据」）——验证 G8 |
| A3 | 提问「血红蛋白是什么」 | **走 RAG**（走医学路径）；**绝不可**出现 `status=casual` —— 验证红线 G3 |
| A4 | 提问「你好，我的血红蛋白是多少算正常」 | **走 RAG**（词表否决生效） |
| A5 | 提问「我停药了可以吗」 | `safe_template`（闸 1 优先，未进闲聊） |
| A6 | 提问「我想自杀」 | `urgent_escalation`（闸 1 优先） |
| A7 | 临时把模型网关地址改成不可达，提问「讲个笑话」 | `degraded` + 固定提示，**无编造内容** |
| A8 | 把 `casual_chat_enabled` 改回 `False` 重启，提问「你好」 | 回到 `insufficient_evidence`（开关可回滚） |

自动化：
- 单元：`query/casual.py` 词表/规则；否决两层生效；`derive_strategy` 的 CASUAL 分支。
- 契约：S1–S5、S7、S8、S11（含诊断键 parity）。
- 集成：S6（桩模型返回医学内容 → 回退 RAG）、S9、S13。
- 回归：**全量**既有测试（基线 859 passed / 3 skipped）必须逐字节不变（S12）。
- 前端：`stores/chat.spec.ts` 新增 casual 映射用例；`vue-tsc -b` exit 0。

---

## 11. 风险与遗留

| 风险 | 缓解 |
|---|---|
| **医学问题被误判为闲聊（红线）** | 三道必要条件 + 两层词表否决 + 闸 2b 输出筛检 + S2/S3/S4/S6 测试钉死；任一条失败即阻断交付 |
| 闲聊路径被当作通用大模型入口滥用 | 输出长度上限 300 tokens + 约束提示词 + 审计按 `purpose=casual_chat` 可统计 |
| 词表偏宽导致寒暄句体验下降 | **刻意接受**（§4.2），并把"宽词表"写死为不可收窄的约束 |
| 诊断键两侧漂移 | 扩展 `test_diagnostic_whitelist_parity.py`，新增枚举必须同步登记 |
| 修天气规则引发既有意图测试回归 | T2 必须全量跑意图测试；既有 `test_query_intent.py` 6 例不许改期望值 |
| 开关默认关 → 功能"上线但未生效" | 阶段 E 单独批准并在验收清单中显式标注 |

**既有遗留项（本次显式不处理，延续在办）**：

1. `POST /v1/chat/answers` 实测 **103.4s** 延迟（已单独立项）。
2. 回答中 `## 标题` / `- 列表` 仍按纯文本渲染（独立视觉缺陷，待立项）。
3. `citations` 的标题/来源/版本、`status`、`safety_notice` **未落库** → 刷新后无法完整还原
   （会话持久化 spec §8 已知缺口）。
4. P0-2 零真实医学语料 / P0-3 复核态只进不出 / P0-4 评估空转。

---

## 12. spec 自检（FEATURE-WORKFLOW §5）

| 检查项 | 结论 |
|---|---|
| **占位符扫描** | 无 `TBD`/`TODO`。`MEDICAL_LEXICON` 与 `CASUAL_PATTERNS` 给出最小集与"可增不可删"规则，具体条目在 T1 落表并由测试枚举——**这是刻意的**：词表条目属实现细节，spec 固定的是"覆盖六类 + 只增不减 + 宽优于窄"这三条约束 |
| **一致性** | §3.2 真值表与 §5.1/§5.3/§5.4 的插入点一一对应；§4.4 响应契约与 §5.8 前端改动对齐；§4.5 诊断键与 §5.6 的 `casual_gate_reason` 取值一致 |
| **范围** | 可用一个实施计划（T1–T5）做完；阶段 E（改 `.env`）单独批准，不影响 A–D 的可验收性 |
| **歧义** | 唯一曾有歧义的是「`ORDINARY` 算不算非医学」——已在 §1.5 与 §3.2 最后一行**写死**为"不算，一律 RAG"。另一处歧义「闸 2b 命中后回退 RAG 还是回固定话术」——已在 §5.6 **写死**为"回退 RAG"并给出理由 |
| **红线是否写进文档** | 是。§1.5 + §3.2 + S2/S3/S4/S6，四条独立断言，非"只活在代码里" |
| **隐性不变量** | §3.5 单一词表实现、§5.7 落库不走 `CitationValidator`、§4.5 双白名单同步——三者均已写入文档并配契约测试 |

---

## 13. 待用户决策项

| # | 决策 | 建议 | 理由 |
|---|---|---|---|
| D-1 | 闲聊路径回退目标：RAG vs 固定话术 | **RAG** | 被拦大概率就是医学问题，固定话术会让用户二次受挫 |
| D-2 | `casual_chat_enabled` 默认值 | **`False`** | 患者侧生成路径默认关，避免隐式行为变更（G7） |
| D-3 | 医学词表宽窄 | **宽**（含"医生/医院"等通用词） | 宁可漏判（→RAG，安全）不可误判（红线） |
| D-4 | 闲聊是否需额外 `HEALTH_EDUCATION` 同意 | **否**，仅 `EXTERNAL_MODEL` | 与医学路径保持一致，避免同意模型分叉 |
| D-5 | 闸 2b 的引用标记检查 | **保留** | 闲聊无需引用；出现 `[...]` 即幻觉引用，必须拦 |
