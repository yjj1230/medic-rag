from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from medical_rag.parsing.contracts import EvidenceUnitDraft


def _serialize_index_value(value: Any) -> Any:
    if isinstance(value, (UUID, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value


@dataclass(frozen=True, slots=True)
class IndexRecord:
    """Normalized payload shared by public and private index writers.

    The broad, additive shape lets each index retain its existing source
    metadata while exposing one stable lifecycle and version contract to
    writers.  Lifecycle flags are intentionally explicit and public records
    are only valid when all four public-safe defaults are present.
    """

    id: str
    text: str
    visibility: str = "public"
    tenant_id: UUID | None = None
    patient_id: UUID | None = None
    document_id: UUID | str | None = None
    document_version_id: UUID | str | None = None
    evidence_unit_id: UUID | str | None = None
    source_id: str | None = None
    source_title: str | None = None
    source_version: str | None = None
    language: str | None = None
    location: str | None = None
    page_number: int | None = None
    section: str | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    source_anchor: str | None = None
    url: str | None = None
    confidence: float | None = None
    content_hash: str | None = None
    parser_version: str | None = None
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None
    embedding_version: str | None = None
    embedding_dimensions: int | None = None
    index_version: str | None = None
    retrieval_eligible: bool = True
    source_active: bool = True
    deleted: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # --- Task 5: provenance mirrors (additive, JSON-safe via _serialize_index_value) ---
    raw_text: str | None = None
    clean_text: str | None = None
    section_path: tuple[str, ...] | None = None
    parent_block_id: str | None = None
    chunk_index: int | None = None
    quality_flags: tuple[str, ...] | None = None
    cleaner_version: str | None = None
    chunker_version: str | None = None
    extractor_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("index record id cannot be empty")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("index record text cannot be empty")
        visibility = self.visibility.value if isinstance(self.visibility, StrEnum) else self.visibility
        if visibility not in {"public", "private"}:
            raise ValueError("index record visibility must be public or private")
        if visibility != self.visibility:
            object.__setattr__(self, "visibility", visibility)
        for name in ("retrieval_eligible", "source_active", "deleted"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"index record {name} must be a boolean")
        if visibility == "private" and (self.tenant_id is None or self.patient_id is None):
            raise ValueError("private index records require tenant and patient ACL fields")
        if visibility == "public" and (
            not self.retrieval_eligible or not self.source_active or self.deleted
        ):
            raise ValueError("public index records must be active, eligible, and not deleted")

    def to_dict(self) -> dict[str, Any]:
        """Return an explicit, JSON-friendly writer payload."""

        return {
            name: _serialize_index_value(getattr(self, name))
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class Citation:
    source_id: str
    title: str
    version: str
    location: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "version": self.version,
            "location": self.location,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class SourceRegistration:
    institution: str
    title: str
    source_version: str
    language: str
    scope: str
    publication_date: date | None = None
    reviewer: str | None = None
    active: bool = False


@dataclass(frozen=True, slots=True)
class SourceDocument:
    title: str
    source_version: str
    language: str
    content: str
    location: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeChunkDraft:
    text: str
    content_hash: str
    source_version: str
    language: str
    location: str | None = None
    page_number: int | None = None
    section: str | None = None
    url: str | None = None

    @classmethod
    def from_evidence(
        cls,
        draft: "EvidenceUnitDraft",
        source: SourceDocument,
        content_hash: str,
    ) -> "KnowledgeChunkDraft":
        """把共享链的语义分块值映射回公共知识草稿，保持 legacy 语义不变。

        - text 取清洗文本（clean_text，缺省回退 text）；
        - content_hash / location / section / URL 语义与原 KnowledgeChunker 一致：
          section = section_path[-1]（≈ legacy 扁平 section）；location 缺省回退 section；
          标题块是边界而非块内容，因此从不成为 text。
        """
        text = draft.clean_text if draft.clean_text is not None else draft.text
        section = draft.section_path[-1] if draft.section_path else None
        return cls(
            text=text,
            content_hash=content_hash,
            source_version=source.source_version,
            language=source.language,
            location=source.location or section,
            page_number=draft.page_number,
            section=section,
            url=source.url,
        )

    def citation(self, source_id: str, title: str) -> Citation:
        return Citation(
            source_id=source_id,
            title=title,
            version=self.source_version,
            location=self.location,
            url=self.url,
        )


@dataclass(frozen=True, slots=True)
class PublicIndexRecord:
    id: str
    text: str
    source_id: str
    source_title: str
    source_version: str
    language: str
    location: str | None
    page_number: int | None
    section: str | None
    url: str | None
    content_hash: str
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    index_version: str | None = None
    # Explicit lifecycle/visibility markers are required by the fail-closed
    # retrieval mapping; defaults preserve existing construction sites.
    visibility: str = "public"
    retrieval_eligible: bool = True
    source_active: bool = True
    deleted: bool = False
    parser_version: str | None = "knowledge-parser-v1"
    cleaner_version: str | None = None
    chunker_version: str | None = None
    extractor_version: str | None = None
    embedding_version: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Public source identity is not a document identity.  Callers with a
    # document-level identifier may provide it explicitly; legacy callers do
    # not have one and remain represented by ``None``.
    document_id: UUID | str | None = None

    def __post_init__(self) -> None:
        if self.visibility != "public":
            raise ValueError("public index records must use public visibility")
        for name in ("retrieval_eligible", "source_active", "deleted"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"public index record {name} must be a boolean")
        if not self.retrieval_eligible or not self.source_active or self.deleted:
            raise ValueError("public index records must be active, eligible, and not deleted")

    def to_index_record(self) -> IndexRecord:
        """Normalize the legacy public record into the shared contract."""

        return IndexRecord(
            id=self.id,
            text=self.text,
            visibility=self.visibility,
            document_id=self.document_id,
            source_id=self.source_id,
            source_title=self.source_title,
            source_version=self.source_version,
            language=self.language,
            location=self.location,
            page_number=self.page_number,
            section=self.section,
            url=self.url,
            content_hash=self.content_hash,
            parser_version=self.parser_version,
            cleaner_version=self.cleaner_version,
            chunker_version=self.chunker_version,
            extractor_version=self.extractor_version,
            embedding=self.embedding,
            embedding_model=self.embedding_model,
            embedding_version=self.embedding_version,
            embedding_dimensions=self.embedding_dimensions,
            index_version=self.index_version,
            retrieval_eligible=self.retrieval_eligible,
            source_active=self.source_active,
            deleted=self.deleted,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize through the normalized contract with explicit lifecycle flags."""

        return self.to_index_record().to_dict()


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    created_chunks: int
    skipped_chunks: int
    indexed_chunks: int
    source_id: str


class PublicIndexWriter(Protocol):
    def upsert(self, records: list[IndexRecord]) -> None: ...

    # Legacy single-record seam retained for existing adapters/callers.
    def index_public_chunk(self, record: PublicIndexRecord) -> None: ...
