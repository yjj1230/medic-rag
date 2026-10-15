from uuid import uuid4

from medical_rag.knowledge.contracts import IndexRecord, PublicIndexRecord
from medical_rag.provenance.contracts import SourceLocator
from medical_rag.retrieval.contracts import PrivateIndexRecord


def test_shared_index_record_serializes_structured_locator():
    locator = SourceLocator(
        locator_type="pdf_text",
        page_number=2,
        section_path=("检验结果",),
        block_id="b-2",
        chunk_index=1,
    )
    record = IndexRecord(id="chunk-1", text="结果", locator=locator)

    payload = record.to_dict()

    assert payload["locator"]["page_number"] == 2
    assert payload["locator"]["section_path"] == ["检验结果"]
    assert payload["locator"]["source_anchor"] == "pdf_text|page:2|block:b-2|chunk:1"


def test_public_and_private_records_keep_locator():
    locator = SourceLocator(locator_type="xlsx_cell_range", sheet_name="Sheet1", cell_range="A1:B2")
    public = PublicIndexRecord(
        id="pub-1",
        text="结果",
        source_id="source-1",
        source_title="指南",
        source_version="v1",
        language="zh",
        location=locator.display_location(),
        page_number=None,
        section=None,
        url=None,
        content_hash="hash",
        locator=locator,
    )
    private = PrivateIndexRecord(
        id="priv-1",
        text="结果",
        tenant_id=uuid4(),
        patient_id=uuid4(),
        document_id=uuid4(),
        document_version_id=uuid4(),
        evidence_unit_id=uuid4(),
        source_title="报告",
        source_version="v1",
        locator=locator,
    )

    assert public.to_index_record().locator == locator
    assert private.to_index_record().locator == locator
