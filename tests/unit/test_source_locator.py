from medical_rag.parsing.contracts import ParsedBlock
from medical_rag.provenance.contracts import SourceLocator, locator_from_block


def test_pdf_locator_round_trips_and_has_deterministic_anchor():
    block = ParsedBlock(
        block_id="block-7",
        block_type="paragraph",
        raw_text="血红蛋白 120 g/L",
        page_number=3,
        section_path=("血常规", "血红蛋白"),
        reading_order=7,
    )

    locator = locator_from_block(block, chunk_index=2, page_total=8)

    assert locator.build_anchor() == "pdf_text|page:3|block:block-7|order:7|chunk:2"
    assert locator.to_dict()["section_path"] == ["血常规", "血红蛋白"]
    assert locator.display_location() == "第3页，章节:血常规 > 血红蛋白"


def test_excel_locator_preserves_sheet_and_range():
    block = ParsedBlock(
        block_id="sheet-1",
        block_type="table",
        raw_text="项目 | 结果",
        sheet_name="检验结果",
        cell_range="A1:B4",
        reading_order=1,
    )

    locator = locator_from_block(block, chunk_index=0, cell_range="A1:B2")

    assert locator.locator_type == "xlsx_cell_range"
    assert locator.to_dict()["cell_range"] == "A1:B2"
    assert locator.display_location() == "Sheet:检验结果，单元格:A1:B2"


def test_locator_rejects_missing_position():
    locator = SourceLocator(locator_type="unknown")

    try:
        locator.validate()
    except ValueError as exc:
        assert "no usable position" in str(exc)
    else:  # pragma: no cover - assertion documents the contract
        raise AssertionError("an empty locator must be rejected")
