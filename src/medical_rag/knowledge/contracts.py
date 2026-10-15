from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from medical_rag.provenance.contracts import SourceLocator

if TYPE_CHECKING:
    from medical_rag.parsing.contracts import EvidenceUnitDraft


def _serialize_index_value(value: Any) -> Any:
    if isinstance(value, (UUID, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, SourceLocator):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_serialize_index_value(item) for item in value]
    if isinstance(value, list):
        return [_serialize_index_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize_index_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class IndexRecord:
    """公开与私有索引写入器共享的规范化载荷。

    宽泛、增量的形态让每个索引保留其既有源元数据，同时向写入器暴露一个稳定的
    生命周期与版本契约。生命周期标志有意显式化，且公开记录只有在四个公开安全
    默认值全部具备时才有效。
    """

    id: str
    text: str
    visibility: str = "public"
    tenant_id: UUID | None = None
    patient_id: UUID | None = None
    document_id: UUID | str | None = None
    document_version_id: UUID | str | None = None
    evidence_unit_id: UUID | str | None = None
    locator: SourceLocator | None = None
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
    # --- Task 5：溯源镜像（增量式，经 _serialize_index_value 保证 JSON 安全）---
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
        """返回显式、JSON 友好的写入器载荷。"""

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
    chunk_id: str | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    evidence_unit_id: str | None = None
    locator: SourceLocator | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "version": self.version,
            "location": self.location,
            "url": self.url,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "evidence_unit_id": self.evidence_unit_id,
            "locator": self.locator.to_dict() if self.locator is not None else None,
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
    locator: SourceLocator | None = None

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
            location=(
                source.location
                or (draft.locator.display_location() if draft.locator is not None else section)
            ),
            page_number=draft.page_number,
            section=section,
            url=source.url,
            locator=draft.locator,
        )

    def citation(self, source_id: str, title: str) -> Citation:
        return Citation(
            source_id=source_id,
            title=title,
            version=self.source_version,
            location=self.location,
            url=self.url,
            locator=self.locator,
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
    # fail-closed 检索映射要求显式的生命周期/可见性标记；
    # 默认值保留既有构造处的行为。
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
    # 公开源标识不是文档标识。持有文档级
    # 标识的调用方可以显式提供它；遗留调用方没有
    # 该标识，仍以 ``None`` 表示。
    document_id: UUID | str | None = None
    locator: SourceLocator | None = None

    def __post_init__(self) -> None:
        if self.visibility != "public":
            raise ValueError("public index records must use public visibility")
        for name in ("retrieval_eligible", "source_active", "deleted"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"public index record {name} must be a boolean")
        if not self.retrieval_eligible or not self.source_active or self.deleted:
            raise ValueError("public index records must be active, eligible, and not deleted")

    def to_index_record(self) -> IndexRecord:
        """把遗留公开记录规范化为共享契约。"""

        return IndexRecord(
            id=self.id,
            text=self.text,
            visibility=self.visibility,
            document_id=self.document_id,
            locator=self.locator,
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
            section_path=self.locator.section_path if self.locator is not None else None,
            parent_block_id=self.locator.block_id if self.locator is not None else None,
            chunk_index=self.locator.chunk_index if self.locator is not None else None,
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
        """通过带显式生命周期标志的规范化契约进行序列化。"""

        return self.to_index_record().to_dict()


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    created_chunks: int
    skipped_chunks: int
    indexed_chunks: int
    source_id: str


class PublicIndexWriter(Protocol):
    def upsert(self, records: list[IndexRecord]) -> None: ...

    # 为既有适配器/调用方保留的遗留单记录接缝。
    def index_public_chunk(self, record: PublicIndexRecord) -> None: ...
