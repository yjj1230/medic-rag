# Document Quality and Knowledge Indexing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 PDF、DOCX、XLS/XLSX 和 OCR 文档接入一条可追溯、可复核、可重建的清洗—解析—分块—证据—质量门禁—索引流水线。

**Architecture:** 在现有 Parser、EvidenceUnit、MedicalFact、IndexJob 和 Public/Private IndexWriter 之上增加稳定的 `ParsedBlock`、`CleanedDocument`、`EvidenceUnitDraft` 和 `QualityDecision` 契约。原文永不覆盖，清洗和分块结果以版本化的增量字段保存；质量不通过的结果仍可保存，但设置 `retrieval_eligible=False`，不进入高可信检索。IndexJob/Outbox 继续承担异步、幂等、可重试的 OpenSearch 写入。

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, pytest, pypdf, python-docx, openpyxl/xlrd, Pillow, 现有 EmbeddingPipeline、IndexJob、OutboxService 和检索 Writer 接口。

**Spec:** `docs/superpowers/specs/2026-09-15-document-quality-indexing-design.md`

## Global Constraints

- 保留原始文件和 Parser 原始文本；`DocumentCleaner` 只能产生新结果，不能覆盖原文。
- 所有可检索 EvidenceUnit 必须能回溯到页码、Sheet/Cell、OCR 坐标或明确的 source anchor。
- `NEEDS_REVIEW` 结果必须保存，但 `retrieval_eligible=False`，不得进入高可信检索结果。
- 公共知识索引和患者私有索引继续使用分离的权限路径，不能把患者字段写入公共索引。
- P0/P0.5 不引入 Agent、查询改写、会话记忆、LLM 改写原文或替换原始医学证据。
- parser、cleaner、layout、chunker、extractor、embedding、index 版本必须进入持久化记录和幂等键。
- 所有失败阶段必须保留明确状态、错误码、attempts 和可重试信息。
- 父子块：引用始终指向 child 自身的页码/Sheet/Cell/坐标；上下文扩展不得超过预算（≤2 个 parent、每 parent ≤4 个兄弟 chunk、≤30% 模型上下文），扩展块共享命中块的 evidence id 前缀（`e1`/`e1c1`）。
- 当前工作区没有 Git 仓库；实施时不创建提交，使用测试结果和变更清单作为任务完成凭证。

---

### Task 1: 固化解析、清洗、分块和质量契约

**Files:**
- Modify: `src/medical_rag/parsing/contracts.py`
- Create: `src/medical_rag/parsing/quality_contracts.py`
- Modify: `src/medical_rag/knowledge/contracts.py`
- Test: `tests/contract/test_document_quality_contracts.py`

**Interfaces:**
- Consumes: 现有 `ParsedSegment`、`ParsedTable`、`ParsedDocument`、`EvidenceUnitDraft`。
- Produces: `ParsedBlock`、`SourceOffset`、`CleanedDocument`、`QualityDecision`、`SemanticChunkDraft`；保留旧类型名以兼容现有测试和调用方。
- Resolution note: `SemanticChunkDraft` is intentionally NOT a separate type — the extended `EvidenceUnitDraft` IS the semantic chunk draft (Task 4's interface returns `list[EvidenceUnitDraft]` reading exactly the fields added here).

- [ ] **Step 1: Write the failing contract tests**

```python
from medical_rag.parsing.contracts import (
    CleanedDocument, ParsedBlock, SourceOffset,
)
from medical_rag.parsing.quality_contracts import QualityDecision


def test_parsed_block_keeps_layout_and_source_identity() -> None:
    block = ParsedBlock(
        block_id="p1-b1",
        block_type="paragraph",
        raw_text="血红蛋白 105 g/L",
        page_number=1,
        source_anchor="p1:x10:y20:w100:h30",
        reading_order=1,
        section_path=("检验结果",),
        language="zh-CN",
        confidence=0.98,
    )
    assert block.raw_text == "血红蛋白 105 g/L"
    assert block.source_anchor == "p1:x10:y20:w100:h30"


def test_cleaned_document_keeps_raw_and_clean_blocks() -> None:
    block = ParsedBlock("b1", "paragraph", "Ａ  1", reading_order=1)
    result = CleanedDocument(
        raw_blocks=(block,),
        cleaned_blocks=(block,),
        source_offsets=(SourceOffset("b1", 0, 4, 0, 3),),
        cleaning_flags=("fullwidth_normalized",),
        cleaner_version="cleaner-v1",
    )
    assert result.raw_blocks[0].raw_text == "Ａ  1"
    assert result.cleaning_flags == ("fullwidth_normalized",)


def test_quality_decision_review_is_not_retrieval_eligible() -> None:
    decision = QualityDecision.review(("low_anchor_coverage",), metrics={"anchor_coverage": 0.5})
    assert decision.status == "NEEDS_REVIEW"
    assert decision.retrieval_eligible is False
```

- [ ] **Step 2: Run the contract tests and verify they fail**

Run: `python -m pytest tests/contract/test_document_quality_contracts.py -q`

Expected: FAIL because the new contract classes and factory method do not yet exist.

- [ ] **Step 3: Implement the additive contracts**

Add these exact dataclasses and result shape:

```python
@dataclass(frozen=True, slots=True)
class SourceOffset:
    block_id: str
    raw_start: int
    raw_end: int
    clean_start: int
    clean_end: int


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    block_id: str
    block_type: Literal["heading", "paragraph", "list", "table", "image"]
    raw_text: str
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    section_path: tuple[str, ...] = ()
    reading_order: int = 0
    language: str = "und"
    confidence: float = 1.0
    source_anchor: str | None = None


@dataclass(frozen=True, slots=True)
class CleanedDocument:
    raw_blocks: tuple[ParsedBlock, ...]
    cleaned_blocks: tuple[ParsedBlock, ...]
    source_offsets: tuple[SourceOffset, ...]
    cleaning_flags: tuple[str, ...]
    cleaner_version: str
```

`ParsedSegment` remains available as a compatibility type. `EvidenceUnitDraft` gains `raw_text`, `clean_text`, `section_path`, `parent_block_id`, `chunk_index`, `parser_version`, `cleaner_version`, `chunker_version`, `quality_flags`, and `retrieval_eligible`, with defaults that preserve current constructors.

- [ ] **Step 4: Run the contract tests and existing contract suite**

Run: `python -m pytest tests/contract/test_document_quality_contracts.py tests/contract/test_index_record_contract.py -q`

Expected: PASS, with no regression in `IndexRecord` serialization.

- [ ] **Step 5: Record the API compatibility checklist**

Verify that `ParsedDocument.segments`, `ParsedDocument.tables`, `KnowledgeChunkDraft`, `PublicIndexRecord`, and `PrivateIndexRecord` remain constructible with their current parameters. Do not remove or rename existing fields in this task.

### Task 2: Implement independent cleaning, layout normalization, and TextSourceParser

**Files:**
- Create: `src/medical_rag/parsing/cleaner.py`
- Create: `src/medical_rag/parsing/layout.py`
- Create: `src/medical_rag/parsing/text_source.py`
- Modify: `src/medical_rag/parsing/adapters.py`
- Test: `tests/unit/test_document_cleaner.py`
- Test: `tests/unit/test_layout_normalizer.py`
- Test: `tests/unit/test_text_source_parser.py`

**Interfaces:**
- Consumes: `ParsedDocument` or `tuple[ParsedBlock, ...]`.
- Produces: `CleanedDocument` through `DocumentCleaner.clean(parsed_document)` and normalized blocks through `LayoutNormalizer.normalize(parsed_document)`.

- [ ] **Step 1: Write failing cleaner tests**

```python
from medical_rag.parsing.cleaner import DocumentCleaner
from medical_rag.parsing.contracts import ParsedBlock, ParsedDocument


def test_cleaner_normalizes_width_spaces_without_losing_raw_text() -> None:
    parsed = ParsedDocument(
        file_type=".pdf",
        raw_text="Ａ  血红蛋白\u00a0105 g/L",
        segments=(),
    )
    result = DocumentCleaner().clean(parsed)
    assert result.raw_blocks[0].raw_text == "Ａ  血红蛋白\u00a0105 g/L"
    assert "A 血红蛋白 105 g/L" in result.cleaned_blocks[0].raw_text
    assert "fullwidth_normalized" in result.cleaning_flags


def test_cleaner_marks_repeated_header_and_ocr_noise_instead_of_silently_deleting() -> None:
    parsed = ParsedDocument(
        file_type="image",
        raw_text="医院报告\n医院报告\n血红蛋白 105 g/L",
        segments=(),
    )
    result = DocumentCleaner().clean(parsed)
    assert "repeated_template_text" in result.cleaning_flags
    assert "ocr_noise_checked" in result.cleaning_flags
```

- [ ] **Step 2: Implement deterministic cleaning rules**

Implement `DocumentCleaner(version="cleaner-v1")` with these private operations, applied in order: Unicode NFKC normalization; invisible-character removal; whitespace normalization; OCR line-join only when the preceding line does not end in sentence punctuation and the combined line is not a table row; repeated header/footer detection by normalized frequency; table delimiter normalization. Every applied operation adds a stable flag. Uncertain text remains in the cleaned block and is marked with a flag.

Implement `LayoutNormalizer.normalize` to map legacy `ParsedSegment` and `ParsedTable` into `ParsedBlock` values with deterministic `block_id`, `reading_order`, `block_type`, `section_path`, and `source_anchor`. The normalizer must not infer a source anchor when none exists.

- [ ] **Step 3: Implement TextSourceParser for the public knowledge chain**

```python
from medical_rag.parsing.text_source import TextSourceParser


def test_text_source_parser_wraps_markdown_into_blocks_without_ocr() -> None:
    blocks = TextSourceParser().parse("## 检验结果\n\n血红蛋白 105 g/L。")
    assert blocks[0].block_type == "heading"
    assert blocks[0].section_path == ("检验结果",)
    assert blocks[1].block_type == "paragraph"
    assert all(block.page_number is None for block in blocks)
    assert all(block.source_anchor is None for block in blocks)
```

`TextSourceParser.parse(text) -> tuple[ParsedBlock, ...]` reuses the existing markdown heading regex to build `section_path` and emits heading/paragraph/table blocks with deterministic `block_id` and `reading_order`. It performs no OCR, no bbox, and no page numbers — the knowledge chain shares the `ParsedBlock`/cleaning contract with the patient chain and forks only at the Parser layer (设计 §3). The normalizer's fail-closed anchor rule is unaffected: no anchor is invented here.

- [ ] **Step 4: Run unit tests**

Run: `python -m pytest tests/unit/test_document_cleaner.py tests/unit/test_layout_normalizer.py tests/unit/test_text_source_parser.py -q`

Expected: PASS.

- [ ] **Step 5: Run existing parser tests**

Run: `python -m pytest tests/unit/test_parsing_service.py -q`

Expected: PASS; existing `ParsedSegment` behavior and source anchors remain unchanged.

### Task 3: Upgrade PDF, DOCX, XLS/XLSX and OCR adapters to emit layout blocks

**Files:**
- Modify: `src/medical_rag/parsing/adapters.py`
- Modify: `src/medical_rag/parsing/service.py`
- Create: `docs/superpowers/notes/excel-parser-p1-gap-checklist.md`
- Test: `tests/contract/test_parser_adapters.py`
- Test: `tests/unit/test_parsing_service.py`

**Interfaces:**
- Consumes: file bytes and existing parser adapter protocols.
- Produces: `ParsedDocument.blocks` containing `ParsedBlock` values while preserving `segments`, `tables`, `raw_text`, and existing parser version fields.

- [ ] **Step 1: Add failing adapter tests for source coverage**

Add assertions that native PDF pages produce one block per non-empty page with `page_number` and `source_anchor`; DOCX headings preserve `section_path`; Excel blocks preserve `sheet_name` and exact `cell_range`; OCR blocks preserve `bbox` and confidence.

```python
def test_excel_parser_preserves_sheet_range_and_cell_types(workbook_bytes):
    result = ExcelParser().parse(workbook_bytes, ".xlsx")
    table = result.tables[0]
    assert table.sheet_name == "检验结果"
    assert table.cell_range == "A1:C2"
    assert result.blocks[0].source_anchor == "sheet:检验结果!A1:C2"
```

- [ ] **Step 2: Implement block emission without breaking legacy fields**

For PDF, emit page blocks and retain `p{page}:text` fallback anchors. For DOCX, classify heading styles and carry the current heading stack into `section_path`. For Excel, detect the used range, preserve original date/number text for the block, and store merged-cell/header metadata in the block flags. For OCR, copy backend coordinates and confidence into `bbox` and `confidence`.

Keep the current deterministic fixtures and parser protocols so tests can use in-memory adapters. Set parser versions explicitly: `pdf-parser-v2`, `docx-parser-v2`, `spreadsheet-parser-v2`, and `ocr-parser-v2`.

- [ ] **Step 3: Add per-page OCR fallback hook**

Add `should_ocr_page(blocks: tuple[ParsedBlock, ...], page_number: int) -> bool` and invoke the OCR adapter only for pages with no usable native text or a text coverage below the configured parser policy. Do not OCR the whole PDF when only one page is empty.

- [ ] **Step 4: Run parser contract and unit suites**

Run: `python -m pytest tests/contract/test_parser_adapters.py tests/unit/test_parsing_service.py -q`

Expected: PASS, including existing PDF, DOCX, XLSX and OCR tests.

- [ ] **Step 5: Record the Excel P1 gap checklist**

Audit the upgraded `ExcelParser` against the design targets (multi-level headers, merged-cell semantics, typed date/number cells) and write the remaining gaps with concrete failing cases into `docs/superpowers/notes/excel-parser-p1-gap-checklist.md`. This document is the P1.2 backlog input, not work for this plan (设计 P0.5b 第 6 项).

### Task 4: Replace character-only chunking with semantic and table-aware chunking

**Files:**
- Modify: `src/medical_rag/knowledge/chunker.py`
- Modify: `src/medical_rag/knowledge/contracts.py`
- Modify: `src/medical_rag/parsing/contracts.py`
- Test: `tests/unit/test_knowledge_chunker.py`
- Create: `tests/unit/test_semantic_chunker.py`

**Interfaces:**
- Consumes: `CleanedDocument` and `ParsedBlock` values.
- Produces: `SemanticChunker.chunk(cleaned_document) -> list[EvidenceUnitDraft]`; legacy `KnowledgeChunker.split(SourceDocument)` remains as a compatibility wrapper.

- [ ] **Step 1: Write failing semantic chunk tests**

```python
def test_semantic_chunker_keeps_heading_and_section_path() -> None:
    document = cleaned_document_from_blocks(
        heading("检验结果"), paragraph("血红蛋白 105 g/L。"), paragraph("白细胞 5.2 10^9/L。")
    )
    chunks = SemanticChunker(target_tokens=400, overlap_ratio=0.12).chunk(document)
    assert chunks[0].section_path == ("检验结果",)
    assert chunks[0].chunk_index == 0


def test_table_chunk_repeats_headers_and_preserves_row_range() -> None:
    document = cleaned_document_from_blocks(table_block(headers=("项目", "结果"), rows=25))
    chunks = SemanticChunker(table_rows_per_chunk=10).chunk(document)
    assert len(chunks) == 3
    assert all("项目" in chunk.clean_text for chunk in chunks)
    assert chunks[0].cell_range == "A1:B11"


def test_long_sentence_split_records_parent_block_id_and_overlap() -> None:
    chunks = SemanticChunker(target_tokens=20, overlap_ratio=0.1).chunk(long_document())
    assert all(chunk.parent_block_id is not None for chunk in chunks)
    assert chunks[1].clean_text.split()[0] in chunks[0].clean_text
```

- [ ] **Step 2: Implement sentence/heading/table-aware chunking**

Use `target_tokens=400`, `min_tokens=80`, `max_tokens=600`, and `overlap_ratio=0.12` as configuration defaults; calculate token estimates through the existing language-neutral character/token estimator rather than hard-coding character slices. 中文按 1 token ≈ 1.5 字符折算（初始 450～900 字符），黄金集阶段校准（设计 §7）。 Split in this order: heading boundary, paragraph boundary, sentence boundary, then hard split as a last resort. Every hard split gets `quality_flags=("hard_split",)`.

For tables, repeat the normalized header in every child chunk, group 10 rows by default, preserve `cell_range`, and emit one extra fact-oriented chunk for rows matching configured medical measurement names. Parent chunks retain the full section/table text; child chunks carry `parent_block_id`.

- [ ] **Step 3: Preserve legacy `KnowledgeChunker` behavior through the shared chain**

Implement `KnowledgeChunker.split` by routing `SourceDocument.content` through `TextSourceParser` → `DocumentCleaner` → `SemanticChunker` — the public knowledge chain shares the same cleaning and chunking contracts as the patient chain and forks only at the Parser layer (设计 §3，验收第 10 条); map the resulting drafts back to `KnowledgeChunkDraft` without changing `content_hash`, `location`, `section`, or URL semantics. Add a contract test asserting knowledge-chain output satisfies the same `ParsedBlock` contract used by the patient chain.

- [ ] **Step 4: Run chunker tests and retrieval regressions**

Run: `python -m pytest tests/unit/test_knowledge_chunker.py tests/unit/test_semantic_chunker.py tests/integration/test_knowledge_ingest.py -q`

Expected: PASS, with table headers retained and public knowledge ingestion still idempotent.

### Task 5: Expand evidence and medical-fact persistence with provenance and review state

**Files:**
- Modify: `src/medical_rag/domain/models.py`
- Create: `alembic/versions/0005_add_document_quality_metadata.py`
- Modify: `src/medical_rag/parsing/service.py`
- Modify: `src/medical_rag/retrieval/indexing.py`
- Test: `tests/contract/test_index_record_contract.py`
- Test: `tests/integration/test_document_lifecycle.py`
- Test: `tests/integration/test_private_indexing.py`

**Interfaces:**
- Consumes: `EvidenceUnitDraft`, `MedicalFactDraft`, `QualityDecision`, and `IndexJob`.
- Produces: database records with `raw_text`, `clean_text`, `section_path`, `parent_block_id`, `chunk_index`, `quality_flags`, `retrieval_eligible`, `cleaner_version`, `chunker_version`, `extractor_version`, and fact review status.

- [ ] **Step 1: Add migration tests for additive metadata**

Create a SQLite schema test that inserts one EvidenceUnit and one MedicalFact with all new fields, reloads them, and verifies JSON flags and version strings survive round-trip serialization.

- [ ] **Step 2: Add additive ORM columns and compatibility mapping**

Add nullable columns to `EvidenceUnit`: `raw_text`, `clean_text`, `section_path` JSON, `parent_block_id`, `chunk_index`, `parser_version`, `cleaner_version`, `chunker_version`, `quality_flags` JSON, and `retrieval_eligible` boolean defaulting to `True`. Keep `text` as the retrieval text and set it to `clean_text` for new rows.

Add nullable columns to `MedicalFact`: `canonical_name`, `raw_name`, `value_number`, `unit_raw`, `unit_normalized`, `reference_low`, `reference_high`, `extractor_version`, and `review_status` defaulting to `"ACCEPTED"` only for facts meeting the quality gate. Existing `name`, `value`, `unit`, and `reference_range` remain populated for backward compatibility.

Add matching fields to `IndexRecord`, `PrivateIndexRecord`, and `EvidenceUnitDraft.to_dict()`; serialization must remain JSON-safe.

- [ ] **Step 3: Update persistence flow to keep raw and cleaned values**

In `ParsingService.process`, persist `raw_text` from the parsed block, `clean_text` from the cleaned block, source metadata, versions, and quality flags. Set `retrieval_eligible=False` when the parse result or quality decision is `NEEDS_REVIEW` or `FAIL`. Never discard an EvidenceUnit solely because fact extraction failed.

- [ ] **Step 4: Run lifecycle and indexing tests**

Run: `python -m pytest tests/contract/test_index_record_contract.py tests/integration/test_document_lifecycle.py tests/integration/test_private_indexing.py -q`

Expected: PASS; existing ACL, deletion, source-anchor, and idempotency behavior remains intact.

### Task 6: Implement QualityGate and explicit `NEEDS_REVIEW` gating

**Files:**
- Create: `src/medical_rag/quality/__init__.py`
- Create: `src/medical_rag/quality/gate.py`
- Modify: `src/medical_rag/parsing/service.py`
- Modify: `src/medical_rag/domain/enums.py`
- Test: `tests/unit/test_quality_gate.py`
- Test: `tests/integration/test_p0_rollout.py`

**Interfaces:**
- Consumes: parsed/cleaned blocks, EvidenceUnitDrafts, MedicalFactDrafts, and a `QualityPolicy`.
- Produces: `QualityDecision.evaluate(...)` with `status` in `PASS | NEEDS_REVIEW | FAIL`, metrics, reasons, and `retrieval_eligible`.

- [ ] **Step 1: Write failing gate tests**

```python
def test_missing_anchor_is_review_and_not_indexable() -> None:
    decision = QualityGate(QualityPolicy()).evaluate(
        document=cleaned_document_without_anchor(), evidence=[evidence_without_anchor()], facts=[]
    )
    assert decision.status == "NEEDS_REVIEW"
    assert decision.retrieval_eligible is False
    assert "missing_source_anchor" in decision.reasons


def test_empty_document_fails_without_creating_index_job() -> None:
    decision = QualityGate(QualityPolicy()).evaluate(empty_document(), [], [])
    assert decision.status == "FAIL"
    assert decision.retrieval_eligible is False
```

- [ ] **Step 2: Implement metric calculation and policy loading**

Implement `QualityPolicy` with named thresholds for `min_text_coverage`, `min_non_empty_block_ratio`, `min_anchor_coverage`, `max_abnormal_character_ratio`, `max_duplicate_block_ratio`, `min_table_column_consistency`, and `min_ocr_confidence`. Load defaults from one policy object; do not scatter numeric literals across Parser or IndexJob code.

Implement metrics for text coverage, non-empty blocks, source-anchor completeness, OCR confidence, abnormal-character ratio, duplicate-block ratio, table-column consistency, and fact coverage. Return the complete metrics dictionary for observability. 默认值是黄金集校准前的保守区间：合成/脱敏黄金集先行，前 50 份真实文档人工复核后再收紧（设计 §9 阈值冷启动）。

- [ ] **Step 3: Connect gate status to ParseJob and IndexJob creation**

`PASS` sets the document/version to ready and permits IndexJob creation. `NEEDS_REVIEW` sets ParseJob and DocumentVersion to `NEEDS_REVIEW`, persists EvidenceUnit rows with `retrieval_eligible=False`, and does not create a searchable IndexJob. `FAIL` sets the parse job to failed with a stable error code and leaves the original object available for reprocessing.

- [ ] **Step 4: Run gate and rollout tests**

Run: `python -m pytest tests/unit/test_quality_gate.py tests/integration/test_p0_rollout.py tests/integration/test_index_outbox.py -q`

Expected: PASS; retry, lease, duplicate delivery, and atomic Outbox behavior continue to work.

### Task 7: Expose document quality status and failure reason codes (pilot operational exit)

**Files:**
- Modify: `src/medical_rag/api/routes/documents.py`
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/stores/documents.ts`
- Modify: `frontend/src/views/DocumentsPage.vue`
- Test: `tests/integration/test_documents_quality_status.py`

**Interfaces:**
- Consumes: `DocumentStatus` / ParseJob `error_code` / version `NEEDS_REVIEW` states produced by Task 6.
- Produces: `GET /v1/documents` items carry a derived `quality_status` (`READY | NEEDS_REVIEW | FAILED_PARSE`) and `failure_reason_code`; the documents page renders a status badge and keeps upload as the re-upload path for failed documents (设计 §9 运营出口，验收第 9 条).

- [ ] **Step 1: Write the failing API test**

```python
def test_documents_list_exposes_quality_status_and_reason_code(client_with_patient):
    items = client_with_patient.get("/v1/documents").json()["items"]
    review = next(i for i in items if i["filename"] == "review.pdf")
    failed = next(i for i in items if i["filename"] == "broken.pdf")
    ready = next(i for i in items if i["filename"] == "ok.pdf")
    assert review["quality_status"] == "NEEDS_REVIEW"
    assert review["failure_reason_code"] == "missing_source_anchor"
    assert failed["quality_status"] == "FAILED_PARSE"
    assert failed["failure_reason_code"] == "empty_document"
    assert ready["quality_status"] == "READY"
    assert ready["failure_reason_code"] is None
```

- [ ] **Step 2: Implement the derived status mapping**

`quality_status` is derived at read time, not stored: `FAILED_PARSE` when the latest ParseJob failed or the document has no usable version; `NEEDS_REVIEW` when any active version is `NEEDS_REVIEW` or has `retrieval_eligible=False`; otherwise `READY`. Expose the most recent stable ParseJob/DocumentVersion `error_code` as `failure_reason_code` (`None` for READY). Additive response fields only — existing consumers must keep working without changes.

- [ ] **Step 3: Show status and re-upload entry in the documents page**

`DocumentsPage` renders a per-document badge (READY=绿 / NEEDS_REVIEW=黄 / FAILED_PARSE=红) with the reason-code text. The existing upload button is the re-upload path for failed documents; do not build a separate review workflow (P1.5 scope).

- [ ] **Step 4: Run API and frontend checks**

Run: `python -m pytest tests/integration/test_documents_quality_status.py -q`

Run: `cd frontend && npx vue-tsc --noEmit`

Expected: PASS.

### Task 8: Version the complete indexing path and preserve Outbox idempotency

**Files:**
- Modify: `src/medical_rag/domain/models.py`
- Modify: `src/medical_rag/indexing/outbox.py`
- Modify: `src/medical_rag/retrieval/indexing.py`
- Modify: `src/medical_rag/knowledge/ingest_service.py`
- Modify: `src/medical_rag/retrieval/contracts.py`
- Test: `tests/integration/test_index_outbox.py`
- Test: `tests/integration/test_private_indexing.py`
- Test: `tests/integration/test_knowledge_ingest.py`

**Interfaces:**
- Consumes: quality-approved EvidenceUnit rows and `VersionBundle` values.
- Produces: index records and IndexJobs carrying `parser_version`, `cleaner_version`, `chunker_version`, `extractor_version`, `embedding_model`, `embedding_dimensions`, and `index_version`.

- [ ] **Step 1: Add a version bundle and deterministic idempotency identity**

Define:

```python
@dataclass(frozen=True, slots=True)
class VersionBundle:
    parser_version: str
    cleaner_version: str
    chunker_version: str
    extractor_version: str
    embedding_model: str
    embedding_dimensions: int | None
    index_version: str
```

Use `(document_version_id, evidence_unit_id, parser_version, cleaner_version, chunker_version, extractor_version, embedding_model, index_version)` as the logical rebuild identity. Preserve the current four-field database uniqueness key until a migration adds the new columns and unique index.

- [ ] **Step 2: Extend Outbox payload and IndexJob creation atomically**

`OutboxService.create_index_event` must accept `VersionBundle`, include all version fields in the JSON payload, and continue to create the OutboxEvent and IndexJob in one transaction. Repeated delivery with the same identity must return the existing job and event.

- [ ] **Step 3: Gate Writer input and embedding metadata**

Before embedding, reject records with `retrieval_eligible=False` or missing source anchors for private evidence. On successful write, persist embedding dimensions and all version fields into the `IndexRecord` sent to the Writer. Public ingest must use the same version bundle but must never include private ACL fields.

- [ ] **Step 4: Run Outbox and Writer regression tests**

Run: `python -m pytest tests/integration/test_index_outbox.py tests/integration/test_private_indexing.py tests/integration/test_knowledge_ingest.py -q`

Expected: PASS, including retry backoff, lease reclaim, duplicate delivery, embedding metadata, and public/private separation.

### Task 9: Retrieval-side parent/child contract and context expansion

**Files:**
- Modify: `src/medical_rag/retrieval/contracts.py`
- Modify: `src/medical_rag/retrieval/indexing.py`
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Modify: `src/medical_rag/retrieval/service.py`
- Modify: `src/medical_rag/chat/orchestrator.py`
- Test: `tests/unit/test_context_expansion.py`
- Test: `tests/integration/test_parent_child_retrieval.py`

**Interfaces:**
- Consumes: EvidenceUnit / IndexRecord `parent_block_id` from Tasks 5 and 8; retrieval hits.
- Produces: `SearchRecord.parent_block_id`, sibling expansion within budget, shared evidence-id prefixes (`e1`, `e1c1`), citations still anchored to the child (设计 §7 父子块检索契约).

- [ ] **Step 1: Write failing expansion tests**

```python
from collections import Counter


def test_expansion_respects_parent_and_context_budget() -> None:
    evidence = [hit(child_block("p1", i)) for i in range(3)] + [hit(child_block("p2", 0))]
    expanded = expand_with_parents(evidence, index=fake_index_with_siblings(), context_token_budget=2000)
    parents = {item.parent_block_id for item in expanded}
    assert len(parents) <= 2
    per_parent = Counter(item.parent_block_id for item in expanded)
    assert all(count <= 5 for count in per_parent.values())  # 1 hit + 4 siblings
    assert total_tokens(expanded) <= 2000


def test_expanded_siblings_share_evidence_id_prefix_and_citations_stay_child_anchored() -> None:
    evidence = [hit(child_block("p1", 0))]
    expanded = expand_with_parents(evidence, index=fake_index_with_siblings(), context_token_budget=2000)
    ids = [item.id for item in expanded]
    assert ids[0] == "e1"
    assert all(sid.startswith("e1c") for sid in ids[1:])
    assert all(item.citation.location == child_location(item) for item in expanded)
```

- [ ] **Step 2: Implement the contract fields and sibling lookup**

`SearchRecord` and `Evidence` gain `parent_block_id: str | None`; the private writer and both index adapters persist and return it. Add a sibling lookup seam on the index (`search_by_parent(parent_block_id, limit)`), used by `expand_with_parents(evidence, index, context_token_budget)`: group hits by parent, expand up to 2 parents and 4 siblings each, deduplicate, order by reading order, and stop when the token budget (default 30% of the model context, configurable) is reached. Hits without `parent_block_id` pass through unchanged.

- [ ] **Step 3: Feed expanded evidence through the prompt with stable prefixes**

The orchestrator passes the expanded set in the same evidence format; expanded siblings carry `e{n}c{m}` ids derived from the hit id. `CitationValidator` sees every displayed id as existing evidence, and citations returned to the patient keep the child's page / Sheet / Cell location — the parent is context only, never a citation source.

- [ ] **Step 4: Run retrieval and chat regressions**

Run: `python -m pytest tests/unit/test_context_expansion.py tests/integration/test_parent_child_retrieval.py tests/integration/test_chat_orchestration.py tests/integration/test_private_indexing.py -q`

Expected: PASS; ACL filtering and citation behavior unchanged for hits without `parent_block_id`.

### Task 10: Build a document-type golden set and parsing quality evaluation

**Files:**
- Create: `tests/golden/README.md`
- Create: `tests/golden/fixtures/pdf_native.json`
- Create: `tests/golden/fixtures/pdf_scanned.json`
- Create: `tests/golden/fixtures/docx_sections.json`
- Create: `tests/golden/fixtures/xlsx_multilevel_headers.json`
- Create: `tests/golden/fixtures/ocr_mixed_page.json`
- Create: `tests/eval/test_document_quality.py`
- Modify: `tests/eval/test_rag_quality.py`

**Interfaces:**
- Consumes: parser, cleaner, semantic chunker, fact extractor, and QualityGate outputs.
- Produces: deterministic metrics and a baseline report suitable for release gating.

- [ ] **Step 1: Define golden fixture schema**

Each fixture must contain `file_type`, `expected_blocks`, `expected_anchors`, `expected_sections`, `expected_tables`, `expected_facts`, and `expected_review_reasons`. Include native PDF, scanned PDF, DOCX heading/table, XLSX merged/multilevel headers, OCR coordinates, Chinese/English mixed text, double-column ordering, and a complex medical table.

- [ ] **Step 2: Add deterministic evaluation tests**

```python
@pytest.mark.parametrize("fixture_path", GOLDEN_FIXTURES)
def test_golden_document_meets_anchor_and_structure_expectations(fixture_path):
    result = run_golden_fixture(fixture_path)
    assert result.anchor_accuracy >= 0.99
    assert result.text_coverage >= 0.95
    assert result.table_header_retention is True
```

Use fixture-specific expected values rather than asserting one global threshold for all document types. Store the measured baseline for Chunk Recall@K, MRR, citation accuracy, permission violations, deletion residuals, and P95 latency.

- [ ] **Step 3: Add release gate command**

Expose `python -m medical_rag.cli evaluate-document-quality --fixtures tests/golden/fixtures` and return exit code 1 when any required metric is below baseline or permission/deletion safety checks are non-zero.

- [ ] **Step 4: Run evaluation and existing RAG quality tests**

Run: `python -m pytest tests/eval/test_document_quality.py tests/eval/test_rag_quality.py -q`

Expected: PASS with a generated in-memory report; no production index is modified by the evaluation command.

### Task 11: End-to-end regression, observability, and rollout verification

**Files:**
- Modify: `src/medical_rag/observability/metrics.py`
- Modify: `src/medical_rag/parsing/worker.py`
- Modify: `src/medical_rag/indexing/outbox.py`
- Test: `tests/integration/test_document_lifecycle.py`
- Test: `tests/integration/test_delete_residuals.py`
- Test: `tests/integration/test_retrieval_permissions.py`
- Test: `tests/integration/test_p0_rollout.py`

**Interfaces:**
- Consumes: all previous task outputs.
- Produces: stage-level counters/timers and a verified end-to-end lifecycle from upload through retrieval and deletion.

- [ ] **Step 1: Add stage metrics**

Emit counters and latency observations keyed by `file_type`, `parser_version`, `cleaner_version`, `chunker_version`, `quality_status`, `index_version`, and `embedding_model`: parse success/failure, review rate, anchor coverage, chunk count, fact count, embedding failure, writer failure, retry count, and deletion residual count.

- [ ] **Step 2: Add end-to-end lifecycle assertions**

Verify the sequence `upload -> ParseJob -> ParsedBlock -> CleanedDocument -> EvidenceUnit -> QualityGate -> IndexJob/Outbox -> embedding -> Writer -> retrieval`. Assert that a review document is stored but absent from retrieval, a failed IndexJob retries, and deletion removes all public/private searchable records.

- [ ] **Step 3: Run the full test suite and static checks**

Run: `python -m pytest -q`

Run: `python -m ruff check src tests`

Expected: all tests pass and Ruff reports no new errors.

- [ ] **Step 4: Perform a manual release checklist**

Confirm: raw files remain readable; every indexed chunk has a source anchor; public records contain no tenant/patient IDs; review records have `retrieval_eligible=False`; all version fields are present; retries are bounded and observable; deletion residual count is zero; and the golden-set report is stored with the release artifacts.

## Coverage and Consistency Review

- The design requirement for an independent cleaner is covered by Task 2.
- `TextSourceParser` and the shared cleaning contract for the public knowledge chain are covered by Tasks 2 and 4 (设计 §3，验收第 10 条).
- Unified PDF/DOCX/XLSX/OCR layout output is covered by Tasks 1 and 3; the Excel P1 gap checklist deliverable is covered by Task 3 Step 5.
- Semantic, table-aware, parent/child chunking is covered by Task 4.
- Structured evidence, raw/normalized fact values, and review status are covered by Task 5.
- Quality metrics, thresholds, PASS/NEEDS_REVIEW/FAIL behavior, and non-indexable review output are covered by Task 6, including conservative pre-golden-set defaults (设计 §9 阈值冷启动).
- The pilot operational exit (derived quality status and failure reason codes in the documents API/UI) is covered by Task 7 (设计 §9 运营出口，验收第 9 条).
- Parser/cleaner/chunker/extractor/embedding/index versions and rebuild identity are covered by Task 8.
- The retrieval-side parent/child contract (`SearchRecord.parent_block_id`, expansion budget, shared evidence-id prefixes) is covered by Task 9 (设计 §7 父子块检索契约).
- Golden datasets and parsing/RAG metrics are covered by Task 10.
- Retry, deletion, permissions, observability, and end-to-end acceptance are covered by Task 11.

## Plan Self-Review

- Placeholder scan completed: no unfinished markers or vague deferred instructions are present.
- Interface names are consistent: `DocumentCleaner.clean`, `LayoutNormalizer.normalize`, `SemanticChunker.chunk`, `QualityGate.evaluate`, and `VersionBundle` are used consistently across tasks.
- Existing public/private writer contracts remain additive and backward compatible.
- The plan is intentionally limited to the document-quality/indexing subsystem; query understanding, Agent behavior, and session memory are out of scope.
