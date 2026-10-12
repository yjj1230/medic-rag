"""legacy ParsedSegment/ParsedTable → ParsedBlock 的确定性版面映射。

本模块拥有唯一的 EvidenceKind → block_type 映射常量；其他模块不得复制。
"""

from __future__ import annotations

from medical_rag.domain.enums import EvidenceKind
from medical_rag.parsing.contracts import ParsedBlock, ParsedDocument, ParsedSegment, ParsedTable

# 唯一的 EvidenceKind → ParsedBlock.block_type 映射（LayoutNormalizer 所有）。
BLOCK_TYPE_BY_KIND: dict[EvidenceKind, str] = {
    EvidenceKind.REPORT_TEXT: "paragraph",
    EvidenceKind.REPORT_TABLE: "table",
    EvidenceKind.KNOWLEDGE_CHUNK: "paragraph",
}

_BLOCK_ID_FORMAT = "blk-{index:04d}"


def make_block_id(index: int) -> str:
    """按阅读序号生成块 ID（同一文档内唯一）。

    block_id 只由 LayoutNormalizer / TextSourceParser 分配；解析适配器沿用各自
    的页级/单元格级 source_anchor。超过 9999 时 ID 按字典序变宽，因此消费方
    必须按 reading_order 排序，不得依赖 ID 的字典序。
    """
    return _BLOCK_ID_FORMAT.format(index=index)


class LayoutNormalizer:
    """把 legacy ParsedSegment/ParsedTable 确定性地映射为 ParsedBlock 值。

    - 解析适配器预产的 `ParsedDocument.blocks` 优先透传（blocks win）：非空时
      整组直接返回，不再从 segments/tables 重新派生 —— 版面职责归适配器，
      LayoutNormalizer 只做转发。同一页/锚点内容不会因 blocks 与 segments 并存
      而重复进入清洗链路（blocks 优先级天然去重）。
    - block_id / reading_order 按映射顺序确定（blk-0000 起，仅 legacy 回退路径）；
    - source_anchor 只透传，缺失时保持 None（fail-closed，绝不推断/编造）；
    - 无任何 segment/table 时回退：把 raw_text 作为一个 paragraph 块承载（同样不造锚点），
      保证清洗链路对 raw_text-only 的文档（如 OCR 整页文本）仍有块可清洗。
    """

    def normalize(self, parsed_document: ParsedDocument) -> tuple[ParsedBlock, ...]:
        if parsed_document.blocks:
            return tuple(parsed_document.blocks)
        blocks = [
            self._block_from_segment(index, segment)
            for index, segment in enumerate(parsed_document.segments)
        ]
        # 适配器对 Word/Excel 会同时产出 REPORT_TABLE segment 与 ParsedTable；
        # 同一 source_anchor 只保留一个块，避免表格内容重复进入清洗链路。
        covered_anchors = {
            segment.source_anchor
            for segment in parsed_document.segments
            if segment.source_anchor is not None
        }
        for table in parsed_document.tables:
            if table.source_anchor is not None and table.source_anchor in covered_anchors:
                continue
            blocks.append(self._block_from_table(len(blocks), table))
        if not blocks and parsed_document.raw_text.strip():
            blocks.append(self._fallback_raw_text_block(parsed_document))
        return tuple(blocks)

    @staticmethod
    def _block_from_segment(index: int, segment: ParsedSegment) -> ParsedBlock:
        return ParsedBlock(
            block_id=make_block_id(index),
            block_type=BLOCK_TYPE_BY_KIND[segment.kind],
            raw_text=segment.text,
            page_number=segment.page_number,
            sheet_name=segment.sheet_name,
            cell_range=segment.cell_range,
            section_path=(),
            reading_order=index,
            language=segment.language,
            confidence=segment.confidence,
            source_anchor=segment.source_anchor,
        )

    @staticmethod
    def _block_from_table(index: int, table: ParsedTable) -> ParsedBlock:
        return ParsedBlock(
            block_id=make_block_id(index),
            block_type="table",
            raw_text=table.text,
            page_number=table.page_number,
            sheet_name=table.sheet_name,
            cell_range=table.cell_range,
            section_path=(),
            reading_order=index,
            language=table.language,
            confidence=table.confidence,
            source_anchor=table.source_anchor,
        )

    @staticmethod
    def _fallback_raw_text_block(parsed_document: ParsedDocument) -> ParsedBlock:
        return ParsedBlock(
            block_id=make_block_id(0),
            block_type="paragraph",
            raw_text=parsed_document.raw_text,
            language=parsed_document.language,
        )
