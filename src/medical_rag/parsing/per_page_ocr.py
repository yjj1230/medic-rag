"""Per-page OCR backend + page-identity-preserving OCR transport (Task 11 seam).

The golden evaluator (``eval/golden.py``) injects synthetic ``ParsedBlock`` so
the cleaner's ``ocr_source`` stays ``False`` and ``FLAG_OCR_NOISE_CHECKED`` is
never exercised.  This module closes that seam with an OCR-backed adapter that
**preserves page identity** and produces an OCR-origin :class:`ParsedDocument`
whose ``file_type`` the cleaner recognises (``ocr_source=True``), so the OCR
noise check actually runs and page-level provenance survives.

Backend choice (KEEP-IT-DETERMINISTIC): the repository has no PDF→image raster
engine (no pypdfium2/pymupdf/pdf2image) and no real OCR engine, so this uses the
**existing deterministic OCR fixture transport** from the test adapters, driven
one page at a time.  This is the buildable, testable form of the "逐页 OCR 后端"
the plan names; a production renderer can implement :class:`PerPageOCRBackend` /
:class:`OCRParser` without changing callers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import BinaryIO, Protocol

from medical_rag.parsing.adapters import (
    PARSER_VERSION_OCR,
    detect_language,
    should_ocr_page,
)
from medical_rag.parsing.contracts import ParsedBlock, ParsedDocument, ParsedSegment

# Page-level block ID prefix for OCR output (page identity is the key fix vs the
# legacy ``_build_ocr_blocks`` which hard-codes page 1).
_PAGE_BLOCK_ID = "p{page}-ocr-{index}"


class PerPageOCRBackend(Protocol):
    """Recognise the text of one page at a time (returns OCR text lines)."""

    page_count: int

    def recognize_page(self, page_index: int) -> list[str]: ...  # noqa: E704


@dataclass(frozen=True, slots=True)
class DeterministicPerPageOCRBackend:
    """Deterministic per-page OCR using the fixture transport.

    ``pages`` maps a 1-based page index to its OCR text lines; ``page_count`` is
    the total number of pages in the scanned document.  A missing page yields no
    text (``[]``), which the parser treats as blank.
    """

    pages: dict[int, list[str]]
    page_count: int

    def recognize_page(self, page_index: int) -> list[str]:
        return list(self.pages.get(page_index, ()))


def _ocr_blocks_for_page(
    page_index: int, lines: list[str], language_hints: list[str]
) -> list[ParsedBlock]:
    """Build one-or-more blocks for an OCR'd page with page identity preserved.

    ``confidence=None``：``PerPageOCRBackend.recognize_page`` 只返回文本行，**没有**
    任何真实置信度可透传。旧实现写死 0.95（伪造的测量值）会让质量门禁把它当成
    "测出来是 0.95"而放行；None 表示"不可判定"，由 QualityGate 判为需复核。
    """
    text = "\n".join(line for line in lines if line.strip())
    if not text.strip():
        return []
    language = detect_language(text)
    if language == "und" and language_hints:
        language = language_hints[0]
    blocks: list[ParsedBlock] = []
    for offset, line in enumerate(lines):
        if not line.strip():
            continue
        blocks.append(
            ParsedBlock(
                block_id=_PAGE_BLOCK_ID.format(page=page_index, index=offset),
                block_type="paragraph",
                raw_text=line,
                page_number=page_index,
                source_anchor=f"p{page_index}",
                language=language,
                confidence=None,
                reading_order=((page_index - 1) * 100) + offset,
            )
        )
    return blocks


class PerPageOCRParser:
    """OCRParser adapter that preserves page identity in per-page blocks.

    Implements the existing :class:`OCRParser` protocol (``parse_images``) so it
    can slot into ``ParserAdapters.ocr``.  The returned :class:`ParsedDocument`
    uses ``file_type="image"`` so ``DocumentCleaner`` marks it ``ocr_source=True``
    and emits ``FLAG_OCR_NOISE_CHECKED``.
    """

    def __init__(self, backend: PerPageOCRBackend) -> None:
        self.backend = backend

    def parse_images(self, file: BinaryIO, language_hints: list[str]) -> ParsedDocument:
        del file
        segments: list[ParsedSegment] = []
        blocks: list[ParsedBlock] = []
        for page_index in range(1, self.backend.page_count + 1):
            lines = self.backend.recognize_page(page_index)
            for block in _ocr_blocks_for_page(page_index, lines, language_hints):
                blocks.append(block)
                segments.append(
                    ParsedSegment(
                        text=block.raw_text,
                        page_number=page_index,
                        source_anchor=block.source_anchor,
                        language=block.language,
                        confidence=block.confidence,
                    )
                )
        raw_text = "\n".join(block.raw_text for block in blocks)
        return ParsedDocument(
            file_type="image",
            raw_text=raw_text,
            segments=tuple(segments),
            language=detect_language(raw_text) or (language_hints[0] if language_hints else "und"),
            parser_version=PARSER_VERSION_OCR,
            blocks=tuple(blocks),
        )


# OCR 页级锚点约定（与 ``_ocr_blocks_for_page`` 一致）：``p{page}``。
_OCR_PAGE_ANCHOR = "p{page}"


def _page_already_backfilled(blocks: list[ParsedBlock], page_index: int) -> bool:
    """该页是否已有 OCR 产物（同一锚点约定）——重复补扫会导致双份入索引。"""
    anchor = _OCR_PAGE_ANCHOR.format(page=page_index)
    return any(
        block.page_number == page_index and (block.source_anchor or "") == anchor
        for block in blocks
    )


def ocr_backfill_pdf(
    parsed: ParsedDocument,
    backend: PerPageOCRBackend,
    page_total: int,
    language_hints: list[str],
) -> ParsedDocument:
    """Upgrade mixed-PDF fallback from "raise" to per-page OCR backfill.

    Keeps the native blocks for pages that already have usable text and adds
    per-page OCR blocks (correct ``page_number`` / ``p{page}`` anchors) for the
    deficient pages.  Whatever pages were detected as deficient are combined
    with the native pages so no OCR-able content is silently dropped.  The
    document is re-labelled ``ocr-parser-v2`` because it required OCR.

    两条保护（缺一即可能双份入索引）：
    - 每页补扫前**重新判定**该页是否仍缺文本：原生文本（含表格文本）已足够的页
      绝不重扫，即使它出现在调用方算出的 deficient 列表里；
    - 已有 OCR 产物的页直接跳过（重复调用/重复并入幂等）。
    """
    deficient = [
        page
        for page in range(1, page_total + 1)
        if should_ocr_page(parsed.blocks, page)
    ]
    if not deficient:
        return parsed
    merged_blocks = list(parsed.blocks)
    extra_segments: list[ParsedSegment] = []
    for page_index in deficient:
        if not should_ocr_page(tuple(merged_blocks), page_index):
            # 该页已有可用原生文本（例如整页原生表格）：跳过，绝不重复 OCR。
            continue
        if _page_already_backfilled(merged_blocks, page_index):
            continue
        lines = backend.recognize_page(page_index)
        for block in _ocr_blocks_for_page(page_index, lines, language_hints):
            # Never duplicate an existing native block for the same anchor.
            if any(existing.source_anchor == block.source_anchor for existing in merged_blocks):
                continue
            merged_blocks.append(block)
            extra_segments.append(
                ParsedSegment(
                    text=block.raw_text,
                    page_number=page_index,
                    source_anchor=block.source_anchor,
                    language=block.language,
                    confidence=block.confidence,
                )
            )
    if len(merged_blocks) == len(parsed.blocks) and not extra_segments:
        # 没有补扫任何页 => 文档未被 OCR 触及：保持原生版本标签（不谎报 OCR）。
        return parsed
    merged_blocks.sort(key=lambda block: (block.page_number or 0, block.reading_order))
    segments = list(parsed.segments) + extra_segments
    raw_text = "\n".join(block.raw_text for block in merged_blocks)
    return replace(
        parsed,
        blocks=tuple(merged_blocks),
        segments=tuple(segments),
        raw_text=raw_text,
        parser_version=PARSER_VERSION_OCR,
    )


__all__ = [
    "DeterministicPerPageOCRBackend",
    "PerPageOCRBackend",
    "PerPageOCRParser",
    "ocr_backfill_pdf",
]
