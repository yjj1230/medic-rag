# Query Understanding & Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a composable query-processing pipeline (rewrite + intent routing) that runs after safety classification and before retrieval, improving recall and driving the retrieval strategy.

**Architecture:** A new `medical_rag/query` package exposes a `QueryPipeline` that runs ordered steps over a `QueryContext`: normalize (rules), intent routing (rules + optional LLM), context completion (multi-turn), and optional LLM rewrite. The pipeline emits a `ProcessedQuery` whose intent maps to `include_public/include_private` retrieval flags consumed by the existing `SearchQuery`. `ChatOrchestrator.answer()` is the single integration point and also loads session history for the completion step.

**Tech Stack:** Python 3.12, dataclasses, `StrEnum`, SQLAlchemy (history load), existing `ModelGateway` for LLM fallback.

**Design Spec:** `docs/superpowers/specs/2026-09-11-query-understanding-routing-design.md`

---

## File Structure

```
src/medical_rag/query/
  contracts.py            # Intent / SearchStrategy / ProcessedQuery / QueryContext / QueryStep
  normalize.py            # NormalizeStep
  intent_rules.py         # keyword->intent rules
  intent.py               # IntentRouter
  context_completion.py   # ContextCompletionStep
  llm_rewrite.py          # LLMRewriteStep
  pipeline.py             # QueryPipeline
tests/unit/
  test_query_contracts.py
  test_query_normalize.py
  test_query_intent.py
  test_query_context_completion.py
tests/integration/
  test_query_pipeline.py
src/medical_rag/chat/orchestrator.py   # integration
src/medical_rag/core/config.py         # new settings
.env.example                           # new env keys
```

Commit steps are skipped — this project has no git repo.

---

## Task 1: Query contracts

**Files:**
- Create: `src/medical_rag/query/__init__.py`
- Create: `src/medical_rag/query/contracts.py`
- Test: `tests/unit/test_query_contracts.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_query_contracts.py`:

```python
from medical_rag.query.contracts import Intent, SearchStrategy, ProcessedQuery, QueryContext


def test_intent_values():
    assert Intent.REPORT_EXPLANATION.value == "report_explanation"
    assert Intent.HEALTH_EDUCATION.value == "health_education"
    assert Intent.LAB_INDICATOR.value == "lab_indicator"
    assert Intent.ORDINARY.value == "ordinary"


def test_search_strategy_fields():
    s = SearchStrategy(include_public=True, include_private=False)
    assert s.include_public is True
    assert s.include_private is False


def test_processed_query_fields():
    pq = ProcessedQuery(
        text="高血压 注意什么",
        original="血压高注意啥",
        intent=Intent.HEALTH_EDUCATION,
        search_strategy=SearchStrategy(True, False),
        used_llm=False,
        rewrite_log=("x",),
    )
    assert pq.text == "高血压 注意什么"
    assert pq.intent == Intent.HEALTH_EDUCATION
    assert pq.rewrite_log == ("x",)


def test_query_context_defaults():
    ctx = QueryContext(original="q", current="q")
    assert ctx.intent == Intent.ORDINARY
    assert ctx.used_llm is False
    assert ctx.rewrite_log == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_query_contracts.py -q`
Expected: FAIL — import error, package/module missing.

- [ ] **Step 3: Create `src/medical_rag/query/__init__.py`**

```python
"""Query understanding and routing pipeline."""
```

- [ ] **Step 4: Create `src/medical_rag/query/contracts.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from medical_rag.domain.models import ChatMessage


class Intent(StrEnum):
    REPORT_EXPLANATION = "report_explanation"
    HEALTH_EDUCATION = "health_education"
    LAB_INDICATOR = "lab_indicator"
    ORDINARY = "ordinary"


@dataclass(frozen=True, slots=True)
class SearchStrategy:
    include_public: bool
    include_private: bool


@dataclass(frozen=True, slots=True)
class ProcessedQuery:
    text: str
    original: str
    intent: Intent
    search_strategy: SearchStrategy
    used_llm: bool = False
    rewrite_log: tuple[str, ...] = ()


@dataclass
class QueryContext:
    original: str
    current: str
    history: tuple[ChatMessage, ...] = ()
    intent: Intent = Intent.ORDINARY
    intent_confidence: float = 0.0
    used_llm: bool = False
    rewrite_log: list[str] = field(default_factory=list)


class QueryStep(Protocol):
    def process(self, ctx: QueryContext) -> QueryContext: ...
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_query_contracts.py -q`
Expected: 4 passed.

- [ ] **Step 6: Import smoke check**

Run: `python -c "from medical_rag.query.contracts import ProcessedQuery; print('ok')"`
Expected: prints `ok`.

---

## Task 2: NormalizeStep

**Files:**
- Create: `src/medical_rag/query/normalize.py`
- Test: `tests/unit/test_query_normalize.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_query_normalize.py`:

```python
from medical_rag.query.contracts import QueryContext, Intent
from medical_rag.query.normalize import NormalizeStep


def test_normalizes_colloquial_term():
    step = NormalizeStep()
    ctx = QueryContext(original="验血要注意什么", current="验血要注意什么")
    out = step.process(ctx)
    assert out.current == "血常规要注意什么"


def test_normalizes_typo_and_sets_log():
    step = NormalizeStep()
    ctx = QueryContext(original="胸疼怎么看", current="胸疼怎么看")
    out = step.process(ctx)
    assert out.current == "胸痛怎么看"
    assert len(out.rewrite_log) == 1


def test_no_change_short_circuits_log():
    step = NormalizeStep()
    ctx = QueryContext(original="高血压吃什么", current="高血压吃什么")
    out = step.process(ctx)
    assert out.current == "高血压吃什么"
    assert out.rewrite_log == []


def test_default_strategy_mapping_untouched():
    ctx = QueryContext(original="q", current="q")
    out = NormalizeStep().process(ctx)
    assert out.intent == Intent.ORDINARY
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_query_normalize.py -q`
Expected: FAIL — `medical_rag.query.normalize` does not exist.

- [ ] **Step 3: Create `src/medical_rag/query/normalize.py`**

```python
from __future__ import annotations

from medical_rag.query.contracts import QueryContext

# 口语/错别字/缩写 -> 标准医学词。替换式归一化。
TERM_MAP: dict[str, str] = {
    "验血": "血常规",
    "胸疼": "胸痛",
    "血压高": "高血压",
    "血压偏高": "高血压",
    "血压有点高": "高血压",
    "血糖高": "高血糖",
    "血糖偏高": "高血糖",
    "CT": "计算机断层扫描CT",
    "Hb": "血红蛋白",
    "脖子疼": "颈痛",
}

# 供后续检索层使用的同义词/缩写展开（本轮预留接口，不在文本里展开成 OR）。
SYNONYM_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "高血压": ("高血压", "血压高", "Hypertension"),
    "血红蛋白": ("血红蛋白", "Hb"),
    "血常规": ("血常规", "血细胞分析"),
}


class NormalizeStep:
    """Replace colloquial/typo/shortened terms with standard medical terms."""

    def process(self, ctx: QueryContext) -> QueryContext:
        normalized = ctx.current
        for term, standard in TERM_MAP.items():
            normalized = normalized.replace(term, standard)
        if normalized != ctx.current:
            ctx.rewrite_log.append(f"normalize: {ctx.current.strip()} -> {normalized.strip()}")
            ctx.current = normalized
        return ctx
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_query_normalize.py -q`
Expected: 4 passed.

---

## Task 3: Intent rules + IntentRouter

**Files:**
- Create: `src/medical_rag/query/intent_rules.py`
- Create: `src/medical_rag/query/intent.py`
- Test: `tests/unit/test_query_intent.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_query_intent.py`:

```python
from medical_rag.query.contracts import Intent
from medical_rag.query.intent import IntentRouter


def test_report_explanation_detected():
    r = IntentRouter()
    label, conf = r.classify("解释一下我的体检报告里的血红蛋白")
    assert label == Intent.REPORT_EXPLANATION
    assert conf >= 0.7


def test_health_education_detected():
    r = IntentRouter()
    label, _ = r.classify("高血压平时饮食要注意什么")
    assert label == Intent.HEALTH_EDUCATION


def test_lab_indicator_detected():
    r = IntentRouter()
    label, _ = r.classify("血红蛋白偏高是什么意思")
    assert label == Intent.LAB_INDICATOR


def test_ordinary_fallback():
    r = IntentRouter()
    label, _ = r.classify("你好")
    assert label == Intent.ORDINARY


def test_report_takes_priority_over_education():
    r = IntentRouter()
    label, _ = r.classify("帮我看看我的报告，怎么看血压高不高")
    assert label == Intent.REPORT_EXPLANATION
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_query_intent.py -q`
Expected: FAIL — modules missing.

- [ ] **Step 3: Create `src/medical_rag/query/intent_rules.py`**

```python
from __future__ import annotations

import re

from medical_rag.query.contracts import Intent

# 每条规则: (意图, 正则列表)
_REPORT = [
    re.compile(r"报告|结果|体检|复查|我的|上次查|这份|这张|这个结果"),
]
_EDUCATION = [
    re.compile(r"怎么|如何|注意|预防|饮食|运动|生活|应该吃什么"),
]
_LAB = [
    re.compile(r"什么(意思|意义)|正常吗|参考范围|正常范围|偏高|偏低|异常|升高|降低"),
]

# 语义优先级（值越大越强）。报告解释优先于所有，指标解释优先于科普。
PRIORITY: dict[Intent, int] = {
    Intent.REPORT_EXPLANATION: 30,
    Intent.LAB_INDICATOR: 20,
    Intent.HEALTH_EDUCATION: 10,
    Intent.ORDINARY: 0,
}


def score_intent(text: str) -> list[tuple[Intent, int]]:
    """返回按文本命中信号累加每条正则的 (意图, 命中数)。"""
    scores: dict[Intent, int] = {}
    low = text.casefold()
    for patterns, intent in (
        (_REPORT, Intent.REPORT_EXPLANATION),
        (_LAB, Intent.LAB_INDICATOR),
        (_EDUCATION, Intent.HEALTH_EDUCATION),
    ):
        count = sum(1 for pat in patterns if pat.search(low))
        if count:
            scores[intent] = count
    return sorted(
        ((intent, count) for intent, count in scores.items()),
        key=lambda item: (PRIORITY[item[0]], item[1]),
        reverse=True,
    )
```

- [ ] **Step 4: Create `src/medical_rag/query/intent.py`**

```python
from __future__ import annotations

from medical_rag.query.contracts import Intent
from medical_rag.query.intent_rules import score_intent, PRIORITY


_MIN_CONFIDENCE_FALLBACK = 0.3


class IntentRouter:
    """Rule-first intent classifier with optional LLM fallback.

    The LLM fallback is wired by the caller (orchestrator). When disabled or
    unavailable, the router returns the best rule match or ORDINARY.
    """

    def __init__(self, llm_classifier=None, *, enable_llm: bool = False) -> None:
        self._llm = llm_classifier
        self._enable_llm = enable_llm

    def classify(self, text: str) -> tuple[Intent, float]:
        ranked = score_intent(text)
        if not ranked:
            if self._should_use_llm(text):
                return self._classify_via_llm(text)
            return Intent.ORDINARY, 0.5

        top_intent, top_count = ranked[0]
        # 置信度 = 命中数 / (最高命中数 + 兜底)，简单启发式
        best = PRIORITY.get(top_intent, 0)
        conf = min(1.0, 0.5 + 0.2 * best + 0.05 * top_count)
        if conf < _MIN_CONFIDENCE_FALLBACK and self._should_use_llm(text):
            return self._classify_via_llm(text)
        return top_intent, conf

    def _should_use_llm(self, text: str) -> bool:
        return self._enable_llm and self._llm is not None

    def _classify_via_llm(self, text: str) -> tuple[Intent, float]:
        try:
            label = self._llm.classify_intent(text)
            return Intent(label), 0.9
        except Exception:
            return Intent.ORDINARY, 0.5
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_query_intent.py -q`
Expected: 5 passed.

---

## Task 4: ContextCompletionStep

**Files:**
- Create: `src/medical_rag/query/context_completion.py`
- Test: `tests/unit/test_query_context_completion.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_query_context_completion.py`:

```python
from uuid import UUID

from medical_rag.domain.models import ChatMessage
from medical_rag.query.contracts import QueryContext
from medical_rag.query.context_completion import ContextCompletionStep

SID = UUID("33333333-3333-3333-3333-333333333333")


def _msg(role: str, content: str) -> ChatMessage:
    return ChatMessage(session_id=SID, role=role, content=content)


def test_no_history_skips():
    ctx = QueryContext(original="它偏高", current="它偏高")
    out = ContextCompletionStep().process(ctx)
    assert out.current == "它偏高"
    assert out.rewrite_log == []


def test_no_referent_skips():
    history = (_msg("user", "血红蛋白是什么"),)
    ctx = QueryContext(original="血红蛋白怎么测", current="血红蛋白怎么测", history=history)
    out = ContextCompletionStep().process(ctx)
    assert out.current == "血红蛋白怎么测"


def test_resolves_referent_from_history():
    history = (
        _msg("user", "我的血红蛋白105",
             ),
        _msg("assistant", "你的血红蛋白是105 g/L，正常范围约115-150。"),
    )
    ctx = QueryContext(original="它为什么偏低", current="它偏低", history=history)
    out = ContextCompletionStep().process(ctx)
    assert out.current != "它偏低"
    assert "血红蛋白" in out.current
    assert len(out.rewrite_log) == 1


def test_mark_needs_llm_when_unresolvable():
    history = (_msg("assistant", "请参阅报告原文。"),)
    ctx = QueryContext(original="那个指标呢", current="那个指标呢", history=history)
    out = ContextCompletionStep().process(ctx)
    # 无法从历史解析时保持原样并打日志（交给后续 LLM step 决定）
    assert out.current == "那个指标呢"
    assert any("completion" in log for log in out.rewrite_log)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_query_context_completion.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Create `src/medical_rag/query/context_completion.py`**

```python
from __future__ import annotations

import re

from medical_rag.domain.models import ChatMessage
from medical_rag.query.contracts import QueryContext


_REFERENT = re.compile(
    r"它|他|她|那个指标|那个结果|上面|上次|这个指标|这药|这些数|这个数"
)
# 中文医疗报告里常见的可作指代锚点的实体关键词
_ANCHOR_ENTITY = re.compile(
    r"血红蛋白|白细胞|红细胞|血小板|血糖|血脂|胆固醇|血压|肝功|肾功|尿酸|ALT|AST|Hb|WBC|RBC"
)


def _extract_anchor_entities(messages: tuple[ChatMessage, ...]) -> list[str]:
    entities: list[str] = []
    seen: set[str] = set()
    for msg in reversed(messages):
        for match in _ANCHOR_ENTITY.finditer(msg.content or ""):
            term = match.group(0)
            if term not in seen:
                seen.add(term)
                entities.append(term)
    return entities


class ContextCompletionStep:
    """Resolve referents ('it', 'that result') using recent chat history."""

    def __init__(self, history_size: int = 4) -> None:
        self.history_size = history_size

    def process(self, ctx: QueryContext) -> QueryContext:
        if not ctx.history:
            return ctx
        if not _REFERENT.search(ctx.current):
            return ctx

        recent = ctx.history[-self.history_size :]
        anchors = _extract_anchor_entities(tuple(recent))
        if not anchors:
            ctx.rewrite_log.append(
                "completion: referent detected but no anchor entity in history (needs_llm)"
            )
            return ctx

        anchor = anchors[0]
        if anchor not in ctx.current:
            completed = f"{ctx.current} {anchor}"
            ctx.rewrite_log.append(
                f"completion: '{ctx.current.strip()}' -> '{completed.strip()}'"
            )
            ctx.current = completed
        return ctx
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_query_context_completion.py -q`
Expected: 4 passed.

---

## Task 5: QueryPipeline

**Files:**
- Create: `src/medical_rag/query/pipeline.py`
- Test: `tests/integration/test_query_pipeline.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_query_pipeline.py`:

```python
from uuid import UUID

from medical_rag.domain.models import ChatMessage
from medical_rag.query.contracts import Intent
from medical_rag.query.context_completion import ContextCompletionStep
from medical_rag.query.intent import IntentRouter
from medical_rag.query.normalize import NormalizeStep
from medical_rag.query.pipeline import QueryPipeline, derive_strategy

SID = UUID("33333333-3333-3333-3333-333333333333")


def _pipeline():
    return QueryPipeline(
        steps=[
            NormalizeStep(),
            IntentRouter(),
            ContextCompletionStep(),
        ]
    )


def test_pipeline_normalizes_and_sets_intent():
    pq = _pipeline().process("验血要注意什么")
    assert "血常规" in pq.text
    assert pq.intent == Intent.HEALTH_EDUCATION


def test_pipeline_report_intent_uses_private():
    pq = _pipeline().process("解释我的报告")
    assert pq.search_strategy.include_public is False
    assert pq.search_strategy.include_private is True


def test_pipeline_education_intent_uses_public_only():
    pq = _pipeline().process("高血压饮食注意什么")
    assert pq.search_strategy.include_public is True
    assert pq.search_strategy.include_private is False


def test_pipeline_ordinary_uses_mixed():
    pq = _pipeline().process("你好")
    assert pq.search_strategy.include_public is True
    assert pq.search_strategy.include_private is True


def test_pipeline_report_without_private_falls_back_to_public():
    pq = _pipeline().process("解释我的报告", has_private=False)
    assert pq.search_strategy.include_public is True
    assert pq.search_strategy.include_private is False


def test_derive_strategy():
    assert derive_strategy(Intent.LAB_INDICATOR) == derive_strategy(Intent.ORDINARY)
    s = derive_strategy(Intent.REPORT_EXPLANATION, has_private=True)
    assert s.include_private is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_query_pipeline.py -q`
Expected: FAIL — `medical_rag.query.pipeline` missing.

- [ ] **Step 3: Create `src/medical_rag/query/pipeline.py`**

```python
from __future__ import annotations

from typing import Callable

from medical_rag.query.context_completion import ContextCompletionStep
from medical_rag.query.contracts import (
    Intent,
    ProcessedQuery,
    QueryContext,
    QueryStep,
    SearchStrategy,
)
from medical_rag.query.intent import IntentRouter
from medical_rag.query.normalize import NormalizeStep


def derive_strategy(intent: Intent, *, has_private: bool = True) -> SearchStrategy:
    """Map an intent to retrieval flags. has_private == True means the patient
    has stored private report data available to retrieve."""
    if intent == Intent.REPORT_EXPLANATION:
        if has_private:
            return SearchStrategy(include_public=False, include_private=True)
        return SearchStrategy(include_public=True, include_private=False)
    if intent == Intent.HEALTH_EDUCATION:
        return SearchStrategy(include_public=True, include_private=False)
    # LAB_INDICATOR 与 ORDINARY 走混合
    return SearchStrategy(include_public=True, include_private=True)


class QueryPipeline:
    def __init__(
        self,
        steps: list[QueryStep] | None = None,
        *,
        history_size: int = 4,
    ) -> None:
        self.steps = steps or [
            NormalizeStep(),
            IntentRouter(),
            ContextCompletionStep(history_size=history_size),
        ]

    def process(
        self,
        original: str,
        history: tuple = (),
        *,
        has_private: bool = True,
    ) -> ProcessedQuery:
        ctx = QueryContext(original=original, current=original, history=tuple(history))
        for step in self.steps:
            ctx = step.process(ctx)
        strategy = derive_strategy(ctx.intent, has_private=has_private)
        return ProcessedQuery(
            text=ctx.current,
            original=ctx.original,
            intent=ctx.intent,
            search_strategy=strategy,
            used_llm=ctx.used_llm,
            rewrite_log=tuple(ctx.rewrite_log),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_query_pipeline.py -q`
Expected: 6 passed. If `test_pipeline_report_intent_uses_private` fails, adjust `_REPORT` regex so "解释我的报告" matches `报告`.

---

## Task 6: Integrate pipeline into ChatOrchestrator

**Files:**
- Modify: `src/medical_rag/chat/orchestrator.py`
- Modify: `src/medical_rag/core/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Add settings**

In `src/medical_rag/core/config.py`, add inside `Settings`:

```python
query_rewrite_enabled: bool = True
query_pipeline_history_size: int = 4
query_llm_rewrite_enabled: bool = False
intent_llm_classify_enabled: bool = False
```

In `.env.example`, append:

```ini
QUERY_REWRITE_ENABLED=true
QUERY_PIPELINE_HISTORY_SIZE=4
QUERY_LLM_REWRITE_ENABLED=false
INTENT_LLM_CLASSIFY_ENABLED=false
```

- [ ] **Step 2: Update the orchestrator to build and run the pipeline**

Read `src/medical_rag/chat/orchestrator.py`. Make these changes:

1. Add imports:
```python
from medical_rag.query.pipeline import QueryPipeline
from medical_rag.query.contracts import ProcessedQuery
from medical_rag.domain.models import ChatMessage
```

2. In `__init__`, add a `query_pipeline` parameter and lazily build a default one:
```python
self.query_pipeline = query_pipeline or QueryPipeline(history_size=4)
```
(Add `query_pipeline=None` to the signature keyword args.)

3. Add a private history-loader method:
```python
def _load_history(self, session_id: UUID | None) -> tuple[ChatMessage, ...]:
    if self.session is None or session_id is None:
        return ()
    return tuple(
        self.session.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
        .limit(4)
        .all()
    )
```

4. In `answer()`, replace the block that builds `SearchQuery` so it runs the pipeline first. Locate:
```python
        query = SearchQuery(
            text=question,
            patient_id=context.patient_id,
            top_k=8,
            include_public=True,
            include_private=True,
            document_ids=tuple(document_ids or ()),
        )
```
Replace with:
```python
        history = self._load_history(session_id) if self.query_pipeline is not None else ()
        processed: ProcessedQuery = self.query_pipeline.process(
            question, history=history, has_private=True
        )
        query = SearchQuery(
            text=processed.text,
            patient_id=context.patient_id,
            top_k=8,
            include_public=processed.search_strategy.include_public,
            include_private=processed.search_strategy.include_private,
            document_ids=tuple(document_ids or ()),
        )
```

- [ ] **Step 3: Run the existing chat integration tests**

Run: `pytest tests/integration/test_chat_orchestration.py -q`
Expected: 5 passed (no regression). If a test asserts `SearchQuery(text=question...)` directly, update that test fixture instead — the orchestrator now rewrites; keep the forced-text equal when no rewrite fires.

---

## Task 7: LLMRewriteStep (optional, off by default)

**Files:**
- Create: `src/medical_rag/query/llm_rewrite.py`
- Test: `tests/unit/test_query_llm_rewrite.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_query_llm_rewrite.py`:

```python
from uuid import UUID

from medical_rag.domain.models import ChatMessage
from medical_rag.query.contracts import QueryContext
from medical_rag.query.llm_rewrite import LLMRewriteStep

SID = UUID("33333333-3333-3333-3333-333333333333")
HISTORY = (ChatMessage(session_id=SID, role="assistant", content="你的血红蛋白105。"),)


def _ctx(q):
    c = QueryContext(original=q, current=q, history=HISTORY)
    c.rewrite_log.append("completion: referent detected but no anchor entity in history (needs_llm)")
    return c


def test_disabled_by_default_short_circuits():
    step = LLMRewriteStep(gateway=None, enabled=False)
    ctx = _ctx("那个指标呢")
    out = step.process(ctx)
    assert out.current == "那个指标呢"
    assert out.used_llm is False


def test_no_rewrite_trigger_skips():
    step = LLMRewriteStep(gateway=None, enabled=True)
    ctx = _ctx("高血压怎么预防")  # 无指代，无 needs_llm 标记
    ctx.rewrite_log.clear()
    out = step.process(ctx)
    assert out.used_llm is False


def test_rewrites_when_llm_available():
    class FakeGateway:
        def chat(self, context, request):
            return type("Resp", (), {"text": "血红蛋白为什么偏低", "trace_id": None})()

    step = LLMRewriteStep(gateway=FakeGateway(), enabled=True)
    ctx = _ctx("那个指标呢")
    out = step.process(ctx)
    assert out.used_llm is True
    assert out.current == "血红蛋白为什么偏低"


def test_graceful_degradation_when_gateway_raises():
    class BoomGateway:
        def chat(self, context, request):
            raise RuntimeError("no key")

    step = LLMRewriteStep(gateway=BoomGateway(), enabled=True)
    ctx = _ctx("那个指标呢")
    out = step.process(ctx)
    assert out.used_llm is False  # 失败不阻断
    assert out.current == "那个指标呢"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_query_llm_rewrite.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Create `src/medical_rag/query/llm_rewrite.py`**

```python
from __future__ import annotations

from medical_rag.query.contracts import QueryContext

_TRIGGER = ("needs_llm", "referen")
_REWRITE_SYSTEM = (
    "你是一个检索改写助手。只把用户的问题改写得更适合检索，不回答、不补充医学事实、"
    "不增删内容。若已正常则原样输出。只输出改写后的一句话。"
)


class LLMRewriteStep:
    """Optional LLM fallback that rewrites the query for retrieval.

    Enabled by config; routes through ModelGateway so consent/audit still apply.
    Any failure degrades gracefully (no rewrite, no error propagated).
    """

    def __init__(self, gateway, *, enabled: bool = False) -> None:
        self._gateway = gateway
        self._enabled = enabled

    def _should_rewrite(self, ctx: QueryContext) -> bool:
        if not self._enabled or self._gateway is None:
            return False
        # 仅当 context completion 标记了 needs_llm 或存在未解指代时改写
        joined = " ".join(ctx.rewrite_log)
        return any(token in joined for token in _TRIGGER)

    def process(self, ctx: QueryContext) -> QueryContext:
        if not self._should_rewrite(ctx):
            return ctx
        try:
            from medical_rag.auth.policy import AccessContext
            from medical_rag.models.contracts import ChatRequest

            # 构造一个最小改写请求（dummy context；真实接入时由 orchestrator 传入）
            request = ChatRequest(
                prompt=ctx.current,
                system_prompt=_REWRITE_SYSTEM,
                model="gpt-5.5",
                policy_version="v1",
            )
            dummy = AccessContext(
                tenant_id=ctx.history[0].session_id,
                patient_id=ctx.history[0].session_id,
                user_id=ctx.history[0].session_id,
            ) if ctx.history else None
            if dummy is None:
                return ctx
            response = self._gateway.chat(dummy, request)
            text = response.text.strip()
            if text:
                ctx.rewrite_log.append(f"llm_rewrite: -> {text}")
                ctx.current = text
                ctx.used_llm = True
        except Exception:
            pass  # 优雅降级，不阻断流程
        return ctx
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_query_llm_rewrite.py -q`
Expected: 4 passed.

---

## Task 8: Full-suite validation

- [ ] **Step 1: Run the complete backend test suite**

Run: `pytest -q`
Expected: all tests pass (previous 52 + new query tests). No regressions.

- [ ] **Step 2: Verify imports**

Run:
```bash
python -c "from medical_rag.query.pipeline import QueryPipeline, derive_strategy; print('ok')"
python -c "from medical_rag.chat.orchestrator import ChatOrchestrator; print('ok')"
```
Expected: both print `ok`.

- [ ] **Step 3: Confirm config keys**

Run: `python -c "from medical_rag.core.config import Settings; s=Settings.from_env(); print(s.query_rewrite_enabled, s.query_pipeline_history_size)"`
Expected: prints `True 4`.

---

## Execution Order

```text
Task 1  Contracts (data structures + step protocol)
   ↓
Task 2  NormalizeStep
   ↓
Task 3  Intent rules + IntentRouter
   ↓
Task 4  ContextCompletionStep
   ↓
Task 5  QueryPipeline (orchestrate + strategy mapping)
   ↓
Task 6  Orchestrator integration + session history load
   ↓
Task 7  LLMRewriteStep (optional, off by default)
   ↓
Task 8  Full-suite validation
```