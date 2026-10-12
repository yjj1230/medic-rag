# 复合 Query 拆解与逐项检索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一句含多个并列子问题的提问被拆解为逐子问题检索（各自意图口径），合并证据后单次生成，并对未覆盖子问题给出确定性提示。

**Architecture:** 在 QueryPipeline 之前插入确定性 `QuestionDecomposer`；QueryPipeline 对每个碎片复用现有 Normalize→Completion→Intent 步骤；新增 `SubQuestionRetriever` 并行检索并合并证据；`ChatOrchestrator` 在复合路径上逐碎片安全复检、单次生成、附加未覆盖 section。`sub_questions` 为空时走现有单查询路径，行为与现状一字不差。

**Tech Stack:** Python 3.12, pytest, dataclasses（frozen/slots），concurrent.futures.ThreadPoolExecutor。

**Spec:** `docs/superpowers/specs/2026-09-16-compound-query-decomposition-design.md`

## Global Constraints

- **当前工作区没有 Git 仓库；不创建提交。** 每个任务以"新测试先失败 → 实现 → 测试转绿"作为完成凭证，替代 commit 步骤（沿用 document-quality-indexing 计划的既有约定）。
- `ProcessedQuery.sub_questions` 为空 = 现有单查询路径；拆解失败 / 开关关闭 / 碎片超上限 / 组件异常都必须落入该路径，行为与现状一字不差。
- 不修改 `ChatRequest` / `OpenAIChatCompletionsAdapter` / `RetrievalService` 内部契约。
- 每个碎片单独过 `safety.classify`；任一碎片非 `ALLOW_RETRIEVAL` → 整句安全模板，绝不部分回答。
- 证据标记 `[evidence-id]` 格式不得改变（`CitationValidator.citation_pattern` 与渲染端共用同一模式）。
- 生成仍为**单次**模型调用；检索为有界并行（墙钟 ≈ max 而非 sum）。
- 执行顺序：Task 1 → 2 → 3 → 4 → 5 → 6 → 7（后续任务依赖前置任务的类型与模块）。

---

### Task 1: 契约扩展 — SubQuestion 与 ProcessedQuery.sub_questions

**Files:**
- Modify: `src/medical_rag/query/contracts.py`
- Test: `tests/unit/test_subquestion_contracts.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_subquestion_contracts.py`：

```python
from medical_rag.query.contracts import Intent, ProcessedQuery, SearchStrategy, SubQuestion


def test_sub_question_carries_text_intent_and_strategy():
    sub = SubQuestion(
        text="我的体检报告结果怎么样？",
        intent=Intent.REPORT_EXPLANATION,
        search_strategy=SearchStrategy(include_public=False, include_private=True),
    )
    assert sub.text == "我的体检报告结果怎么样？"
    assert sub.search_strategy.include_public is False
    assert sub.search_strategy.include_private is True


def test_processed_query_defaults_to_empty_sub_questions():
    pq = ProcessedQuery(
        text="你好",
        original="你好",
        intent=Intent.ORDINARY,
        search_strategy=SearchStrategy(include_public=True, include_private=True),
    )
    assert pq.sub_questions == ()


def test_processed_query_accepts_sub_questions():
    sub = SubQuestion(
        text="血红蛋白偏高吗？",
        intent=Intent.LAB_INDICATOR,
        search_strategy=SearchStrategy(include_public=True, include_private=True),
    )
    pq = ProcessedQuery(
        text="血红蛋白偏高吗？",
        original="血红蛋白偏高吗？",
        intent=Intent.LAB_INDICATOR,
        search_strategy=SearchStrategy(include_public=True, include_private=True),
        sub_questions=(sub,),
    )
    assert pq.sub_questions[0].text == "血红蛋白偏高吗？"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/test_subquestion_contracts.py -q`
Expected: FAIL，`ImportError: cannot import name 'SubQuestion'`

- [ ] **Step 3: 实现契约**

`src/medical_rag/query/contracts.py`：在 `SearchStrategy` 定义之后、`ProcessedQuery` 之前新增：

```python
@dataclass(frozen=True, slots=True)
class SubQuestion:
    """一个可独立检索的子问题：检索文本 + 该碎片自己的意图与检索口径。"""

    text: str
    intent: Intent
    search_strategy: SearchStrategy
```

并在 `ProcessedQuery` 字段末尾（`rewrite_log` 之后）追加一个带默认值的字段：

```python
    sub_questions: tuple[SubQuestion, ...] = ()  # 空 = 未拆分（旧单查询路径）
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/unit/test_subquestion_contracts.py -q`
Expected: 3 passed

- [ ] **Step 5: 确认无回归**

Run: `python -m pytest tests/unit/test_query_contracts.py tests/integration/test_query_pipeline.py -q`
Expected: 全部 passed（`sub_questions` 有默认值，现有构造不受影响）

---

### Task 2: QuestionDecomposer（确定性规则拆解）

**Files:**
- Create: `src/medical_rag/query/decompose.py`
- Test: `tests/unit/test_query_decompose.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_query_decompose.py`：

```python
from medical_rag.query.decompose import QuestionDecomposer


def test_splits_on_question_marks():
    fragments = QuestionDecomposer().decompose("血红蛋白偏高吗？还有我该吃什么？")
    assert fragments == ("血红蛋白偏高吗？", "还有我该吃什么？")


def test_splits_on_connector_after_punctuation():
    fragments = QuestionDecomposer().decompose("血红蛋白偏高吗，还有我该注意什么")
    assert fragments == ("血红蛋白偏高吗", "还有我该注意什么")


def test_does_not_split_when_connector_follows_a_word():
    # "还有" = "仍然有"，不是并列连接词，不得切分。
    assert QuestionDecomposer().decompose("报告里还有哪些异常？") == ("报告里还有哪些异常？",)


def test_invalid_first_fragment_merges_into_next():
    # "我"既无疑问信号又短于 4 字 → 并回下一个碎片 → 整体不拆。
    assert QuestionDecomposer().decompose("我，还有血糖问题吗？") == ("我，还有血糖问题吗？",)


def test_duplicate_fragments_collapse_to_single():
    assert QuestionDecomposer().decompose("血红蛋白偏高吗？血红蛋白偏高吗？") == ("血红蛋白偏高吗？",)


def test_fragments_over_cap_abandon_decomposition():
    decomposer = QuestionDecomposer(max_sub_questions=2)
    text = "血红蛋白偏高吗？白细胞正常吗？我该注意什么？"
    assert decomposer.decompose(text) == (text,)


def test_single_question_is_untouched_and_stripped():
    assert QuestionDecomposer().decompose("  血红蛋白偏高吗？  ") == ("血红蛋白偏高吗？",)
    assert QuestionDecomposer().decompose("你好") == ("你好",)


def test_empty_input_returns_single_element():
    assert QuestionDecomposer().decompose("") == ("",)


def test_decompose_is_deterministic():
    text = "血红蛋白偏高吗？还有我该吃什么？"
    assert QuestionDecomposer().decompose(text) == QuestionDecomposer().decompose(text)


def test_decompose_degrades_to_single_on_internal_error():
    class _Broken(QuestionDecomposer):
        def _decompose(self, text):
            raise RuntimeError("boom")

    assert _Broken().decompose("任何问题") == ("任何问题",)


def test_max_sub_questions_must_be_positive():
    import pytest

    with pytest.raises(ValueError):
        QuestionDecomposer(max_sub_questions=0)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/test_query_decompose.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'medical_rag.query.decompose'`

- [ ] **Step 3: 实现拆解器**

创建 `src/medical_rag/query/decompose.py`：

```python
"""确定性复合问题拆解（spec 2026-09-16 §6.1）。

QuestionDecomposer 只做一件事：把一句可能包含多个并列子问题的提问拆成
若干可独立检索的子问题文本。纯规则、无模型调用、无 IO；同一输入恒产
同一输出。任何异常或不确定情形都降级为"不拆"（返回单元素），保证调用
方行为与单查询路径一致（spec §4.2 降级不变式）。
"""

from __future__ import annotations

import re

# 规则 1 的切分标点：问号/分号。句号不切——陈述句不是子问题边界。
_SPLIT_PUNCT = "？！;；"
# 规则 2 的并列连接词：仅当紧邻前方标点时才是切分点，防止
# "报告里还有哪些异常"（还有=仍然有）被误拆。
_CONNECTORS = ("还有", "以及", "另外", "同时", "并且", "然后", "顺便")
_PRE_CONNECTOR_PUNCT = "，。；！？,.;!?"
# 规则 3 的问题性信号：命中任一即视为"像一个独立子问题"。
_QUESTION_SIGNAL = re.compile(r"吗|呢|什么|怎么|如何|哪些|多少|是不是|能否|可否|[？?]")
_MIN_FRAGMENT_CHARS = 4


def _is_valid_fragment(fragment: str) -> bool:
    return len(fragment) >= _MIN_FRAGMENT_CHARS and bool(_QUESTION_SIGNAL.search(fragment))


class QuestionDecomposer:
    """把一句提问拆成 1..N 个子问题；返回长度 1 表示不拆。"""

    def __init__(self, max_sub_questions: int = 3) -> None:
        if max_sub_questions < 1:
            raise ValueError("max_sub_questions must be positive")
        self.max_sub_questions = max_sub_questions

    def decompose(self, text: str) -> tuple[str, ...]:
        try:
            return self._decompose(text)
        except Exception:
            return (text,)

    def _decompose(self, text: str) -> tuple[str, ...]:
        stripped = (text or "").strip()
        if not stripped:
            return (text,)
        fragments = self._split_on_punctuation(stripped)
        fragments = self._split_on_connectors(fragments)
        fragments = self._merge_invalid(fragments)
        pre_dedupe_count = len(fragments)
        fragments = self._dedupe(fragments)
        if len(fragments) > self.max_sub_questions:
            return (stripped,)
        if len(fragments) == 1:
            # 去重把多个碎片折叠成 1 个 = 重复提问视为同一问题，返回去重后的
            # 碎片本身；其余单碎片情形（本就未拆 / 无效碎片并回）返回原句
            # stripped，保留原始标点与措辞（不暴露 _merge_invalid 并回时插入的空格）。
            return (fragments[0],) if pre_dedupe_count > 1 else (stripped,)
        return tuple(fragments)

    def _split_on_punctuation(self, text: str) -> list[str]:
        segments: list[str] = []
        buffer: list[str] = []
        for char in text:
            buffer.append(char)
            if char in _SPLIT_PUNCT:
                segment = "".join(buffer).strip()
                if segment:
                    segments.append(segment)
                buffer = []
        tail = "".join(buffer).strip()
        if tail:
            segments.append(tail)
        return segments

    def _split_on_connectors(self, segments: list[str]) -> list[str]:
        result: list[str] = []
        for segment in segments:
            pieces = [segment]
            for connector in _CONNECTORS:
                split_pieces: list[str] = []
                for piece in pieces:
                    split_pieces.extend(self._split_piece(piece, connector))
                pieces = split_pieces
            result.extend(pieces)
        return result

    def _split_piece(self, piece: str, connector: str) -> list[str]:
        cuts = [0]
        start = 0
        while True:
            index = piece.find(connector, start)
            if index < 0:
                break
            if index > 0 and piece[index - 1] in _PRE_CONNECTOR_PUNCT:
                cuts.append(index)
            start = index + len(connector)
        if len(cuts) == 1:
            return [piece]
        fragments: list[str] = []
        for position, cut in enumerate(cuts):
            end = cuts[position + 1] if position + 1 < len(cuts) else len(piece)
            fragment = piece[cut:end].strip()
            if position + 1 < len(cuts):
                fragment = fragment.rstrip(_PRE_CONNECTOR_PUNCT).strip()
            if fragment:
                fragments.append(fragment)
        return fragments

    def _merge_invalid(self, fragments: list[str]) -> list[str]:
        merged: list[str] = []
        pending: list[str] = []
        for fragment in fragments:
            if _is_valid_fragment(fragment):
                if pending:
                    merged.append(" ".join([*pending, fragment]))
                    pending = []
                else:
                    merged.append(fragment)
            else:
                pending.append(fragment)
        if pending:
            if merged:
                merged[-1] = f"{merged[-1]} {' '.join(pending)}".strip()
            else:
                merged.append(" ".join(pending))
        return merged

    def _dedupe(self, fragments: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for fragment in fragments:
            key = " ".join(fragment.split())
            if key in seen:
                continue
            seen.add(key)
            result.append(fragment)
        return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/unit/test_query_decompose.py -q`
Expected: 11 passed

---

### Task 3: QueryPipeline 复合路径（拆解 → 逐碎片理解）

**Files:**
- Modify: `src/medical_rag/query/pipeline.py`（整体重写）
- Test: `tests/unit/test_query_pipeline_compound.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_query_pipeline_compound.py`：

```python
from uuid import uuid4

from medical_rag.domain.models import ChatMessage
from medical_rag.query.contracts import Intent
from medical_rag.query.decompose import QuestionDecomposer
from medical_rag.query.pipeline import QueryPipeline


def _pipeline() -> QueryPipeline:
    return QueryPipeline(decomposer=QuestionDecomposer())


def _history_with_anchor() -> tuple[ChatMessage, ...]:
    return (
        ChatMessage(session_id=uuid4(), role="assistant", content="你的血红蛋白是 105 g/L，低于参考范围。"),
    )


def test_compound_query_produces_per_fragment_sub_questions():
    pq = _pipeline().process("我的体检报告结果怎么样？还有平时饮食该注意什么？")
    assert len(pq.sub_questions) == 2
    assert pq.sub_questions[0].intent == Intent.REPORT_EXPLANATION
    assert pq.sub_questions[1].intent == Intent.HEALTH_EDUCATION


def test_compound_query_routes_strategies_per_fragment():
    pq = _pipeline().process("我的体检报告结果怎么样？还有平时饮食该注意什么？", has_private=True)
    assert pq.sub_questions[0].search_strategy.include_public is False
    assert pq.sub_questions[0].search_strategy.include_private is True
    assert pq.sub_questions[1].search_strategy.include_public is True
    assert pq.sub_questions[1].search_strategy.include_private is False


def test_completion_anchor_lands_in_the_right_fragment():
    # 关键回归（spec §4.1）：先拆解后补全，锚点必须落进含指代词的碎片。
    pq = _pipeline().process("它偏高吗？还有我该注意什么？", history=_history_with_anchor())
    assert "血红蛋白" in pq.sub_questions[0].text
    assert pq.sub_questions[0].intent == Intent.LAB_INDICATOR
    assert pq.sub_questions[1].intent == Intent.HEALTH_EDUCATION


def test_compound_intent_is_highest_priority_fragment():
    pq = _pipeline().process("我的体检报告结果怎么样？还有平时饮食该注意什么？")
    assert pq.intent == Intent.REPORT_EXPLANATION
    assert "decompose: 2 fragments" in pq.rewrite_log[0]


def test_single_question_keeps_legacy_shape():
    pq = _pipeline().process("解释我的报告")
    assert pq.sub_questions == ()
    assert pq.text == "解释我的报告"
    assert pq.search_strategy.include_public is False


def test_no_decomposer_keeps_legacy_shape():
    pq = QueryPipeline().process("验血要注意什么")
    assert pq.sub_questions == ()
    assert "血常规" in pq.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/test_query_pipeline_compound.py -q`
Expected: FAIL，`TypeError: QueryPipeline.__init__() got an unexpected keyword argument 'decomposer'`

- [ ] **Step 3: 重写 pipeline.py**

用以下完整内容替换 `src/medical_rag/query/pipeline.py`（`derive_strategy` 保持原样不动）：

```python
from __future__ import annotations

from medical_rag.query.context_completion import ContextCompletionStep
from medical_rag.query.contracts import (
    Intent,
    ProcessedQuery,
    QueryContext,
    QueryStep,
    SearchStrategy,
    SubQuestion,
)
from medical_rag.query.decompose import QuestionDecomposer
from medical_rag.query.intent import IntentRouter
from medical_rag.query.intent_rules import PRIORITY
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
        decomposer: QuestionDecomposer | None = None,
    ) -> None:
        self.steps = steps or [
            NormalizeStep(),
            IntentRouter(),
            ContextCompletionStep(history_size=history_size),
        ]
        # decomposer 为 None 表示关闭拆解（默认），行为与旧版完全一致；
        # 生产由 composition 依据 QUERY_DECOMPOSITION_ENABLED 决定是否注入。
        self.decomposer = decomposer

    def process(
        self,
        original: str,
        history: tuple = (),
        *,
        has_private: bool = True,
        access_context=None,
    ) -> ProcessedQuery:
        ctx = QueryContext(
            original=original,
            current=original,
            history=tuple(history),
            access_context=access_context,
        )
        fragments = (
            self.decomposer.decompose(original) if self.decomposer is not None else (original,)
        )
        if len(fragments) <= 1:
            return self._process_single(ctx, has_private=has_private)
        return self._process_compound(ctx, fragments, has_private=has_private)

    def _process_single(self, ctx: QueryContext, *, has_private: bool) -> ProcessedQuery:
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

    def _process_compound(
        self, ctx: QueryContext, fragments: tuple[str, ...], *, has_private: bool
    ) -> ProcessedQuery:
        # 顺序依据 spec §4.1：先拆解，再逐碎片 Normalize → Completion → Intent，
        # 保证 ContextCompletion 的锚点实体落在含指代词的碎片上。
        rewrite_log: list[str] = [f"decompose: {len(fragments)} fragments"]
        sub_questions: list[SubQuestion] = []
        best_priority = -1
        best_intent = Intent.ORDINARY
        used_llm = False
        for fragment in fragments:
            frag_ctx = QueryContext(
                original=ctx.original,
                current=fragment,
                history=ctx.history,
                access_context=ctx.access_context,
            )
            for step in self.steps:
                frag_ctx = step.process(frag_ctx)
            strategy = derive_strategy(frag_ctx.intent, has_private=has_private)
            sub_questions.append(
                SubQuestion(text=frag_ctx.current, intent=frag_ctx.intent, search_strategy=strategy)
            )
            used_llm = used_llm or frag_ctx.used_llm
            rewrite_log.extend(f"[{fragment}] {entry}" for entry in frag_ctx.rewrite_log)
            priority = PRIORITY.get(frag_ctx.intent, 0)
            if priority > best_priority:
                best_priority = priority
                best_intent = frag_ctx.intent
        # 复合路径下 text 保持原句：orchestrator 只读 sub_questions，
        # 且 `processed.text != question` 的整句复检因此自然跳过（由逐碎片复检替代）。
        return ProcessedQuery(
            text=ctx.original,
            original=ctx.original,
            intent=best_intent,
            search_strategy=derive_strategy(best_intent, has_private=has_private),
            used_llm=used_llm,
            rewrite_log=tuple(rewrite_log),
            sub_questions=tuple(sub_questions),
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/unit/test_query_pipeline_compound.py tests/integration/test_query_pipeline.py -q`
Expected: 全部 passed（新测试 6 个 + 既有管线测试不回归）

---

### Task 4: SubQuestionRetriever（并行检索编排与证据合并）

**Files:**
- Create: `src/medical_rag/chat/subquestion_retrieval.py`
- Test: `tests/unit/test_subquestion_retrieval.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_subquestion_retrieval.py`：

```python
import time
from uuid import uuid4

from medical_rag.auth.policy import AccessContext
from medical_rag.chat.subquestion_retrieval import SubQuestionRetriever
from medical_rag.knowledge.contracts import Citation
from medical_rag.query.contracts import Intent, SearchStrategy, SubQuestion
from medical_rag.retrieval.contracts import Evidence, RetrievalResult, SearchQuery, Visibility


def _context() -> AccessContext:
    return AccessContext(tenant_id=uuid4(), patient_id=uuid4(), user_id=uuid4())


def _sub(text: str, *, include_public: bool = True, include_private: bool = True) -> SubQuestion:
    return SubQuestion(
        text=text,
        intent=Intent.ORDINARY,
        search_strategy=SearchStrategy(include_public=include_public, include_private=include_private),
    )


def _evidence(evidence_id: str, text: str) -> Evidence:
    return Evidence(
        id=evidence_id,
        text=text,
        citation=Citation(source_id="src-1", title="检验解读", version="v1", location="第1节"),
        score=1.0,
        visibility=Visibility.PUBLIC,
    )


class _FakeRetriever:
    def __init__(self, results: dict[str, list[Evidence]]):
        self.results = results
        self.queries: list[SearchQuery] = []

    def retrieve(self, query: SearchQuery, context):
        self.queries.append(query)
        items = self.results.get(query.text, [])
        return RetrievalResult(
            items=list(items), reason=None if items else "insufficient_evidence", diagnostics={}
        )


def test_merges_union_deduped_and_ordered():
    shared = _evidence("ev-shared", "共享证据")
    retriever = _FakeRetriever(
        {
            "问题一": [_evidence("ev-1", "证据一"), shared],
            "问题二": [shared, _evidence("ev-2", "证据二")],
        }
    )
    merged = SubQuestionRetriever(retriever).retrieve_all((_sub("问题一"), _sub("问题二")), _context())
    assert [item.id for item in merged.items] == ["ev-1", "ev-shared", "ev-2"]
    assert [group.reason for group in merged.groups] == [None, None]


def test_each_subquestion_gets_its_own_strategy_flags():
    retriever = _FakeRetriever({"问题一": [], "问题二": []})
    SubQuestionRetriever(retriever).retrieve_all(
        (_sub("问题一", include_public=False), _sub("问题二", include_private=False)),
        _context(),
    )
    assert retriever.queries[0].include_public is False
    assert retriever.queries[0].include_private is True
    assert retriever.queries[1].include_public is True
    assert retriever.queries[1].include_private is False


def test_empty_group_is_marked_not_failed():
    retriever = _FakeRetriever({"问题一": [_evidence("ev-1", "证据一")], "问题二": []})
    merged = SubQuestionRetriever(retriever).retrieve_all((_sub("问题一"), _sub("问题二")), _context())
    assert merged.groups[0].reason is None
    assert merged.groups[1].reason == "insufficient_evidence"
    assert merged.groups[1].items == ()


def test_retriever_failure_only_marks_that_group():
    class _Boom:
        def retrieve(self, query: SearchQuery, context):
            if query.text == "问题一":
                raise RuntimeError("boom")
            return RetrievalResult(items=[_evidence("ev-2", "证据二")], reason=None, diagnostics={})

    merged = SubQuestionRetriever(_Boom()).retrieve_all((_sub("问题一"), _sub("问题二")), _context())
    assert merged.groups[0].reason == "retrieval_error"
    assert merged.groups[1].reason is None
    assert [item.id for item in merged.items] == ["ev-2"]


def test_parallel_wall_clock_is_max_not_sum():
    class _Slow:
        def retrieve(self, query: SearchQuery, context):
            time.sleep(0.2)
            return RetrievalResult(items=[], reason="insufficient_evidence", diagnostics={})

    started = time.perf_counter()
    merged = SubQuestionRetriever(_Slow(), max_workers=3).retrieve_all(
        (_sub("一"), _sub("二"), _sub("三")), _context()
    )
    elapsed = time.perf_counter() - started
    assert len(merged.groups) == 3
    assert elapsed < 0.55  # 串行 sum ≈ 0.6s，并行 ≈ 0.2s + 调度余量
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/test_subquestion_retrieval.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'medical_rag.chat.subquestion_retrieval'`

- [ ] **Step 3: 实现 SubQuestionRetriever**

创建 `src/medical_rag/chat/subquestion_retrieval.py`：

```python
"""逐子问题并行检索编排与证据合并（spec 2026-09-16 §6.4）。

SubQuestionRetriever 只做编排：把 N 个子问题并行交给同一个
RetrievalService，收集各组结果并合并为去重后的证据并集。单组失败只
标记该组 reason，绝不向上传播——部分失败时其余子问题照常回答。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID

from medical_rag.auth.policy import AccessContext
from medical_rag.query.contracts import SearchStrategy, SubQuestion
from medical_rag.retrieval.contracts import Evidence, SearchQuery


@dataclass(frozen=True, slots=True)
class SubQuestionEvidence:
    question: str
    items: tuple[Evidence, ...]
    # 该组检索时使用的口径：orchestrator 逐组扩展兄弟上下文时按组取用，
    # 不依赖 groups 与 sub_questions 的顺序配对。
    search_strategy: SearchStrategy
    reason: str | None = None  # None=成功；否则 insufficient_evidence/retrieval_timeout/retrieval_error


@dataclass(frozen=True, slots=True)
class MergedRetrieval:
    groups: tuple[SubQuestionEvidence, ...]

    @property
    def items(self) -> list[Evidence]:
        """并集：按证据 id 去重、保序（首次出现优先）。"""
        seen: set[str] = set()
        merged: list[Evidence] = []
        for group in self.groups:
            for item in group.items:
                if item.id in seen:
                    continue
                seen.add(item.id)
                merged.append(item)
        return merged


class SubQuestionRetriever:
    def __init__(self, retriever, *, max_workers: int = 4) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.retriever = retriever
        # 与 RetrievalService 同款：进程内长生命周期池，构造时创建。
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rag-subquestion")

    def retrieve_all(
        self,
        sub_questions: tuple[SubQuestion, ...],
        context: AccessContext,
        *,
        timeout_seconds: float | None = None,
        top_k: int = 10,
        document_ids: tuple[UUID, ...] = (),
    ) -> MergedRetrieval:
        futures: list[tuple[SubQuestion, object]] = []
        for sub_question in sub_questions:
            query = SearchQuery(
                text=sub_question.text,
                patient_id=context.patient_id,
                top_k=top_k,
                include_public=sub_question.search_strategy.include_public,
                include_private=sub_question.search_strategy.include_private,
                document_ids=tuple(document_ids),
            )
            futures.append((sub_question, self._executor.submit(self.retriever.retrieve, query, context)))
        groups: list[SubQuestionEvidence] = []
        for sub_question, future in futures:
            try:
                result = future.result(timeout=timeout_seconds)
            except TimeoutError:
                groups.append(
                    SubQuestionEvidence(
                        question=sub_question.text,
                        items=(),
                        search_strategy=sub_question.search_strategy,
                        reason="retrieval_timeout",
                    )
                )
                continue
            except Exception:
                groups.append(
                    SubQuestionEvidence(
                        question=sub_question.text,
                        items=(),
                        search_strategy=sub_question.search_strategy,
                        reason="retrieval_error",
                    )
                )
                continue
            reason = None if result.items else "insufficient_evidence"
            groups.append(
                SubQuestionEvidence(
                    question=sub_question.text,
                    items=tuple(result.items),
                    search_strategy=sub_question.search_strategy,
                    reason=reason,
                )
            )
        return MergedRetrieval(groups=tuple(groups))
```

（Python 3.12 中 `concurrent.futures.TimeoutError` 即内建 `TimeoutError`，直接捕获即可。）

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/unit/test_subquestion_retrieval.py -q`
Expected: 5 passed

---

### Task 5: prompts 逐项指令与子问题清单

**Files:**
- Modify: `src/medical_rag/chat/prompts.py`（整体重写）
- Test: `tests/unit/test_chat_prompts.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_chat_prompts.py`：

```python
from medical_rag.chat.prompts import SYSTEM_PROMPT, build_chat_request
from medical_rag.knowledge.contracts import Citation
from medical_rag.query.contracts import Intent, SearchStrategy, SubQuestion
from medical_rag.retrieval.contracts import Evidence, Visibility


def _evidence(evidence_id: str) -> Evidence:
    return Evidence(
        id=evidence_id,
        text="血红蛋白 105 g/L",
        citation=Citation(source_id="src-1", title="检验解读", version="v1", location="第1节"),
        score=1.0,
        visibility=Visibility.PUBLIC,
    )


def _sub(text: str) -> SubQuestion:
    return SubQuestion(
        text=text,
        intent=Intent.ORDINARY,
        search_strategy=SearchStrategy(include_public=True, include_private=True),
    )


def test_single_question_prompt_unchanged():
    request = build_chat_request("你好", [_evidence("ev-1")])
    assert request.prompt == "你好"
    assert request.system_prompt == SYSTEM_PROMPT


def test_compound_prompt_lists_subquestions_and_augments_system():
    subs = (_sub("血红蛋白偏高吗"), _sub("平时饮食注意什么"))
    request = build_chat_request(
        "我血红蛋白偏高吗？还有平时饮食注意什么？",
        [_evidence("ev-1")],
        sub_questions=subs,
    )
    assert request.prompt.startswith("我血红蛋白偏高吗？还有平时饮食注意什么？")
    assert "1. 血红蛋白偏高吗" in request.prompt
    assert "2. 平时饮食注意什么" in request.prompt
    assert request.system_prompt.startswith(SYSTEM_PROMPT)
    assert "逐项回答" in request.system_prompt
    assert request.evidence_hashes  # 证据哈希必须保留
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/test_chat_prompts.py -q`
Expected: FAIL，`TypeError: build_chat_request() got an unexpected keyword argument 'sub_questions'`

- [ ] **Step 3: 重写 prompts.py**

用以下完整内容替换 `src/medical_rag/chat/prompts.py`：

```python
from __future__ import annotations

import hashlib
from uuid import uuid4

from medical_rag.models.contracts import ChatRequest
from medical_rag.query.contracts import SubQuestion
from medical_rag.retrieval.contracts import Evidence


SYSTEM_PROMPT = """你是患者健康教育助手。你只能根据提供的证据回答。
区分患者报告中的客观事实与公共医学知识；不作诊断，不开处方，不建议改变药物或剂量。
使用不确定性表述，不能从证据推断未提供的事实。每个医学结论必须在句末使用对应证据 ID，格式如 [evidence-id]。
如果证据不足，明确说明无法可靠回答，并建议咨询医生。"""

# 仅复合路径（提供了 sub_questions）追加；单查询路径的 system_prompt 一字不变。
MULTI_QUESTION_INSTRUCTION = (
    "\n用户的问题可能包含多个子问题。你必须逐项回答每个子问题；"
    "若某项证据不足，仅对该项明确说明无法可靠回答并建议咨询医生，不影响其他项。"
    "每个子问题的回答段落都必须在句末引用对应证据 [evidence-id]。"
)


def build_chat_request(
    question: str,
    evidence: list[Evidence],
    *,
    sub_questions: tuple[SubQuestion, ...] = (),
    policy_version: str = "v1",
    trace_id: str | None = None,
) -> ChatRequest:
    prompt = question
    system_prompt = SYSTEM_PROMPT
    if sub_questions:
        listed = "\n".join(f"{index}. {sub.text}" for index, sub in enumerate(sub_questions, 1))
        prompt = f"{question}\n\n请分别回答以下子问题：\n{listed}"
        system_prompt = f"{SYSTEM_PROMPT}{MULTI_QUESTION_INSTRUCTION}"
    return ChatRequest(
        prompt=prompt,
        system_prompt=system_prompt,
        evidence=evidence,
        model="gpt-5.5",
        policy_version=policy_version,
        trace_id=trace_id or str(uuid4()),
        purpose="health_education",
        evidence_hashes=[hashlib.sha256(item.text.encode("utf-8")).hexdigest() for item in evidence],
    )
```

注意：证据仍平铺传入（调用方传好**扩展后**的并集——含逐组兄弟上下文，见 Task 6 Step 4d），`[evidence-id]` 标记格式与 `CitationValidator.citation_pattern` 保持一致。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/unit/test_chat_prompts.py -q`
Expected: 2 passed

---

### Task 6: config + composition + orchestrator 编排

**Files:**
- Modify: `src/medical_rag/core/config.py`
- Modify: `src/medical_rag/chat/orchestrator.py`
- Modify: `src/medical_rag/api/composition.py`
- Modify: `.env.example`
- Test: `tests/integration/test_compound_query.py`（新建，本任务 Step 1）

- [ ] **Step 1: 写失败集成测试**

创建 `tests/integration/test_compound_query.py`：

```python
from types import SimpleNamespace
from uuid import uuid4

from medical_rag.auth.policy import AccessContext
from medical_rag.chat.orchestrator import ChatOrchestrator
from medical_rag.chat.subquestion_retrieval import SubQuestionRetriever
from medical_rag.domain.enums import RiskClass
from medical_rag.knowledge.contracts import Citation
from medical_rag.query.decompose import QuestionDecomposer
from medical_rag.query.pipeline import QueryPipeline
from medical_rag.retrieval.contracts import (
    Evidence,
    RetrievalResult,
    SearchQuery,
    SearchRecord,
    Visibility,
)
from medical_rag.safety.service import SafetyAction, SafetyDecision, SafetyService


COMPOUND_QUESTION = "我的体检报告结果怎么样？还有平时饮食该注意什么？"


def _context() -> AccessContext:
    return AccessContext(tenant_id=uuid4(), patient_id=uuid4(), user_id=uuid4())


def _evidence(evidence_id: str, text: str, parent_block_id: str | None = None) -> Evidence:
    return Evidence(
        id=evidence_id,
        text=text,
        citation=Citation(source_id="src-1", title="检验解读指南", version="v2", location="第3节"),
        score=1.0,
        visibility=Visibility.PUBLIC,
        parent_block_id=parent_block_id,
    )


class _FakeRetriever:
    def __init__(self, results: dict[str, list[Evidence]]):
        self.results = results
        self.queries: list[SearchQuery] = []

    def retrieve(self, query: SearchQuery, context):
        self.queries.append(query)
        items = self.results.get(query.text, [])
        return RetrievalResult(
            items=list(items), reason=None if items else "insufficient_evidence", diagnostics={}
        )


class _FakeGateway:
    def __init__(self, text: str):
        self.text = text
        self.requests: list = []

    def chat(self, context, request):
        self.requests.append(request)
        return SimpleNamespace(text=self.text, trace_id="trace-1")


def _orchestrator(retriever: _FakeRetriever, gateway: _FakeGateway, **kwargs) -> ChatOrchestrator:
    return ChatOrchestrator(
        retriever=retriever,
        model_gateway=gateway,
        query_pipeline=QueryPipeline(decomposer=QuestionDecomposer()),
        subquestion_retriever=SubQuestionRetriever(retriever),
        **kwargs,
    )


def test_compound_query_retrieves_each_subquestion_with_own_strategy():
    retriever = _FakeRetriever(
        {
            "我的体检报告结果怎么样？": [_evidence("ev-report", "白细胞 5.2 10^9/L，在参考范围内。")],
            "还有平时饮食该注意什么？": [_evidence("ev-diet", "少油少盐，规律作息。")],
        }
    )
    gateway = _FakeGateway("报告显示白细胞在参考范围内 [ev-report]。饮食建议少油少盐 [ev-diet]。")
    response = _orchestrator(retriever, gateway).answer(_context(), COMPOUND_QUESTION)

    assert response.status == "answered"
    assert len(retriever.queries) == 2
    report_query = next(q for q in retriever.queries if "体检报告" in q.text)
    diet_query = next(q for q in retriever.queries if "饮食" in q.text)
    assert report_query.include_public is False and report_query.include_private is True
    assert diet_query.include_public is True and diet_query.include_private is False
    assert response.sections[0].title == "基于资料的说明"
    assert all(section.title != "未能回答的子问题" for section in response.sections)


def test_partial_coverage_appends_deterministic_uncovered_section():
    retriever = _FakeRetriever({"我的体检报告结果怎么样？": [_evidence("ev-report", "白细胞 5.2 10^9/L。")]})
    gateway = _FakeGateway("报告显示白细胞在参考范围内 [ev-report]。")
    response = _orchestrator(retriever, gateway).answer(_context(), COMPOUND_QUESTION)

    assert response.status == "answered"
    uncovered = [section for section in response.sections if section.title == "未能回答的子问题"]
    assert len(uncovered) == 1
    assert "还有平时饮食该注意什么？" in uncovered[0].text


def test_compound_path_expands_siblings_per_group_strategy():
    """复合路径逐组扩展兄弟上下文：每组用自己的口径查 siblings，再按证据 id 去重。

    锁定 Task 6 Step 4d 的关键行为：composition 已把 `expansion_index` 接进
    生产（Task 9 兄弟上下文扩展是激活的），复合路径不能跳过扩展，否则
    复合查询会丢失单查询拥有的父/子阅读窗口（违背"永不劣于现状"）。
    """

    class _SiblingIndex:
        def __init__(self, siblings: dict[str, list[SearchRecord]]):
            self.siblings = siblings
            self.calls: list[tuple[str, bool, bool]] = []

        def search_by_parent(
            self,
            parent_block_id: str,
            *,
            limit: int = 4,
            tenant_id=None,
            patient_id=None,
            include_public: bool = True,
            include_private: bool = True,
        ):
            self.calls.append((parent_block_id, include_public, include_private))
            return list(self.siblings.get(parent_block_id, []))[:limit]

    def _sibling(sibling_id: str, text: str) -> SearchRecord:
        return SearchRecord(
            id=sibling_id,
            text=text,
            visibility=Visibility.PUBLIC,
            citation=Citation(source_id="src-1", title="检验解读指南", version="v2", location="第3节"),
        )

    index = _SiblingIndex(
        {
            "p-report": [_sibling("sib-report", "报告上下文：血常规总体正常。")],
            "p-diet": [_sibling("sib-diet", "饮食上下文：均衡饮食。")],
        }
    )
    retriever = _FakeRetriever(
        {
            "我的体检报告结果怎么样？": [_evidence("ev-report", "白细胞 5.2 10^9/L。", "p-report")],
            "还有平时饮食该注意什么？": [_evidence("ev-diet", "少油少盐，规律作息。", "p-diet")],
        }
    )
    gateway = _FakeGateway("报告显示白细胞在参考范围内 [ev-report]。饮食建议少油少盐 [ev-diet]。")
    orchestrator = ChatOrchestrator(
        retriever=retriever,
        model_gateway=gateway,
        query_pipeline=QueryPipeline(decomposer=QuestionDecomposer()),
        subquestion_retriever=SubQuestionRetriever(retriever),
        expansion_index=index,
    )
    response = orchestrator.answer(_context(), COMPOUND_QUESTION)

    assert response.status == "answered"
    # 每组用自己的口径查兄弟块：报告组私有-only，饮食组公共-only。
    assert ("p-report", False, True) in index.calls
    assert ("p-diet", True, False) in index.calls
    # 兄弟块进入模型请求的证据集合（sibling id 由命中 id 派生：{hit_id}c{n}）。
    sibling_ids = {item.id for item in gateway.requests[0].evidence}
    assert "ev-reportc1" in sibling_ids
    assert "ev-dietc1" in sibling_ids


def test_all_groups_empty_returns_insufficient_evidence():
    retriever = _FakeRetriever({})
    gateway = _FakeGateway("不应被调用")
    response = _orchestrator(retriever, gateway).answer(_context(), COMPOUND_QUESTION)
    assert response.status == "insufficient_evidence"
    assert gateway.requests == []


def test_fragment_hitting_safety_blocks_whole_answer():
    class _WholePassFragmentBlockSafety(SafetyService):
        def __init__(self) -> None:
            super().__init__()
            self.evaluated: list[str] = []

        def classify(self, question: str) -> SafetyDecision:
            self.evaluated.append(question)
            if question.startswith("还有"):
                return SafetyDecision(
                    risk_class=RiskClass.MEDICATION_CHANGE,
                    action=SafetyAction.SAFE_TEMPLATE,
                    safety_notice="测试用安全提示",
                )
            return SafetyDecision(risk_class=RiskClass.ORDINARY, action=SafetyAction.ALLOW_RETRIEVAL)

    safety = _WholePassFragmentBlockSafety()
    retriever = _FakeRetriever(
        {
            "我的体检报告结果怎么样？": [_evidence("ev-report", "白细胞 5.2 10^9/L。")],
            "还有平时饮食该注意什么？": [_evidence("ev-diet", "少油少盐。")],
        }
    )
    gateway = _FakeGateway("不应被调用")
    response = _orchestrator(retriever, gateway, safety_service=safety).answer(_context(), COMPOUND_QUESTION)

    assert response.status == "safe_template"
    assert any(text.startswith("还有") for text in safety.evaluated), "碎片必须被逐个复检"
    assert gateway.requests == []


def test_decomposition_disabled_keeps_single_query_path():
    retriever = _FakeRetriever({COMPOUND_QUESTION: [_evidence("ev-1", "证据一")]})
    gateway = _FakeGateway("整体回答 [ev-1]。")
    orchestrator = ChatOrchestrator(
        retriever=retriever,
        model_gateway=gateway,
        query_pipeline=QueryPipeline(),  # 无 decomposer = 关闭拆解
    )
    response = orchestrator.answer(_context(), COMPOUND_QUESTION)

    assert response.status == "answered"
    assert len(retriever.queries) == 1
    assert retriever.queries[0].text == COMPOUND_QUESTION
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/integration/test_compound_query.py -q`
Expected: FAIL，`TypeError: ChatOrchestrator.__init__() got an unexpected keyword argument 'subquestion_retriever'`

- [ ] **Step 3: config 增加两个配置项**

`src/medical_rag/core/config.py`：在 `intent_llm_classify_enabled: bool = False` 之后追加：

```python
    query_decomposition_enabled: bool = True
    query_decomposition_max_subquestions: int = Field(default=3, gt=0)
```

`.env.example` 末尾追加：

```ini
QUERY_DECOMPOSITION_ENABLED=true
QUERY_DECOMPOSITION_MAX_SUBQUESTIONS=3
```

- [ ] **Step 4: orchestrator 增加复合编排**

`src/medical_rag/chat/orchestrator.py`，共四处修改：

**4a.** import 区新增：

```python
from medical_rag.chat.subquestion_retrieval import SubQuestionRetriever
```

**4b.** 构造函数：在 `query_pipeline: QueryPipeline | None = None,` 之后加一行参数：

```python
        subquestion_retriever: SubQuestionRetriever | None = None,
```

并在 `self.query_pipeline = query_pipeline or QueryPipeline()` 之后加：

```python
        self.subquestion_retriever = subquestion_retriever
```

**4c.** 替换 `answer()` 中从 `processed: ProcessedQuery = ...` 到 `insufficient_evidence` 返回块的整段代码。原代码：

```python
            processed: ProcessedQuery = self.query_pipeline.process(
                question,
                history=history,
                has_private=has_private,
                access_context=context,
            )
            # 归一化/上下文补全可能引入新的风险表述，对处理后的文本二次分类。
            if processed.text != question:
                recheck = self.safety.classify(processed.text)
                if recheck.action != SafetyAction.ALLOW_RETRIEVAL:
                    return self._safe_response(context, recheck, db=db)

            query = SearchQuery(
                text=processed.text,
                patient_id=context.patient_id,
                top_k=self.top_k,
                include_public=processed.search_strategy.include_public,
                include_private=processed.search_strategy.include_private,
                document_ids=tuple(document_ids or ()),
            )
            try:
                retrieval: RetrievalResult = self.retriever.retrieve(query, context)
            except Exception:
                logger.exception("retrieval failed for patient %s", context.patient_id)
                return self._degraded("检索服务暂时不可用，未生成回答。请稍后重试或咨询医生。")
            if not retrieval.items:
```

替换为：

```python
            processed: ProcessedQuery = self.query_pipeline.process(
                question,
                history=history,
                has_private=has_private,
                access_context=context,
            )
            # 逐子问题安全复检（fail-closed，spec §6.6）：任一碎片命中急症/用药/诊断，
            # 整句走安全模板，绝不部分回答。
            for sub_question in processed.sub_questions:
                recheck = self.safety.classify(sub_question.text)
                if recheck.action != SafetyAction.ALLOW_RETRIEVAL:
                    return self._safe_response(context, recheck, db=db)
            # 归一化/上下文补全可能引入新的风险表述，对处理后的文本二次分类。
            # （复合路径下 processed.text == original，此检查自然跳过。）
            if processed.text != question:
                recheck = self.safety.classify(processed.text)
                if recheck.action != SafetyAction.ALLOW_RETRIEVAL:
                    return self._safe_response(context, recheck, db=db)

            uncovered: list[str] = []
            if processed.sub_questions and self.subquestion_retriever is not None:
                retrieval_budget = (
                    self.total_timeout_seconds
                    - _MIN_MODEL_BUDGET_SECONDS
                    - (perf_counter() - started)
                )
                try:
                    merged = self.subquestion_retriever.retrieve_all(
                        processed.sub_questions,
                        context,
                        timeout_seconds=max(retrieval_budget, 0.0),
                        top_k=self.top_k,
                        document_ids=tuple(document_ids or ()),
                    )
                except Exception:
                    logger.exception("subquestion retrieval failed for patient %s", context.patient_id)
                    return self._degraded("检索服务暂时不可用，未生成回答。请稍后重试或咨询医生。")
                evidence_items = merged.items
                uncovered = [group.question for group in merged.groups if group.reason is not None]
            else:
                query = SearchQuery(
                    text=processed.text,
                    patient_id=context.patient_id,
                    top_k=self.top_k,
                    include_public=processed.search_strategy.include_public,
                    include_private=processed.search_strategy.include_private,
                    document_ids=tuple(document_ids or ()),
                )
                try:
                    retrieval: RetrievalResult = self.retriever.retrieve(query, context)
                except Exception:
                    logger.exception("retrieval failed for patient %s", context.patient_id)
                    return self._degraded("检索服务暂时不可用，未生成回答。请稍后重试或咨询医生。")
                evidence_items = retrieval.items
            if not evidence_items:
```

**4d.** 同函数内，证据扩展按复合/单查询分支化，生成请求带子问题清单，返回附加未覆盖 section。

当前文件中 `if not evidence_items:` 空证据返回块之后、模型调用之前的代码是（**Task 9 已落地**：这里存在超时预算检查与 `_expand_evidence` 兄弟上下文扩展，模型输入与引用校验共用 `evidence_for_model`——不是 `retrieval.items`）。原代码：

```python
            remaining = self.total_timeout_seconds - (perf_counter() - started)
            if remaining <= _MIN_MODEL_BUDGET_SECONDS:
                return self._degraded("请求处理超时，未生成回答。请稍后重试。")
            # Task 9: expand child-block hits with sibling context so the model
            # sees the surrounding parent reading window.  Both the request and
            # the citation validator operate on the same expanded set.
            evidence_for_model = self._expand_evidence(
                retrieval.items,
                context,
                include_public=processed.search_strategy.include_public,
                include_private=processed.search_strategy.include_private,
            )
            request = build_chat_request(question, evidence_for_model, policy_version=self.policy_version)
```

替换为（复合路径逐组用**该组自己的口径**扩展兄弟上下文——`SubQuestionEvidence` 携带 `search_strategy`，不依赖 groups 与 sub_questions 的顺序配对——再按证据 id 去重合并；单查询路径与现状一字不差）：

```python
            remaining = self.total_timeout_seconds - (perf_counter() - started)
            if remaining <= _MIN_MODEL_BUDGET_SECONDS:
                return self._degraded("请求处理超时，未生成回答。请稍后重试。")
            # Task 9: expand child-block hits with sibling context so the model
            # sees the surrounding parent reading window.  Both the request and
            # the citation validator operate on the same expanded set.
            # 复合路径：逐组用该组自己的检索口径扩展（组携带 search_strategy），
            # 再按证据 id 去重合并；兄弟上下文开销随组数同向增长，与证据并集
            # 同向，由 spec §10.4 评测把关。
            if processed.sub_questions and self.subquestion_retriever is not None:
                expanded_items: list = []
                expanded_seen_ids: set[str] = set()
                for group in merged.groups:
                    if not group.items:
                        continue
                    for item in self._expand_evidence(
                        list(group.items),
                        context,
                        include_public=group.search_strategy.include_public,
                        include_private=group.search_strategy.include_private,
                    ):
                        if item.id not in expanded_seen_ids:
                            expanded_seen_ids.add(item.id)
                            expanded_items.append(item)
                evidence_for_model = expanded_items
            else:
                evidence_for_model = self._expand_evidence(
                    retrieval.items,
                    context,
                    include_public=processed.search_strategy.include_public,
                    include_private=processed.search_strategy.include_private,
                )
            request = build_chat_request(
                question,
                evidence_for_model,
                sub_questions=processed.sub_questions,
                policy_version=self.policy_version,
            )
```

要点（执行者不得"简化"）：

- 4c 中的 `evidence_items` 是**未扩展**并集，只用于空证据判断；模型输入与引用校验都用 **`evidence_for_model`（扩展后并集）**，二者必须是同一个变量。
- 引用校验调用**不需要改动**：`self.citations.validate(DraftAnswer(text=..., cited_evidence=cited_ids), evidence_for_model)` 保持原样。
- 紧随其后的 `request = replace(request, timeout_seconds=min(request.timeout_seconds, remaining))`、模型调用与异常处理、`cited_ids` 提取全部不动。
- 该分支行为由 `test_compound_path_expands_siblings_per_group_strategy` 锁定。

原返回块：

```python
            return AnswerResponse(
                status="answered",
                sections=[AnswerSection(title="基于资料的说明", text=validated.text)],
                citations=validated.citations,
                safety_notice=None,
                trace_id=model_response.trace_id,
                session_id=persisted_session_id,
            )
```

替换为：

```python
            sections = [AnswerSection(title="基于资料的说明", text=validated.text)]
            if uncovered:
                sections.append(
                    AnswerSection(
                        title="未能回答的子问题",
                        text="以下子问题未找到足够的可定位医学证据，未能给出可靠回答："
                        + "；".join(uncovered),
                    )
                )
            return AnswerResponse(
                status="answered",
                sections=sections,
                citations=validated.citations,
                safety_notice=None,
                trace_id=model_response.trace_id,
                session_id=persisted_session_id,
            )
```

- [ ] **Step 5: composition 装配**

`src/medical_rag/api/composition.py`：import 区新增：

```python
from medical_rag.chat.subquestion_retrieval import SubQuestionRetriever
from medical_rag.query.decompose import QuestionDecomposer
```

`build_chat_orchestrator` 中，把现有（注意保留末尾的 `expansion_index=search_index`——Task 9 兄弟上下文扩展的生产接线，替换时不得丢失）

```python
    pipeline = QueryPipeline(steps, history_size=settings.query_pipeline_history_size)
    return ChatOrchestrator(
        retriever=retriever,
        model_gateway=gateway,
        session_factory=session_factory,
        policy_version=settings.consent_policy_version,
        query_pipeline=pipeline,
        history_size=settings.query_pipeline_history_size,
        # Wire the same search index as the parent/child sibling-lookup seam so
        # Task-9 context expansion actually runs in production (was previously
        # left at its None default and silently disabled).
        expansion_index=search_index,
    )
```

替换为：

```python
    decomposer = (
        QuestionDecomposer(max_sub_questions=settings.query_decomposition_max_subquestions)
        if settings.query_decomposition_enabled
        else None
    )
    pipeline = QueryPipeline(
        steps,
        history_size=settings.query_pipeline_history_size,
        decomposer=decomposer,
    )
    subquestion_retriever = (
        SubQuestionRetriever(retriever, max_workers=settings.query_decomposition_max_subquestions)
        if decomposer is not None
        else None
    )
    return ChatOrchestrator(
        retriever=retriever,
        model_gateway=gateway,
        session_factory=session_factory,
        policy_version=settings.consent_policy_version,
        query_pipeline=pipeline,
        subquestion_retriever=subquestion_retriever,
        history_size=settings.query_pipeline_history_size,
        # Wire the same search index as the parent/child sibling-lookup seam so
        # Task-9 context expansion actually runs in production (was previously
        # left at its None default and silently disabled).
        expansion_index=search_index,
    )
```

- [ ] **Step 6: 运行集成测试确认通过**

Run: `python -m pytest tests/integration/test_compound_query.py -q`
Expected: 6 passed

---

### Task 7: 全量回归与静态检查

**Files:** 无新改动（验证任务）

- [ ] **Step 1: 全量测试**

Run: `python -m pytest -q`
Expected: 全部 passed，0 failed（重点确认 `tests/integration/test_chat_orchestration.py`、`tests/unit/test_retrieval_service.py`、`tests/unit/test_context_expansion.py`、`tests/integration/test_parent_child_retrieval.py` 等既有测试零回归——默认 `QueryPipeline()` 无 decomposer、`ChatOrchestrator` 无 `subquestion_retriever` 时全部走旧路径，Task 9 证据扩展行为不变）

- [ ] **Step 2: 静态检查**

Run: `python -m ruff check src tests`
Expected: 无新增告警（既有告警不在本计划范围）

- [ ] **Step 3: composition 导入冒烟**

Run: `python -c "from medical_rag.api.composition import build_chat_orchestrator; from medical_rag.chat.subquestion_retrieval import SubQuestionRetriever; from medical_rag.query.decompose import QuestionDecomposer; print('ok')"`
Expected: 输出 `ok`（composition 在应用 lifespan 启动时才执行，冒烟只验证接线可导入）

- [ ] **Step 4: 记录变更清单**

把以下信息写入任务完成凭证（无 Git 仓库，用清单代替提交）：

```text
新增：src/medical_rag/query/decompose.py
新增：src/medical_rag/chat/subquestion_retrieval.py
新增：tests/unit/test_subquestion_contracts.py / test_query_decompose.py /
      test_query_pipeline_compound.py / test_subquestion_retrieval.py / test_chat_prompts.py
新增：tests/integration/test_compound_query.py
修改：src/medical_rag/query/contracts.py（SubQuestion + sub_questions 字段）
修改：src/medical_rag/query/pipeline.py（拆解 + 逐碎片理解）
修改：src/medical_rag/chat/prompts.py（逐项指令 + 子问题清单）
修改：src/medical_rag/chat/orchestrator.py（复合编排 + 逐碎片安全复检 + 未覆盖 section）
修改：src/medical_rag/api/composition.py（按配置装配 decomposer 与 retriever）
修改：src/medical_rag/core/config.py（2 个配置项）、.env.example
```

---

## Coverage and Consistency Review

- spec §3.1（范围 5 点）→ Task 1（契约）、2（拆解）、3（逐碎片理解）、4（并行检索/合并）、5（逐项指令）、6（未覆盖 section 与编排）
- spec §4.1（拆解先于补全）→ Task 3 实现 + `test_completion_anchor_lands_in_the_right_fragment`
- spec §4.2（降级不变式）→ Task 2 规则 5/6、Task 3 `decomposer=None`、Task 4 异常降级、Task 6 开关关闭测试
- spec §5/§6.1-6.6 → 各 Task 一一对应；§6.4 组级超时 → Task 4 `timeout_seconds` + Task 6 `retrieval_budget`
- spec §8（配置）→ Task 6 Step 3
- spec §10.3 集成场景 → Task 6 Step 1（跨意图口径/部分覆盖/全空/碎片安全拦截/兄弟扩展逐组口径/开关关闭）+ Task 4 计时测试（P95 并行性）
- spec §10.4 golden 集评测 → 不在本计划内（需要真实脱敏语料，属部署前评估步骤，见 spec §10.4）
- spec §11 非目标 → 计划未实现 LLM 拆解、多跳、独立生成，未改动 ChatRequest/adapter/RetrievalService
- 类型一致性：`SubQuestion(text, intent, search_strategy)`、`SubQuestionEvidence(question, items, search_strategy, reason)`、`retrieve_all(sub_questions, context, *, timeout_seconds, top_k, document_ids)`、`build_chat_request(..., sub_questions=(), ...)`、`MergedRetrieval.groups/items` 在各任务间引用一致

## Plan Self-Review

- 占位符扫描：无 TBD/TODO/“适当处理”；所有代码步骤均给出完整代码
- `used_llm` 聚合、`document_ids` 透传、组级超时预算均已落实为具体代码
- 既有测试零回归路径已论证：默认 `decomposer=None` + `subquestion_retriever=None` → 单查询路径
- 基线核对（2026-09-16 修订）：Task 2 拆解器去重折叠语义与测试对齐（重复碎片折叠到 1 个时返回该碎片，其余单碎片情形返回原句）；Task 6 Step 4/5 的补丁基线已按当前 orchestrator/composition 重写——Task 9 超时预算与 `_expand_evidence` 保留，复合路径逐组扩展兄弟上下文，`build_chat_request` 与引用校验统一用 `evidence_for_model`，composition 替换保留 `expansion_index=search_index`
