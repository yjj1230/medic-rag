from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import PurePath
from typing import BinaryIO
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from medical_rag.domain.enums import (
    DocumentStatus,
    EvidenceKind,
    IndexJobStatus,
    ParseJobStatus,
    QualityStatus,
)
from medical_rag.domain.models import (
    Document,
    DocumentVersion,
    EvidenceUnit,
    IndexJob,
    MedicalFact,
    ParseJob,
    OutboxEvent,
    VersionBundle,
)
from medical_rag.knowledge.semantic_chunker import SemanticChunker
from medical_rag.parsing.adapters import ExcelParser, PdfTextParser, WordTextParser, detect_language, should_ocr_page
from medical_rag.parsing.cleaner import DocumentCleaner
from medical_rag.parsing.contracts import (
    CleanedDocument,
    EvidenceUnitDraft,
    FactExtractionResult,
    MedicalFactDraft,
    MedicalFactExtractor,
    ParseResult,
    ParsedBlock,
    ParsedDocument,
    SourceOffset,
)
from medical_rag.quality.contracts import QualityDecision
from medical_rag.quality.gate import QualityGate, QualityPolicy
from medical_rag.parsing.filetypes import IMAGE_FILE_TYPES as SUPPORTED_IMAGE_TYPES
from medical_rag.storage.object_store import ObjectStore
from medical_rag.retrieval.contracts import PrivateIndexWriter
from medical_rag.models.contracts import EmbeddingPipeline
from medical_rag.audit.service import AuditService
from medical_rag.auth.policy import AccessContext
from medical_rag.indexing.outbox import OutboxService


SUPPORTED_TABLE_TYPES = {
    ".xlsx",
    ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}


def _normalise_file_type(file_type: str) -> str:
    value = file_type.lower().strip()
    if "/" in value:
        return value.split(";", 1)[0]
    return PurePath(value).suffix.lower() or value


def _normalise_number(value: str) -> str:
    return value.replace(",", ".")


def _fail_error_code(quality: QualityDecision) -> str:
    """Stable machine-parseable error code for a FAIL quality decision.

    ``quality_fail:<first_reason>`` is deterministic so operators and
    reconcilers can key off it without coupling to the enum label.
    """
    primary = quality.reasons[0] if quality.reasons else "unknown"
    return f"quality_fail:{primary}"


class RuleBasedMedicalFactExtractor:
    """Deterministic baseline extractor; production can replace it with a governed adapter."""

    _patterns = (
        re.compile(
            r"(?P<name>血红蛋白|Hemoglobin)\s*[:：]?\s*"
            r"(?P<value>[+-]?\d+(?:[.,]\d+)?)\s*"
            r"(?P<unit>g/L|g/dL|mg/dL|mmol/L|μmol/L|umol/L)?"
            r"(?:\s*(?:参考范围|参考值|reference(?:\s+range)?)\s*[:：]?\s*"
            r"(?P<reference>[<>]?\s*[+-]?\d+(?:[.,]\d+)?\s*[-–]\s*[+-]?\d+(?:[.,]\d+)?))?",
            re.IGNORECASE,
        ),
    )
    _allowed_units = {"血红蛋白": {"g/L", "g/dL"}, "Hemoglobin": {"g/L", "g/dL"}}

    def extract(self, evidence: list[EvidenceUnitDraft]) -> FactExtractionResult:
        result = FactExtractionResult()
        candidates: dict[str, list[MedicalFactDraft]] = {}
        for evidence_index, item in enumerate(evidence):
            if not item.source_anchor:
                result.needs_review = True
                if "missing_source_anchor" not in result.review_reasons:
                    result.review_reasons.append("missing_source_anchor")
                continue
            if item.confidence is None:
                # 来源未提供置信度（例如远端 OCR 只返回文本）：不得按 1.0 或 0.0
                # 冒充测量值来自动采纳事实——保守送人工复核，且不产出事实候选。
                result.needs_review = True
                if "confidence_unavailable" not in result.review_reasons:
                    result.review_reasons.append("confidence_unavailable")
                continue
            for pattern in self._patterns:
                for match in pattern.finditer(item.text):
                    name = match.group("name")
                    value_raw = match.group("value")
                    unit = match.group("unit")
                    if "," in value_raw:
                        result.needs_review = True
                        if "ambiguous_decimal_separator" not in result.review_reasons:
                            result.review_reasons.append("ambiguous_decimal_separator")
                        continue
                    if unit and unit not in self._allowed_units.get(name, set()):
                        result.needs_review = True
                        if "unit_mismatch" not in result.review_reasons:
                            result.review_reasons.append("unit_mismatch")
                        continue
                    confidence = max(0.0, min(1.0, item.confidence * 0.98))
                    if confidence < 0.80:
                        result.needs_review = True
                        if "low_confidence" not in result.review_reasons:
                            result.review_reasons.append("low_confidence")
                        continue
                    draft = MedicalFactDraft(
                        name=name,
                        value=_normalise_number(value_raw),
                        unit=unit,
                        reference_range=(
                            match.group("reference").replace(" ", "")
                            if match.group("reference")
                            else None
                        ),
                        collection_time=None,
                        abnormal_flag=None,
                        source_page=item.page_number,
                        source_anchor=item.source_anchor,
                        evidence_index=evidence_index,
                        confidence=confidence,
                    )
                    candidates.setdefault(name.lower(), []).append(draft)

        for _name, drafts in candidates.items():
            distinct_values = {(draft.value, draft.unit) for draft in drafts}
            if len(distinct_values) > 1:
                result.needs_review = True
                if "conflicting_fact" not in result.review_reasons:
                    result.review_reasons.append("conflicting_fact")
                continue
            result.facts.append(drafts[0])
        return result


class MixedPdfOcrDeferredError(ValueError):
    """混合 PDF（部分页缺原生文本、部分页有原生文本）且逐页 OCR 无法执行时抛出。

    是 ValueError 的子类型：`pytest.raises(ValueError)` 与既有调用方均吻合；而
    `type(exc).__name__` 给出稳定的 `"MixedPdfOcrDeferredError"` 错误码，Task 7
    的操作出口可据此向用户呈现可执行的动作（拆分 PDF / 接入页级 OCR 后端），
    不会被误判为“不支持的文件类型”。
    """


class ParsingService:
    parser_version = "parser-v1"

    def __init__(
        self,
        session: Session | None = None,
        object_store: ObjectStore | None = None,
        adapters: object | None = None,
        extractor: MedicalFactExtractor | None = None,
        private_index_writer: PrivateIndexWriter | None = None,
        index_job_factory: object | None = None,
        index_version: str = "private-v1",
        embedding_model: str = "none",
        embedding_pipeline: EmbeddingPipeline | None = None,
        allow_unembedded: bool = False,
        audit_service: AuditService | None = None,
        audit_context: AccessContext | None = None,
        outbox_service: OutboxService | None = None,
        metrics: object | None = None,
        cleaner: DocumentCleaner | None = None,
        chunker: SemanticChunker | None = None,
    ) -> None:
        self.session = session
        self.object_store = object_store
        configured = adapters
        self.text_parser = getattr(configured, "text", None) if configured else None
        self.ocr_parser = getattr(configured, "ocr", None) if configured else None
        self.table_parser = getattr(configured, "table", None) if configured else None
        # Task 11 per-page OCR backend (optional; drives per-page backfill).
        self.per_page_ocr = getattr(configured, "per_page_ocr", None) if configured else None
        # Remote per-PDF OCR backend *factory* (callable pdf_bytes -> backend),
        # built lazily per document.  Additive; deterministic backend untouched.
        self.remote_ocr = getattr(configured, "remote_ocr", None) if configured else None
        self.extractor = extractor or RuleBasedMedicalFactExtractor()
        # The parser only owns the persistence seam.  Actual index writes are
        # performed by IndexWorker, which keeps parsing decoupled from any
        # OpenSearch/client implementation.
        self.private_index_writer = private_index_writer
        self.index_job_factory = index_job_factory
        self.index_version = index_version
        self.embedding_pipeline = embedding_pipeline
        self.allow_unembedded = allow_unembedded
        self.audit_service = audit_service
        self.audit_context = audit_context
        self.outbox_service = outbox_service
        # Task 11: optional bounded observability recorder (no-op when None).
        self.metrics = metrics
        # 患者链路与公共知识链路共用同一套清洗/分块组件（设计 §3）：可注入，
        # 便于按部署调整 token 预算、overlap 与指标名清单；默认语义不变。
        self.cleaner = cleaner or DocumentCleaner()
        self.chunker = chunker or SemanticChunker()
        # Single QualityPolicy / QualityGate for the whole parse path; no
        # scattered threshold literals in Parser/IndexJob code.
        self.gate = QualityGate(QualityPolicy())
        if self.audit_service is not None and self.audit_context is None:
            raise ValueError("audit_context is required when audit_service is configured")
        inferred_model = getattr(getattr(embedding_pipeline, "model", None), "model", None)
        self.embedding_model = (
            inferred_model if embedding_model == "none" and isinstance(inferred_model, str) else embedding_model
        )

    @staticmethod
    def _record(metrics: object | None, name: str, **kwargs: object) -> None:
        """Emit one metric onto the optional recorder (no-op when None)."""
        if metrics is None:
            return
        method = getattr(metrics, name, None)
        if callable(method):
            method(**kwargs)

    def evidence_from_text(
        self,
        text: str,
        *,
        page_number: int | None = None,
        source_anchor: str | None = None,
        confidence: float = 1.0,
        kind: EvidenceKind = EvidenceKind.REPORT_TEXT,
        sheet_name: str | None = None,
        cell_range: str | None = None,
    ) -> EvidenceUnitDraft:
        anchor = source_anchor or (f"p{page_number}:text" if page_number is not None else "text:0")
        return EvidenceUnitDraft(
            text=text,
            kind=kind,
            page_number=page_number,
            sheet_name=sheet_name,
            cell_range=cell_range,
            source_anchor=anchor,
            language=detect_language(text),
            confidence=confidence,
        )

    def extract_facts(self, evidence: list[EvidenceUnitDraft]) -> FactExtractionResult:
        return self.extractor.extract(evidence)

    def _cleaned_from_parse(self, result: ParseResult) -> CleanedDocument:
        """Synthesize a :class:`CleanedDocument` from legacy parse evidence.

        The patient parse path currently produces EvidenceUnitDraft values
        directly (no cleaner/chunker pass), so the gate needs a block view of
        the same content.  Each evidence unit maps 1:1 to a cleaned block
        (raw==clean for the legacy path); anchor/confidence/table metrics all
        read the identical fields the gate needs.
        """
        source_offsets: list[SourceOffset] = []
        blocks: list[ParsedBlock] = []
        for index, item in enumerate(result.evidence):
            text = item.clean_text if item.clean_text is not None else item.text
            blocks.append(
                ParsedBlock(
                    block_id=f"evidence:{index}",
                    block_type=(
                        "table" if item.kind == EvidenceKind.REPORT_TABLE else "paragraph"
                    ),
                    raw_text=text,
                    page_number=item.page_number,
                    sheet_name=item.sheet_name,
                    cell_range=item.cell_range,
                    section_path=item.section_path,
                    source_anchor=item.source_anchor,
                    confidence=item.confidence,
                )
            )
            source_offsets.append(
                SourceOffset(block_id=f"evidence:{index}", raw_start=0, raw_end=len(text), clean_start=0, clean_end=len(text))
            )
        return CleanedDocument(
            raw_blocks=tuple(blocks),
            cleaned_blocks=tuple(blocks),
            source_offsets=tuple(source_offsets),
            cleaning_flags=tuple(result.review_reasons),
            cleaner_version="gate-synth",
        )

    def eligibility_decision(self, result: ParseResult) -> QualityDecision:
        """Derive the quality decision in ONE place via the real QualityGate.

        This is the single seam every consumer reads eligibility from.  Task 6
        replaces the placeholder with a real ``QualityGate(...).evaluate(...)``.
        Legacy extractor review reasons (``result.needs_review`` /
        ``result.review_reasons``) are merged additively so existing review
        signals are never dropped.  The ``__post_init__`` invariant of
        :class:`QualityDecision` enforces PASS→True / NEEDS_REVIEW/FAIL→False.
        """
        document = result.cleaned or self._cleaned_from_parse(result)
        decision = self.gate.evaluate(document, result.evidence, result.facts)
        extra = tuple(dict.fromkeys(result.review_reasons))
        combined = tuple(dict.fromkeys((*decision.reasons, *extra)))
        if result.needs_review and decision.status == QualityStatus.PASS:
            # Legacy extractor flagged a review even though the gate passed;
            # stay conservative and promote to NEEDS_REVIEW.
            return QualityDecision(
                status=QualityStatus.NEEDS_REVIEW,
                reasons=combined or ("manual_review",),
                metrics=decision.metrics,
                retrieval_eligible=False,
            )
        if combined != decision.reasons:
            return QualityDecision(
                status=decision.status,
                reasons=combined or decision.reasons,
                metrics=decision.metrics,
                retrieval_eligible=decision.retrieval_eligible,
            )
        return decision

    def _apply_eligibility(self, result: ParseResult) -> tuple[ParseResult, QualityDecision]:
        """Fold the single-sourced eligibility decision into the frozen drafts.

        Transforming happens through :func:`dataclasses.replace` on the frozen
        :class:`EvidenceUnitDraft` values.  NEEDS_REVIEW/FAIL never discards an
        evidence unit; it only marks it non-retrievable.
        """
        decision = self.eligibility_decision(result)
        if not decision.retrieval_eligible:
            result = replace(
                result,
                evidence=[
                    replace(item, retrieval_eligible=False) for item in result.evidence
                ],
            )
        return result, decision

    def _version_bundle(self, evidence: EvidenceUnit) -> VersionBundle:
        """Build the VersionBundle for one persisted evidence row.

        This is the single construction site for the private indexing path:
        parser/cleaner/chunker come from the persisted evidence row (so per-block
        cleaner/chunker provenance is preserved), the extractor version from the
        configured extractor, and embedding metadata from the configured model.
        ``embedding_dimensions`` is None until a successful embedding run reveals
        the actual vector width (Step 3 preserves that).
        """
        return VersionBundle(
            parser_version=evidence.parser_version or self.parser_version,
            cleaner_version=evidence.cleaner_version,
            chunker_version=evidence.chunker_version,
            extractor_version=getattr(self.extractor, "version", None),
            embedding_model=self.embedding_model,
            embedding_dimensions=None,
            index_version=self.index_version,
        )

    @staticmethod
    def _reference_bounds(reference_range: str | None) -> tuple[float | None, float | None]:
        """Parse ``low-high`` from a reference-range string; non-numeric → (None, None)."""
        if not reference_range:
            return None, None
        parts = re.split(r"[-–—]", reference_range.strip())
        if len(parts) != 2:
            return None, None
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            return None, None

    @staticmethod
    def _value_number(value: str) -> float | None:
        """Parse the numeric reading from a fact value string; non-numeric → None."""
        text = re.sub(r"\s", "", value)
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _materialize_fact(
        self,
        fact: MedicalFactDraft,
        evidence_rows: list[EvidenceUnit],
        document: Document,
        version: DocumentVersion,
        quality: QualityDecision,
    ) -> MedicalFact:
        """Build a durable MedicalFact row from a draft, single-sourcing review state."""
        reference_low, reference_high = self._reference_bounds(fact.reference_range)
        return MedicalFact(
            tenant_id=document.tenant_id,
            patient_id=document.patient_id,
            document_version_id=version.id,
            evidence_unit_id=evidence_rows[fact.evidence_index].id,
            name=fact.name,
            value=fact.value,
            unit=fact.unit,
            reference_range=fact.reference_range,
            collection_time=fact.collection_time,
            abnormal_flag=fact.abnormal_flag,
            source_page=fact.source_page,
            source_anchor=fact.source_anchor,
            confidence=fact.confidence,
            value_number=self._value_number(fact.value),
            reference_low=reference_low,
            reference_high=reference_high,
            canonical_name=fact.name,
            raw_name=fact.name,
            unit_raw=fact.unit,
            unit_normalized=fact.unit,
            extractor_version=getattr(self.extractor, "version", None),
            # Gate-passing facts are ACCEPTED; NEEDS_REVIEW/FAIL are flagged
            # with a DISTINCT status so FAIL is never conflated with NEEDS_REVIEW.
            review_status=(
                "ACCEPTED"
                if quality.retrieval_eligible
                else (
                    QualityStatus.FAIL.value
                    if quality.status == QualityStatus.FAIL
                    else "NEEDS_REVIEW"
                )
            ),
        )

    def _default_parsed_document(self, file: BinaryIO, file_type: str, language_hints: list[str]) -> ParsedDocument:
        normalized = _normalise_file_type(file_type)
        if normalized in SUPPORTED_IMAGE_TYPES:
            if self.ocr_parser is None:
                raise RuntimeError("图片解析需要配置 OCR 适配器")
            return self.ocr_parser.parse_images(file, language_hints)
        if normalized in SUPPORTED_TABLE_TYPES:
            parsed = ExcelParser().parse(file, normalized)
        elif normalized in {".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}:
            parsed = WordTextParser().parse(file, normalized)
        elif normalized in {".pdf", "application/pdf"}:
            parser = self.text_parser or PdfTextParser()
            parsed = parser.parse(file, normalized)
            # Task 11: enter the per-page fallback when either a whole-doc OCR
            # adapter, the per-page OCR backend, or the remote per-PDF backend
            # factory is configured.  The per-page backend alone must be able to
            # recover blank/scanned pages.
            if self.ocr_parser is not None or self.per_page_ocr is not None or self.remote_ocr is not None:
                parsed = self._apply_pdf_ocr_fallback(file, parsed, language_hints)
        elif self.text_parser is not None:
            parsed = self.text_parser.parse(file, normalized)
        else:
            raise ValueError(f"不支持的解析文件类型: {file_type}")
        return parsed

    def _apply_pdf_ocr_fallback(
        self, file: BinaryIO, parsed: ParsedDocument, language_hints: list[str]
    ) -> ParsedDocument:
        """逐页 OCR 回退（Step 3 seam；Path B —— 真实逐页 OCR 在本任务不可行）。

        用 should_ocr_page 逐页判定，绝不静默丢失 OCR-able 内容。识别到缺文本页
        （含原生 blocks 不存在的空白/扫描页）时：
          - 无缺文本页                        -> 保留原生解析；
          - 全部页缺文本（无原生可丢）          -> 整份回退 OCR；
          - 混合（部分页缺、部分页有原生文本）  -> 若配置了页级 OCR 后端
            （Task 11，``per_page_ocr``），逐页补扫缺文本页并保留页身份 / ``p{page}``
            锚点；否则抛 MixedPdfOcrDeferredError（文档化结果，Task 7 操作出口可
            据此呈现可执行动作）；
          - 无法确认页数（PDF 重开失败）       -> 同样抛 MixedPdfOcrDeferredError，
            绝不静默假定“全部覆盖”。

        仓库无 PDF→图像渲染器（无 pypdfium2/pymupdf/pdf2image），故 Task 11 的
        页级 OCR 后端基于确定性 fixture transport（见 per_page_ocr.py）；生产环境
        可在保留页身份的前提下替换为真实渲染器，不改变调用方。
        """
        if not parsed.blocks:
            # 非版面结构解析（如 deterministic 文本解析器）：无法逐页判定，
            # 沿用 legacy 语义 —— 仅当完全无原生文本才整份回退 OCR，绝不覆盖
            # 已有原生文本。
            if parsed.raw_text.strip():
                return parsed
            return self._whole_doc_ocr(file, language_hints)
        total_pages = self._pdf_page_count(file, parsed)
        if total_pages is None:
            # 无法重开 PDF 确认页数：不得静默假设“全部覆盖”（否则尾部空白页会被
            # 当作已被覆盖而丢失），交给操作出口判定。
            raise MixedPdfOcrDeferredError(
                "无法确认 PDF 页数以执行逐页 OCR 回退；为不静默丢失内容，请使用可重开"
                "读取的 PDF 或接入页级 OCR 后端（见 gap-checklist，Task 7/11）。"
            )
        deficient_pages = [
            page
            for page in range(1, total_pages + 1)
            if should_ocr_page(parsed.blocks, page)
        ]
        if not deficient_pages:
            return parsed
        if len(deficient_pages) == total_pages:
            return self._whole_doc_ocr(file, language_hints)
        # Task 11 seam: when a per-page OCR backend is configured (either a fixed
        # deterministic instance or the remote per-PDF backend factory), backfill
        # the deficient pages in-place instead of deferring.  Page identity and
        # p{page} anchors are preserved and the result is OCR-labelled.  The
        # remote backend is built lazily from the document's own bytes so one
        # PdfPageRenderer is bound to this PDF.
        backend = self.per_page_ocr
        if backend is None and self.remote_ocr is not None:
            backend = self.remote_ocr(self._read_bytes(file))
        if backend is not None:
            from medical_rag.parsing.per_page_ocr import ocr_backfill_pdf

            return ocr_backfill_pdf(
                parsed, backend, total_pages, language_hints
            )
        raise MixedPdfOcrDeferredError(
            f"PDF OCR 回退无法逐页执行：第 {deficient_pages} 页缺原生文本，但文件还有其他"
            f"原生文本页。现行 OCR 后端仅支持整份回退且不保留页身份，无法只补扫缺文本页；"
            f"为不静默丢失内容，请拆分 PDF 后重试，或接入页级 OCR 后端"
            f"（见 docs/superpowers/notes/excel-parser-p1-gap-checklist.md，Task 7/11）。"
        )

    def _whole_doc_ocr(self, file: BinaryIO, language_hints: list[str]) -> ParsedDocument:
        if hasattr(file, "seek"):
            file.seek(0)
        return self.ocr_parser.parse_images(file, language_hints)

    @staticmethod
    def _read_bytes(file: BinaryIO) -> bytes:
        """Return the full raw bytes of a seekable stream, resetting to ``0``."""
        if hasattr(file, "seek"):
            file.seek(0)
        data = file.read()
        return bytes(data) if not isinstance(data, bytes) else data

    @staticmethod
    def _pdf_page_count(file: BinaryIO, parsed: ParsedDocument) -> int | None:
        """真实 PDF 页数；无法重开确认时返回 None（调用方须按“不可确认”走安全分支）。"""
        if not parsed.blocks:
            # 无页面结构（deterministic 解析）=> 当作单文档，回退到 raw_text 判定。
            return 1
        try:
            if hasattr(file, "seek"):
                file.seek(0)
            from pypdf import PdfReader

            return len(PdfReader(file).pages)
        except Exception:
            # 不能从 blocks 反推页数：尾部空白页在 blocks 中缺席，取 max(page_number)
            # 会把尾部空白页谎报为“已覆盖”。返回 None 让调用方走安全分支。
            return None

    def _chunk_cleaned(self, cleaned: CleanedDocument) -> list[EvidenceUnitDraft]:
        """按页/sheet 分组做语义分块，并把 chunk_index 重排为文档内全局单调。

        ``SemanticChunker`` 自身按阅读顺序累积，跨页/跨 sheet 聚合会把页码与锚点
        并成一页，毁掉页级溯源（质量门禁的页覆盖与前端溯源卡都依赖它），因此分组
        工作放在服务层，分块器仍是纯粹的文本聚合器。
        """
        drafts: list[EvidenceUnitDraft] = []
        for view in self._page_group_views(cleaned):
            drafts.extend(self.chunker.chunk(view))
        return [replace(draft, chunk_index=index) for index, draft in enumerate(drafts)]

    @staticmethod
    def _page_group_views(cleaned: CleanedDocument) -> list[CleanedDocument]:
        """把清洗结果切成互不跨 (page_number, sheet_name) 的分块视图（保阅读顺序）。"""
        raw_by_id = {block.block_id: block for block in cleaned.raw_blocks}
        offset_by_id = {offset.block_id: offset for offset in cleaned.source_offsets}
        groups: dict[tuple[int | None, str | None], list[ParsedBlock]] = {}
        for block in cleaned.cleaned_blocks:
            groups.setdefault((block.page_number, block.sheet_name), []).append(block)
        ordered = sorted(
            groups.values(), key=lambda blocks: min(block.reading_order for block in blocks)
        )
        views: list[CleanedDocument] = []
        for blocks in ordered:
            block_ids = tuple(block.block_id for block in blocks)
            views.append(
                replace(
                    cleaned,
                    raw_blocks=tuple(
                        raw_by_id[block_id] for block_id in block_ids if block_id in raw_by_id
                    ),
                    cleaned_blocks=tuple(blocks),
                    source_offsets=tuple(
                        offset_by_id[block_id]
                        for block_id in block_ids
                        if block_id in offset_by_id
                    ),
                )
            )
        return views

    def _raw_text_fallback_evidence(
        self, parsed: ParsedDocument, cleaned: CleanedDocument
    ) -> list[EvidenceUnitDraft]:
        """块级链路无产出时的兜底证据：绝不静默丢内容，也不编造位置（锚点保持 None）。"""
        return [
            EvidenceUnitDraft(
                text=parsed.raw_text,
                kind=EvidenceKind.REPORT_TEXT,
                source_anchor=None,
                language=parsed.language,
                confidence=1.0,
                raw_text=parsed.raw_text,
                clean_text=parsed.raw_text,
                cleaner_version=cleaned.cleaner_version,
                chunker_version=self.chunker.version,
                chunk_index=0,
            )
        ]

    def _with_page_total(self, file: BinaryIO, parsed: ParsedDocument) -> ParsedDocument:
        """把"文档声明总页数"补进解析结果，供质量门禁判定缺页。

        PDF 用 pypdf 真实确认（不能用 max(page_number) 反推：尾部空白页不在 blocks 里，
        反推会把缺页谎报成"已覆盖"）。确认失败 => None => 门禁不判定。其他文件类型退回
        块页码最大值（无页码信息时同样为 None）。
        """
        if parsed.page_total is not None:
            return parsed
        if _normalise_file_type(parsed.file_type) in {".pdf", "application/pdf"}:
            total = self._pdf_page_count(file, parsed)
        else:
            # 非 PDF：没有权威页数来源，退回块页码最大值（无页码信息 => None => 不判定）。
            pages = {b.page_number for b in parsed.blocks if b.page_number is not None}
            total = max(pages) if pages else None
        if total is None:
            return parsed
        return replace(parsed, page_total=total)

    def process_file(
        self,
        file: BinaryIO,
        file_type: str,
        language_hints: list[str] | None = None,
    ) -> ParseResult:
        hints = language_hints or ["zh-CN", "en-US"]
        parsed = self._default_parsed_document(file, file_type, hints)
        parsed = self._with_page_total(file, parsed)
        # 真实清洗链路（raw↔clean 分层、不可见字符/全角归一化、OCR 断行、模板行检测），
        # 随后语义分块。原文永不覆盖：ParsedDocument.raw_text 与 raw_blocks 原样保留。
        cleaned = self.cleaner.clean(parsed)
        evidence = self._chunk_cleaned(cleaned)
        if not evidence and parsed.raw_text.strip():
            evidence = self._raw_text_fallback_evidence(parsed, cleaned)
        extraction = self.extract_facts(evidence) if evidence else FactExtractionResult()
        return ParseResult(
            raw_text=parsed.raw_text,
            evidence=evidence,
            facts=extraction.facts,
            tables=list(parsed.tables),
            language=parsed.language,
            parser_version=parsed.parser_version or self.parser_version,
            needs_review=extraction.needs_review,
            review_reasons=extraction.review_reasons,
            cleaned=cleaned,
        )

    def process(self, job_id: UUID) -> ParseResult:
        if self.session is None or self.object_store is None:
            raise RuntimeError("持久化解析需要 session 和 object_store")
        job = self.session.get(ParseJob, job_id)
        if job is None:
            raise ValueError(f"解析任务不存在: {job_id}")
        version = self.session.get(DocumentVersion, job.document_version_id)
        if version is None:
            raise ValueError(f"文档版本不存在: {job.document_version_id}")
        document = self.session.get(Document, version.document_id)
        if document is None:
            raise ValueError(f"文档不存在: {version.document_id}")

        job.status = ParseJobStatus.RUNNING
        job.attempts += 1
        job.started_at = datetime.now(UTC)
        document.status = DocumentStatus.PROCESSING
        version.status = DocumentStatus.PROCESSING
        self.session.commit()
        index_job_ids: list[UUID] = []
        try:
            content = self.object_store.get(document.storage_key)
            result = self.process_file(content, document.content_type)
            # Task 11: parse-stage observability (no-op when metrics is None).
            self._record(
                self.metrics, "record_parse", outcome="success",
                file_type=document.content_type, parser_version=result.parser_version,
            )
            # Single-sourced eligibility fold: evidence drafts and fact
            # review_status both derive from one QualityDecision.
            result, quality = self._apply_eligibility(result)
            # Task 11: review/chunk/fact/anchor-coverage stage observability.
            if quality.status != QualityStatus.PASS:
                self._record(
                    self.metrics, "record_review",
                    file_type=document.content_type, parser_version=result.parser_version,
                    review_status=quality.status.value,
                )
            self._record(
                self.metrics, "record_chunks", count=len(result.evidence),
                chunker_version=(
                    result.evidence[0].chunker_version if result.evidence else None
                ),
                file_type=document.content_type,
            )
            self._record(
                self.metrics, "record_facts", count=len(result.facts),
                file_type=document.content_type,
            )
            anchored = sum(1 for item in result.evidence if item.source_anchor)
            coverage = (anchored / len(result.evidence)) if result.evidence else 1.0
            self._record(
                self.metrics, "record_anchor_coverage", coverage=coverage,
                file_type=document.content_type, parser_version=result.parser_version,
            )
            # A reparse replaces all derived evidence for this version.  Keep
            # the transition durable and identifier/hash-only so a
            # reconciler can remove residual records without receiving text.
            replacement_hash = sha256(
                "|".join(item.text for item in result.evidence).encode("utf-8")
            ).hexdigest()
            outbox = self.outbox_service or OutboxService(self.session)
            # A reparse supersedes all derived rows and their outbox jobs.  The
            # new rows receive fresh IDs, while the composite IndexJob key
            # keeps retries of one parse/version idempotent.
            self.session.execute(delete(IndexJob).where(IndexJob.document_version_id == version.id))
            self.session.execute(
                delete(OutboxEvent).where(OutboxEvent.aggregate_id == str(version.id))
            )
            outbox.create_lifecycle_event(
                event_type="document.version_replaced",
                aggregate_id=version.id,
                idempotency_key=f"document-version:{version.id}:replacement:{job.attempts}",
                payload={
                    "document_id": str(document.id),
                    "document_version_id": str(version.id),
                    "version_number": version.version_number,
                    "content_hash": replacement_hash,
                    "parser_version": result.parser_version,
                },
            )
            self.session.execute(delete(EvidenceUnit).where(EvidenceUnit.document_version_id == version.id))
            self.session.execute(delete(MedicalFact).where(MedicalFact.document_version_id == version.id))
            evidence_rows = [
                EvidenceUnit(
                    tenant_id=document.tenant_id,
                    patient_id=document.patient_id,
                    document_version_id=version.id,
                    kind=item.kind,
                    # Retrieval text is always the clean text for new rows;
                    # raw_text/clean_text are preserved separately for provenance.
                    text=item.clean_text if item.clean_text is not None else item.text,
                    page_number=item.page_number,
                    sheet_name=item.sheet_name,
                    cell_range=item.cell_range,
                    source_anchor=item.source_anchor,
                    confidence=item.confidence,
                    raw_text=item.raw_text,
                    clean_text=item.clean_text,
                    section_path=list(item.section_path) if item.section_path else None,
                    parent_block_id=item.parent_block_id,
                    chunk_index=item.chunk_index,
                    # Drafts from a chunker come back with parser_version=None;
                    # populate from the actual parser for the Task 8 rebuild identity.
                    parser_version=item.parser_version or result.parser_version,
                    cleaner_version=item.cleaner_version,
                    chunker_version=item.chunker_version,
                    # Forward the gate's decision reasons per evidence row so
                    # the persisted column is the single source of truth for
                    # index-side eligibility and reconciliation (Task 6 A4).
                    quality_flags=tuple(
                        dict.fromkeys((*(item.quality_flags or ()), *quality.reasons))
                    )
                    or None,
                    retrieval_eligible=item.retrieval_eligible,
                )
                for item in result.evidence
            ]
            self.session.add_all(evidence_rows)
            self.session.flush()
            facts = [
                self._materialize_fact(fact, evidence_rows, document, version, quality)
                for fact in result.facts
                if 0 <= fact.evidence_index < len(evidence_rows)
            ]
            self.session.add_all(facts)
            self.session.flush()
            index_jobs = []
            # IndexJobs are created for every evidence row (write-but-flag):
            # ineligible rows are still written to the index marked
            # ``retrieval_eligible=False`` (never searchable at query time),
            # and IndexWorker/reconciliation trust the persisted column.
            for evidence in evidence_rows:
                bundle = self._version_bundle(evidence)
                values = {
                    "document_version_id": version.id,
                    "evidence_unit_id": evidence.id,
                    "index_version": bundle.index_version,
                    "parser_version": bundle.parser_version,
                    "embedding_model": bundle.embedding_model,
                    "cleaner_version": bundle.cleaner_version,
                    "chunker_version": bundle.chunker_version,
                    "extractor_version": bundle.extractor_version,
                    "embedding_dimensions": bundle.embedding_dimensions,
                    "status": IndexJobStatus.PENDING,
                }
                factory = self.index_job_factory
                if factory is None:
                    outbox = self.outbox_service or OutboxService(self.session)
                    dispatch = outbox.create_index_event(
                        document_version_id=version.id,
                        evidence_unit_id=evidence.id,
                        version_bundle=bundle,
                        idempotency_key=(
                            f"{version.id}:{evidence.id}:"
                            f"{bundle.parser_version}:{bundle.cleaner_version}:"
                            f"{bundle.chunker_version}:{bundle.extractor_version}:"
                            f"{bundle.embedding_model}:{bundle.index_version}"
                        ),
                        payload={
                            "document_version_id": str(version.id),
                            "evidence_unit_id": str(evidence.id),
                            "content_hash": sha256(evidence.text.encode("utf-8")).hexdigest(),
                            "index_version": bundle.index_version,
                            "parser_version": bundle.parser_version,
                            "cleaner_version": bundle.cleaner_version,
                            "chunker_version": bundle.chunker_version,
                            "extractor_version": bundle.extractor_version,
                            "embedding_model": bundle.embedding_model,
                            "embedding_dimensions": None,
                        },
                    )
                    # Keep the payload identifier-only and make the durable
                    # Outbox event directly traceable to its IndexJob.
                    dispatch.event.payload = {
                        **dispatch.event.payload,
                        "job_id": str(dispatch.job.id),
                    }
                    index_jobs.append(dispatch.job)
                elif hasattr(factory, "create"):
                    index_jobs.append(factory.create(**values))
                else:
                    try:
                        index_jobs.append(factory(**values))
                    except TypeError:
                        # Permit small test factories that accept the mapping
                        # as one argument while keeping the public seam simple.
                        index_jobs.append(factory(values))
            self.session.add_all(index_jobs)
            finished_at = datetime.now(UTC)
            job.finished_at = finished_at
            version.parser_version = result.parser_version
            if quality.status == QualityStatus.PASS:
                job.status = ParseJobStatus.SUCCEEDED
                job.error_code = None
                document.status = DocumentStatus.READY
                version.status = DocumentStatus.READY
            elif quality.status == QualityStatus.NEEDS_REVIEW:
                job.status = ParseJobStatus.NEEDS_REVIEW
                job.error_code = "manual_review_required"
                document.status = DocumentStatus.NEEDS_REVIEW
                version.status = DocumentStatus.NEEDS_REVIEW
                outbox.create_lifecycle_event(
                    event_type="document.evidence.review_required",
                    aggregate_id=version.id,
                    idempotency_key=f"document-version:{version.id}:review-required:{job.attempts}",
                    payload={
                        "document_id": str(document.id),
                        "document_version_id": str(version.id),
                        "parser_version": result.parser_version,
                        "reason_count": len(quality.reasons),
                    },
                )
            else:  # FAIL: stable error code, original object preserved for reprocessing.
                job.status = ParseJobStatus.FAILED
                job.error_code = _fail_error_code(quality)
                document.status = DocumentStatus.FAILED
                version.status = DocumentStatus.FAILED
                outbox.create_lifecycle_event(
                    event_type="document.evidence.quality_failed",
                    aggregate_id=version.id,
                    idempotency_key=f"document-version:{version.id}:quality-failed:{job.attempts}",
                    payload={
                        "document_id": str(document.id),
                        "document_version_id": str(version.id),
                        "parser_version": result.parser_version,
                        "reasons": list(quality.reasons),
                    },
                )
            self.session.commit()
            index_job_ids = [item.id for item in index_jobs]
        except Exception as exc:
            self._record(
                self.metrics, "record_parse", outcome="failure",
                file_type=document.content_type,
            )
            job.status = ParseJobStatus.FAILED
            job.error_code = type(exc).__name__[:100]
            job.finished_at = datetime.now(UTC)
            document.status = DocumentStatus.FAILED
            version.status = DocumentStatus.FAILED
            self.session.commit()
            raise

        # Explicitly supplied writers are useful for synchronous callers and
        # tests; production callers can omit one and let an asynchronous
        # IndexWorker consume the durable pending jobs instead.  Keep this
        # outside the parse transaction/error handler so a writer outage does
        # not falsely turn an already-committed parse into ParseJob.FAILED.
        if self.private_index_writer is not None:
            from medical_rag.retrieval.indexing import IndexWorker

            worker = IndexWorker(
                self.session,
                self.private_index_writer,
                embedding_pipeline=self.embedding_pipeline,
                allow_unembedded=self.allow_unembedded,
                audit_service=self.audit_service,
                audit_context=self.audit_context,
                metrics=self.metrics,
            )
            for index_job_id in index_job_ids:
                worker.run(index_job_id)
        return result

    def recover_stale_jobs(self, timeout_minutes: int = 30) -> int:
        if self.session is None:
            raise RuntimeError("恢复解析任务需要 session")
        cutoff = datetime.now(UTC) - timedelta(minutes=timeout_minutes)
        jobs = self.session.scalars(
            select(ParseJob).where(
                ParseJob.status == ParseJobStatus.RUNNING,
                ParseJob.started_at.is_not(None),
                ParseJob.started_at < cutoff,
            )
        ).all()
        for job in jobs:
            job.status = ParseJobStatus.PENDING
            job.error_code = "stale_job_recovered"
        self.session.commit()
        return len(jobs)
