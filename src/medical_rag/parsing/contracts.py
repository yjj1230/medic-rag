from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import BinaryIO, Literal, Protocol

from medical_rag.domain.enums import EvidenceKind


@dataclass(frozen=True, slots=True)
class ParsedSegment:
    text: str
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    source_anchor: str | None = None
    language: str = "und"
    # None = 该来源没有提供置信度（例如远端 OCR 只返回文本）。绝不伪造默认值：
    # 质量门禁把"不可判定"与"低置信度"区分开处理。
    confidence: float | None = 1.0
    kind: EvidenceKind = EvidenceKind.REPORT_TEXT


@dataclass(frozen=True, slots=True)
class ParsedTable:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    source_anchor: str | None = None
    language: str = "und"
    confidence: float = 1.0

    @property
    def text(self) -> str:
        lines = [" | ".join(self.headers)] if self.headers else []
        lines.extend(" | ".join(row) for row in self.rows)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    file_type: str
    raw_text: str
    segments: tuple[ParsedSegment, ...] = ()
    tables: tuple[ParsedTable, ...] = ()
    language: str = "und"
    parser_version: str = "parser-v1"
    # 解析适配器直接产出的版面块（Task 3）。LayoutNormalizer 优先透传：
    # segments/tables 仍是 legacy 映射路径的回退。纯增量字段，缺省空元组，
    # 既有构造不受影响。
    blocks: tuple[ParsedBlock, ...] = ()
    # 文档声明的总页数（PDF 由 pypdf 确认、图片为 1）；None = 无法确认，
    # 质量门禁的页覆盖判定据此跳过（fail-closed 到"不判定"，绝不假定已覆盖）。
    page_total: int | None = None


# ParsedBlock 与 ParsedSegment 并存是刻意的兼容设计：ParsedSegment 服务于现有解析器契约，
# ParsedBlock 是清洗/分块链路的统一版面块；LayoutNormalizer（后续任务）把前者映射为后者。


@dataclass(frozen=True, slots=True)
class SourceOffset:
    """raw↔clean 文本偏移映射（Task 2 清洗器的实现契约）。

    - 偏移量为块内局部偏移：raw_start/raw_end 指向 raw 块自身文本，
      clean_start/clean_end 指向对应 cleaned 块自身文本，[start, end) 切片语义。
    - v1 清洗器保持 raw↔clean 块一一对应（只原地归一化并打 flag，不合并/不拆分块），
      两块共享同一 block_id。
    - 未来若引入合并/拆分：必须为每一对 raw↔clean 输出一条 SourceOffset，
      并以 cleaning flag 显式标记。
    """

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
    confidence: float | None = 1.0
    source_anchor: str | None = None


@dataclass(frozen=True, slots=True)
class CleanedDocument:
    """清洗结果：原文永不覆盖；v1 保持 raw/clean 元组等长且按 block_id 一一配对。"""

    raw_blocks: tuple[ParsedBlock, ...]
    cleaned_blocks: tuple[ParsedBlock, ...]
    source_offsets: tuple[SourceOffset, ...]
    cleaning_flags: tuple[str, ...]
    cleaner_version: str
    # 声明总页数（来自 ParsedDocument.page_total）；None = 不可确认 => 门禁不判定页覆盖。
    page_total: int | None = None


@dataclass(frozen=True, slots=True)
class EvidenceUnitDraft:
    text: str
    kind: EvidenceKind
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    source_anchor: str | None = None
    language: str = "und"
    # None = 解析/OCR 来源未提供置信度（不可判定），由 QualityGate 判为需复核。
    confidence: float | None = 1.0
    # 版本化增量字段（原文/清洗文本分层保留，原文永不覆盖；分块与质量结果可回放）。
    # 缺省值保持既有构造方式不变：legacy 草稿未经过清洗/分块/质量门禁。
    raw_text: str | None = None
    clean_text: str | None = None
    section_path: tuple[str, ...] = ()
    parent_block_id: str | None = None
    chunk_index: int | None = None
    parser_version: str | None = None
    cleaner_version: str | None = None
    chunker_version: str | None = None
    quality_flags: tuple[str, ...] = ()
    retrieval_eligible: bool = True

    def to_dict(self) -> dict[str, object]:
        """JSON-safe writer payload (tuples serialized as lists)."""
        return {
            "text": self.text,
            "kind": self.kind.value if isinstance(self.kind, str) else str(self.kind),
            "page_number": self.page_number,
            "sheet_name": self.sheet_name,
            "cell_range": self.cell_range,
            "source_anchor": self.source_anchor,
            "language": self.language,
            "confidence": self.confidence,
            "raw_text": self.raw_text,
            "clean_text": self.clean_text,
            "section_path": list(self.section_path),
            "parent_block_id": self.parent_block_id,
            "chunk_index": self.chunk_index,
            "parser_version": self.parser_version,
            "cleaner_version": self.cleaner_version,
            "chunker_version": self.chunker_version,
            "quality_flags": list(self.quality_flags),
            "retrieval_eligible": self.retrieval_eligible,
        }


@dataclass(frozen=True, slots=True)
class MedicalFactDraft:
    name: str
    value: str
    unit: str | None
    reference_range: str | None
    collection_time: object | None
    abnormal_flag: str | None
    source_page: int | None
    source_anchor: str
    evidence_index: int
    confidence: float


@dataclass(slots=True)
class FactExtractionResult:
    facts: list[MedicalFactDraft] = field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParseResult:
    raw_text: str
    evidence: list[EvidenceUnitDraft]
    facts: list[MedicalFactDraft]
    tables: list[ParsedTable] = field(default_factory=list)
    language: str = "und"
    parser_version: str = "parser-v1"
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    # 真实清洗产物（raw↔clean 分层、清洗 flag 与 source_offsets）。质量门禁优先读取
    # 它，而不是从 evidence 反推；legacy/手工构造的 ParseResult 保持 None 由门禁回退。
    cleaned: CleanedDocument | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "raw_text_length": len(self.raw_text),
            "evidence_count": len(self.evidence),
            "fact_count": len(self.facts),
            "language": self.language,
            "parser_version": self.parser_version,
            "needs_review": self.needs_review,
            "review_reasons": list(self.review_reasons),
        }


class TextParser(Protocol):
    def parse(self, file: BinaryIO, file_type: str) -> ParsedDocument: ...


class OCRParser(Protocol):
    def parse_images(self, file: BinaryIO, language_hints: list[str]) -> ParsedDocument: ...


class TableParser(Protocol):
    def extract(self, parsed: ParsedDocument) -> list[ParsedTable]: ...


class MedicalFactExtractor(Protocol):
    def extract(self, evidence: list[EvidenceUnitDraft]) -> FactExtractionResult: ...


def as_bytes_io(file: BinaryIO) -> BytesIO:
    if hasattr(file, "seek"):
        file.seek(0)
    return BytesIO(file.read())
