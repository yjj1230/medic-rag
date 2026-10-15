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
from medical_rag.provenance.contracts import SourceLocator
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
    """FAIL 质量判定的稳定、机器可解析错误码。

    ``quality_fail:<first_reason>`` 是确定性的，因此运维和对账器可以依据它，
    而不必耦合到枚举标签。
    """
    primary = quality.reasons[0] if quality.reasons else "unknown"
    return f"quality_fail:{primary}"


class RuleBasedMedicalFactExtractor:
    """确定性基线抽取器；生产可将其替换为受治理的适配器。"""

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
        # Task 11 逐页 OCR 后端（可选；驱动逐页回填）。
        self.per_page_ocr = getattr(configured, "per_page_ocr", None) if configured else None
        # 远端逐 PDF OCR 后端*工厂*（可调用 pdf_bytes -> 后端），
        # 按文档惰性构建。增量式；确定性后端不受影响。
        self.remote_ocr = getattr(configured, "remote_ocr", None) if configured else None
        self.extractor = extractor or RuleBasedMedicalFactExtractor()
        # 解析器只拥有持久化接缝。实际的索引写入
        # 由 IndexWorker 执行，这使解析与任何
        # OpenSearch/客户端实现保持解耦。
        self.private_index_writer = private_index_writer
        self.index_job_factory = index_job_factory
        self.index_version = index_version
        self.embedding_pipeline = embedding_pipeline
        self.allow_unembedded = allow_unembedded
        self.audit_service = audit_service
        self.audit_context = audit_context
        self.outbox_service = outbox_service
        # Task 11：可选的有界可观测记录器（为 None 时为空操作）。
        self.metrics = metrics
        # 患者链路与公共知识链路共用同一套清洗/分块组件（设计 §3）：可注入，
        # 便于按部署调整 token 预算、overlap 与指标名清单；默认语义不变。
        self.cleaner = cleaner or DocumentCleaner()
        self.chunker = chunker or SemanticChunker()
        # 整条解析路径使用单一的 QualityPolicy / QualityGate；Parser/IndexJob 代码中
        # 不再散落阈值字面量。
        self.gate = QualityGate(QualityPolicy())
        if self.audit_service is not None and self.audit_context is None:
            raise ValueError("audit_context is required when audit_service is configured")
        inferred_model = getattr(getattr(embedding_pipeline, "model", None), "model", None)
        self.embedding_model = (
            inferred_model if embedding_model == "none" and isinstance(inferred_model, str) else embedding_model
        )

    @staticmethod
    def _record(metrics: object | None, name: str, **kwargs: object) -> None:
        """向可选记录器发出一个指标（为 None 时为空操作）。"""
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
        locator = None
        if page_number is not None or sheet_name or cell_range or source_anchor:
            locator = SourceLocator(
                locator_type="xlsx_cell_range" if sheet_name else "pdf_text",
                page_number=page_number,
                sheet_name=sheet_name,
                cell_range=cell_range,
                source_anchor=source_anchor,
            )
        return EvidenceUnitDraft(
            text=text,
            kind=kind,
            page_number=page_number,
            sheet_name=sheet_name,
            cell_range=cell_range,
            source_anchor=anchor,
            language=detect_language(text),
            confidence=confidence,
            locator=locator,
        )

    def extract_facts(self, evidence: list[EvidenceUnitDraft]) -> FactExtractionResult:
        return self.extractor.extract(evidence)

    def _cleaned_from_parse(self, result: ParseResult) -> CleanedDocument:
        """从遗留解析证据合成一个 :class:`CleanedDocument`。

        患者解析路径当前直接产出 EvidenceUnitDraft 值（没有 cleaner/chunker
        环节），因此门禁需要同一内容的块视图。每条证据单元与一个清洗后块 1:1
        对应（对遗留路径而言 raw==clean）；锚点/置信度/表格各项度量读取的都是
        门禁所需的同一批字段。
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
        """通过真实 QualityGate 在**一处**推导质量判定。

        这是所有消费方读取资格的唯一接缝。Task 6 用真实的
        ``QualityGate(...).evaluate(...)`` 替换占位实现。遗留抽取器的复核原因
        （``result.needs_review`` / ``result.review_reasons``）以增量方式合并，
        因此既有复核信号绝不丢失。:class:`QualityDecision` 的 ``__post_init__``
        不变量强制 PASS→True / NEEDS_REVIEW/FAIL→False。
        """
        document = result.cleaned or self._cleaned_from_parse(result)
        decision = self.gate.evaluate(document, result.evidence, result.facts)
        extra = tuple(dict.fromkeys(result.review_reasons))
        combined = tuple(dict.fromkeys((*decision.reasons, *extra)))
        if result.needs_review and decision.status == QualityStatus.PASS:
            # 遗留抽取器标记了复核，尽管门禁已通过；
            # 保持保守，提升为 NEEDS_REVIEW。
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
        """把单一来源的资格判定折叠进冻结的草稿中。

        转换通过对冻结的 :class:`EvidenceUnitDraft` 值调用
        :func:`dataclasses.replace` 完成。NEEDS_REVIEW/FAIL 绝不丢弃证据单元；
        它只是把它标记为不可检索。
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
        """为某一条已持久化的证据行构建 VersionBundle。

        这是私有索引路径的唯一构造处：parser/cleaner/chunker 来自已持久化的证据行
        （因此逐块的 cleaner/chunker 溯源得以保留），抽取器版本来自已配置的抽取器，
        向量化元数据来自已配置的模型。``embedding_dimensions`` 在一次成功的向量化
        运行揭示真实向量宽度之前为 None（Step 3 保留这一点）。
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
        """从参考区间字符串解析 ``low-high``；非数字 → (None, None)。"""
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
        """从事实值字符串解析数值读数；非数字 → None。"""
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
        """从草稿构建持久化的 MedicalFact 行，并单一来源化复核状态。"""
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
            # 通过门禁的事实为 ACCEPTED；NEEDS_REVIEW/FAIL 会被标记
            # 一个*不同*的状态，使 FAIL 绝不会与 NEEDS_REVIEW 混为一谈。
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
            # Task 11：当配置了整文档 OCR
            # 适配器、逐页 OCR 后端、或远端逐 PDF 后端
            # 工厂中的任一时，进入逐页兜底。仅凭逐页后端
            # 就必须能够恢复空白/扫描页。
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
        # Task 11 接缝：配置了逐页 OCR 后端时（无论是固定的
        # 确定性实例还是远端逐 PDF 后端工厂），就地回填
        # 有缺陷的页，而不是延后处理。页标识与
        # p{page} 锚点被保留，结果会被标记为 OCR。远端
        # 后端从文档自身字节惰性构建，这样一个
        # PdfPageRenderer 就绑定到该 PDF。
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
        """返回可寻址流的全部原始字节，并复位到 ``0``。"""
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
            # Task 11：解析阶段可观测（metrics 为 None 时为空操作）。
            self._record(
                self.metrics, "record_parse", outcome="success",
                file_type=document.content_type, parser_version=result.parser_version,
            )
            # 单一来源的资格折叠：证据草稿与事实
            # review_status 都从同一个 QualityDecision 推导。
            result, quality = self._apply_eligibility(result)
            # Task 11：复核/分块/事实/锚点覆盖率阶段可观测。
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
            # 重新解析会替换该版本的全部派生证据。让状态迁移
            # 保持持久化且仅含标识符/哈希，这样
            # 对账器可以在不接收文本的情况下移除残留记录。
            replacement_hash = sha256(
                "|".join(item.text for item in result.evidence).encode("utf-8")
            ).hexdigest()
            outbox = self.outbox_service or OutboxService(self.session)
            # 重新解析会取代全部派生行及其 outbox 作业。
            # 新行会获得全新的 ID，而复合 IndexJob 键
            # 使同一次 parse/version 的重试保持幂等。
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
                    # 新行的检索文本始终是清洗后文本；
                    # raw_text/clean_text 为溯源而分别保留。
                    text=item.clean_text if item.clean_text is not None else item.text,
                    page_number=item.page_number,
                    sheet_name=item.sheet_name,
                    cell_range=item.cell_range,
                    source_anchor=item.source_anchor,
                    confidence=item.confidence,
                    raw_text=item.raw_text,
                    clean_text=item.clean_text,
                    section_path=list(item.section_path) if item.section_path else None,
                    locator=item.locator.to_dict() if item.locator is not None else None,
                    parent_block_id=item.parent_block_id,
                    chunk_index=item.chunk_index,
                    # 来自分块器的草稿返回时 parser_version=None；
                    # 从实际解析器填充它，以满足 Task 8 的重建标识。
                    parser_version=item.parser_version or result.parser_version,
                    cleaner_version=item.cleaner_version,
                    chunker_version=item.chunker_version,
                    # 按证据行转发门禁的判定原因，使
                    # 持久化的列成为索引侧资格与
                    # 对账的唯一真值来源（Task 6 A4）。
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
            # 为每条证据行创建 IndexJobs（写但标记）：
            # 不合格的行仍会被写入索引，并标记
            # ``retrieval_eligible=False``（查询时永不可检索），
            # 且 IndexWorker/reconciliation 信任该持久化列。
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
                    # 保持载荷仅含标识符，并让持久化
                    # Outbox 事件可直接追溯到其 IndexJob。
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
                        # 允许那些把映射作为单个参数接收的小型测试工厂，
                        # 同时保持公开接缝简单。
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
            else:  # FAIL：稳定的错误码，原始对象为重新处理而保留。
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

        # 显式提供的写入器对同步调用方和测试很有用；
        # 生产调用方可以省略，转而让异步
        # IndexWorker 消费持久化的待处理作业。把这段代码
        # 放在解析事务/错误处理之外，这样写入器故障
        # 不会把一次已提交的解析错误地变成 ParseJob.FAILED。
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
