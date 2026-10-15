from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from medical_rag.parsing.contracts import ParsedBlock


_LOCATOR_TYPES = frozenset(
    {
        "pdf_text",
        "pdf_image",
        "docx_paragraph",
        "xlsx_cell_range",
        "xls_cell_range",
        "image_region",
        "unknown",
    }
)


@dataclass(frozen=True, slots=True)
class SourceLocator:
    """块在其源文档中的稳定、JSON 安全位置。

    在应用的其他地方，``location`` 字符串仍是表示层面的兼容字段。本对象是
    持久化、引用、删除和对账所使用的权威、机器可读的定位器。
    """

    locator_type: str = "unknown"
    page_number: int | None = None
    page_total: int | None = None
    section_path: tuple[str, ...] = ()
    block_id: str | None = None
    reading_order: int | None = None
    chunk_index: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    source_anchor: str | None = None

    def __post_init__(self) -> None:
        if self.locator_type not in _LOCATOR_TYPES:
            raise ValueError(f"unsupported locator_type: {self.locator_type}")
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("page_number must be positive")
        if self.page_total is not None and self.page_total < 1:
            raise ValueError("page_total must be positive")
        if self.page_number is not None and self.page_total is not None:
            if self.page_number > self.page_total:
                raise ValueError("page_number cannot exceed page_total")
        if self.chunk_index is not None and self.chunk_index < 0:
            raise ValueError("chunk_index cannot be negative")
        if self.reading_order is not None and self.reading_order < 0:
            raise ValueError("reading_order cannot be negative")
        if self.char_start is not None and self.char_start < 0:
            raise ValueError("char_start cannot be negative")
        if self.char_end is not None and self.char_end < 0:
            raise ValueError("char_end cannot be negative")
        if self.char_start is not None and self.char_end is not None:
            if self.char_end < self.char_start:
                raise ValueError("char_end cannot be before char_start")
        if self.bbox is not None and len(self.bbox) != 4:
            raise ValueError("bbox must contain four coordinates")

    def build_anchor(self) -> str:
        """返回一个不嵌入源文本的确定性锚点。"""

        parts = [self.locator_type]
        if self.page_number is not None:
            parts.append(f"page:{self.page_number}")
        if self.sheet_name:
            parts.append(f"sheet:{self.sheet_name}")
        if self.cell_range:
            parts.append(f"range:{self.cell_range}")
        if self.block_id:
            parts.append(f"block:{self.block_id}")
        if self.reading_order is not None:
            parts.append(f"order:{self.reading_order}")
        if self.chunk_index is not None:
            parts.append(f"chunk:{self.chunk_index}")
        if self.char_start is not None and self.char_end is not None:
            parts.append(f"chars:{self.char_start}-{self.char_end}")
        if self.bbox is not None:
            parts.append("bbox:" + ",".join(f"{value:g}" for value in self.bbox))
        return "|".join(parts) if len(parts) > 1 else ""

    def validate(self) -> None:
        """拒绝无法标识任何源位置的定位器。"""

        if not (
            self.source_anchor
            or self.page_number is not None
            or self.section_path
            or self.block_id
            or self.cell_range
            or self.bbox is not None
        ):
            raise ValueError("source locator has no usable position")

    def to_dict(self) -> dict[str, object]:
        """返回带规范锚点的 JSON 安全表示。"""

        anchor = self.source_anchor or self.build_anchor() or None
        return {
            "locator_type": self.locator_type,
            "page_number": self.page_number,
            "page_total": self.page_total,
            "section_path": list(self.section_path),
            "block_id": self.block_id,
            "reading_order": self.reading_order,
            "chunk_index": self.chunk_index,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "sheet_name": self.sheet_name,
            "cell_range": self.cell_range,
            "source_anchor": anchor,
        }

    def display_location(self) -> str:
        """返回供 API/UI 展示的简洁、人类可读位置。"""

        parts: list[str] = []
        if self.page_number is not None:
            parts.append(f"第{self.page_number}页")
        if self.sheet_name:
            parts.append(f"Sheet:{self.sheet_name}")
        if self.cell_range:
            parts.append(f"单元格:{self.cell_range}")
        if self.section_path:
            parts.append("章节:" + " > ".join(self.section_path))
        if self.block_id and not parts:
            parts.append(f"区块:{self.block_id}")
        if self.bbox is not None and not parts:
            parts.append("图像区域:" + ",".join(f"{value:g}" for value in self.bbox))
        return "，".join(parts) or (self.source_anchor or "来源位置未知")


def locator_from_block(
    block: "ParsedBlock",
    chunk_index: int | None = None,
    *,
    cell_range: str | None = None,
    page_total: int | None = None,
) -> SourceLocator:
    """从规范化解析器块构建定位器。"""

    if block.block_type == "table" and block.sheet_name:
        locator_type = "xlsx_cell_range"
    elif block.block_type == "image":
        locator_type = "image_region" if block.bbox is not None else "pdf_image"
    elif block.page_number is not None:
        locator_type = "pdf_text"
    else:
        locator_type = "docx_paragraph"
    locator = SourceLocator(
        locator_type=locator_type,
        page_number=block.page_number,
        page_total=page_total,
        section_path=tuple(block.section_path),
        block_id=block.block_id,
        reading_order=block.reading_order,
        chunk_index=chunk_index,
        bbox=block.bbox,
        sheet_name=block.sheet_name,
        cell_range=cell_range if cell_range is not None else block.cell_range,
        source_anchor=block.source_anchor,
    )
    return locator
