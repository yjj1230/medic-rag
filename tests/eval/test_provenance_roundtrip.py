from uuid import uuid4

from medical_rag.knowledge.contracts import PublicIndexRecord
from medical_rag.provenance.contracts import SourceLocator
from medical_rag.retrieval.contracts import InMemorySearchIndex, SearchQuery, Visibility


def test_public_chunk_round_trips_locator_through_search_and_citation():
    locator = SourceLocator(
        locator_type="xlsx_cell_range",
        sheet_name="检验结果",
        cell_range="A1:B4",
        section_path=("血常规",),
        block_id="table-1",
        chunk_index=0,
    )
    record = PublicIndexRecord(
        id="chunk-1",
        text="白细胞 | 6.2",
        source_id="source-1",
        source_title="检验指南",
        source_version="v1",
        language="zh",
        location=locator.display_location(),
        page_number=None,
        section="血常规",
        url="https://example.test/source-1",
        content_hash="hash-1",
        locator=locator,
    )
    index = InMemorySearchIndex()
    index.upsert([record])

    hits = index.search(
        SearchQuery(text="白细胞", patient_id=None, top_k=1, include_private=False),
        tenant_id=uuid4(),
        patient_id=uuid4(),
        include_public=True,
        include_private=False,
        limit=1,
    )

    assert len(hits) == 1
    assert hits[0].visibility is Visibility.PUBLIC
    assert hits[0].locator == locator
    assert hits[0].citation.locator == locator
    assert hits[0].citation.location == "Sheet:检验结果，单元格:A1:B4，章节:血常规"
