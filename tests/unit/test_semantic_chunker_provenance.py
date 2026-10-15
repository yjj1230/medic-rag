from medical_rag.domain.enums import EvidenceKind
from medical_rag.knowledge.semantic_chunker import SemanticChunker
from medical_rag.parsing.contracts import CleanedDocument, ParsedBlock, SourceOffset


def test_chunker_preserves_page_section_block_and_chunk_index():
    block = ParsedBlock(
        block_id="b-1",
        block_type="paragraph",
        raw_text="血红蛋白结果为 120 g/L。",
        page_number=3,
        section_path=("血常规", "血红蛋白"),
        reading_order=4,
    )
    document = CleanedDocument(
        raw_blocks=(block,),
        cleaned_blocks=(block,),
        source_offsets=(SourceOffset("b-1", 0, len(block.raw_text), 0, len(block.raw_text)),),
        cleaning_flags=(),
        cleaner_version="cleaner-v1",
        page_total=8,
    )

    drafts = SemanticChunker(target_tokens=10, min_tokens=1, max_tokens=20).chunk(document)

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.kind == EvidenceKind.REPORT_TEXT
    assert draft.locator is not None
    assert draft.locator.page_number == 3
    assert draft.locator.page_total == 8
    assert draft.locator.section_path == ("血常规", "血红蛋白")
    assert draft.locator.block_id == "b-1"
    assert draft.locator.chunk_index == 0
    assert draft.source_anchor == draft.locator.build_anchor()
