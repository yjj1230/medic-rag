from __future__ import annotations

from dataclasses import asdict, replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.domain.models import KnowledgeChunk, KnowledgeSource, VersionBundle
from medical_rag.knowledge.chunker import KnowledgeChunker
from medical_rag.knowledge.contracts import (
    IngestReceipt,
    PublicIndexRecord,
    SourceDocument,
    SourceRegistration,
)
from medical_rag.models.contracts import EmbeddingPipeline
from medical_rag.indexing.outbox import OutboxService


class KnowledgeSourceRegistry:
    def __init__(self, session: Session, outbox_service: OutboxService | None = None) -> None:
        self.session = session
        self.outbox = outbox_service or OutboxService(session)

    def register(self, source: SourceRegistration) -> KnowledgeSource:
        required = {
            "institution": source.institution,
            "title": source.title,
            "source_version": source.source_version,
            "language": source.language,
            "scope": source.scope,
        }
        if any(not value.strip() for value in required.values()):
            raise ValueError("知识源机构、标题、版本、语言和适用范围均不能为空")
        if source.publication_date is None:
            raise ValueError("知识源必须提供发布日期")
        if source.active and not source.reviewer:
            raise ValueError("启用知识源必须提供医学审核人")
        existing = self.session.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.title == source.title,
                KnowledgeSource.source_version == source.source_version,
            )
        )
        if existing is not None:
            return existing
        entity = KnowledgeSource(**asdict(source))
        self.session.add(entity)
        self.session.flush()
        if source.active:
            self._deactivate_previous_versions(entity)
        return entity

    def _deactivate_previous_versions(self, current: KnowledgeSource) -> None:
        """停用同一标题源的较旧活动版本。"""

        previous = self.session.scalars(
            select(KnowledgeSource).where(
                KnowledgeSource.title == current.title,
                KnowledgeSource.id != current.id,
                KnowledgeSource.active.is_(True),
            )
        ).all()
        for item in previous:
            item.active = False
            chunks = self.session.scalars(
                select(KnowledgeChunk).where(KnowledgeChunk.source_id == item.id)
            ).all()
            for chunk in chunks:
                chunk.active = False
            self.outbox.create_lifecycle_event(
                event_type="knowledge.source_version_replaced",
                aggregate_id=item.id,
                idempotency_key=f"knowledge-source:{item.id}:replaced-by:{current.id}",
                payload={
                    "source_id": str(item.id),
                    "replacement_source_id": str(current.id),
                    "source_version": item.source_version,
                    "replacement_version": current.source_version,
                    "status": "inactive",
                },
            )

    def set_active(self, source_id: UUID, active: bool) -> KnowledgeSource:
        source = self.session.get(KnowledgeSource, source_id)
        if source is None:
            raise ValueError("知识源不存在")
        if active and not source.reviewer:
            raise ValueError("启用知识源必须提供医学审核人")
        changed = source.active != active
        source.active = active
        chunks = self.session.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id)
        ).all()
        for chunk in chunks:
            chunk.active = active
        if changed:
            event_type = "knowledge.source.activated" if active else "knowledge.source.deactivated"
            self.outbox.create_lifecycle_event(
                event_type=event_type,
                aggregate_id=source.id,
                idempotency_key=f"knowledge-source:{source.id}:{'active' if active else 'inactive'}:{source.updated_at}",
                payload={
                    "source_id": str(source.id),
                    "source_version": source.source_version,
                    "active": active,
                    "chunk_count": len(chunks),
                },
            )
        self.session.commit()
        return source

    def deactivate(self, source_id: UUID) -> KnowledgeSource:
        return self.set_active(source_id, False)

    # 显式命名的别名让生命周期调用方读起来清晰，并为不需要
    # registry 对象的 API 处理器保留一个小的公开接缝。
    deactivate_source = deactivate


class KnowledgeIngestService:
    def __init__(
        self,
        session: Session,
        chunker: KnowledgeChunker | None = None,
        public_index: object | None = None,
        embedding_pipeline: EmbeddingPipeline | None = None,
        embedding_model_name: str = "text-embedding-3-small",
        embedding_index_version: str = "public-v1",
        allow_unembedded: bool = False,
        parser_version: str = "knowledge-parser-v1",
        outbox_service: OutboxService | None = None,
    ) -> None:
        self.session = session
        self.chunker = chunker or KnowledgeChunker()
        self.public_index = public_index
        self.embedding_pipeline = embedding_pipeline
        self.embedding_model_name = embedding_model_name
        self.embedding_index_version = embedding_index_version
        self.allow_unembedded = allow_unembedded
        self.parser_version = parser_version
        self.outbox = outbox_service or OutboxService(session)

    def deactivate_source(self, source_id: UUID) -> KnowledgeSource:
        return KnowledgeSourceRegistry(self.session, self.outbox).deactivate(source_id)

    def activate_source(self, source_id: UUID) -> KnowledgeSource:
        return KnowledgeSourceRegistry(self.session, self.outbox).set_active(source_id, True)

    def _version_bundle(self) -> VersionBundle:
        """为公开 ingest 路径构建唯一的 VersionBundle。

        公开记录与私有证据共享同一个值对象形态，但有意不携带私有 ACL 字段。
        cleaner/chunker 阶段版本从已配置的共享块链读取，因此阶段升级在每一条
        公开索引记录上都能被观测到。
        """
        semantic = getattr(self.chunker, "_semantic", None)
        cleaner = getattr(self.chunker, "_cleaner", None)
        return VersionBundle(
            parser_version=self.parser_version,
            cleaner_version=getattr(cleaner, "version", None),
            chunker_version=getattr(semantic, "version", None),
            extractor_version=None,
            embedding_model=self.embedding_model_name,
            embedding_dimensions=None,
            index_version=self.embedding_index_version,
        )

    def _index(self, record: PublicIndexRecord) -> None:
        if self.public_index is None:
            return
        if hasattr(self.public_index, "index_public_chunk"):
            self.public_index.index_public_chunk(record)
            return
        if hasattr(self.public_index, "append"):
            self.public_index.append(record.to_dict())
            return
        raise TypeError("public_index 必须提供 index_public_chunk 或 append")

    def _index_batch(self, records: list[PublicIndexRecord]) -> None:
        if self.public_index is not None and hasattr(self.public_index, "upsert"):
            self.public_index.upsert([record.to_index_record() for record in records])
            return
        for record in records:
            self._index(record)

    def ingest(self, source_id: UUID, content: SourceDocument) -> IngestReceipt:
        source = self.session.get(KnowledgeSource, source_id)
        if source is None:
            raise ValueError("知识源不存在")
        if not source.active or not source.reviewer:
            raise ValueError("知识源未启用或尚未完成医学审核")
        if content.source_version != source.source_version:
            raise ValueError("内容版本与登记的知识源版本不一致")
        if content.title != source.title:
            raise ValueError("内容标题与登记的知识源标题不一致")

        created = skipped = indexed = 0
        bundle = self._version_bundle()
        drafts = list(self.chunker.split(content))
        current_hashes = {draft.content_hash for draft in drafts}
        existing_by_hash = {
            chunk.content_hash: chunk
            for chunk in self.session.scalars(
                select(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id)
            ).all()
        }
        # 医学安全：新修订版不再包含的内容必须
        # 停止可被检索。只依据"哈希已存在"跳过，会让
        # 重新摄取变得单调 —— 一段被移除的段落（例如"药物 X 不再
        # 被推荐"）会永远保持活动、可引用、可搜索，而
        # 对账抓不到它，因为它仍是一行表记录。
        for content_hash, chunk in existing_by_hash.items():
            if content_hash in current_hashes or not chunk.active:
                continue
            chunk.active = False
            self.outbox.create_lifecycle_event(
                event_type="knowledge.chunk_deactivated",
                aggregate_id=source.id,
                idempotency_key=f"knowledge-chunk:{chunk.id}:inactive:{content_hash}",
                payload={
                    "source_id": str(source.id),
                    "chunk_id": str(chunk.id),
                    "content_hash": content_hash,
                    "source_version": source.source_version,
                    "active": False,
                    "reason": "absent_from_current_revision",
                },
            )

        pending: list[tuple[KnowledgeChunk, PublicIndexRecord]] = []
        for draft in drafts:
            existing = existing_by_hash.get(draft.content_hash)
            if existing is not None and existing.active:
                skipped += 1
                continue
            if existing is not None:
                # 重新激活的内容：保留该行（其标识和哈希未变）
                # 并重新发出它，这样索引也会刷新。
                chunk = existing
                created += 1
            else:
                chunk = KnowledgeChunk(
                    source_id=source.id,
                    content_hash=draft.content_hash,
                    text=draft.text,
                    location=draft.location,
                    page_number=draft.page_number,
                    section=draft.section,
                    locator=draft.locator.to_dict() if draft.locator is not None else None,
                    url=draft.url,
                    active=False,
                )
                self.session.add(chunk)
                self.session.flush()
                existing_by_hash[draft.content_hash] = chunk
                created += 1
            if draft.locator is not None and chunk.locator is None:
                chunk.locator = draft.locator.to_dict()
            record = PublicIndexRecord(
                id=str(chunk.id),
                text=chunk.text,
                source_id=str(source.id),
                source_title=source.title,
                source_version=source.source_version,
                language=source.language,
                location=chunk.location,
                page_number=chunk.page_number,
                section=chunk.section,
                url=chunk.url,
                content_hash=chunk.content_hash,
                visibility="public",
                retrieval_eligible=True,
                source_active=True,
                deleted=False,
                parser_version=bundle.parser_version,
                cleaner_version=bundle.cleaner_version,
                chunker_version=bundle.chunker_version,
                extractor_version=bundle.extractor_version,
                locator=chunk._locator_object(),
            )
            pending.append((chunk, record))
        if pending and self.embedding_pipeline is not None:
            vectors = self.embedding_pipeline.embed_records(
                [record.text for _, record in pending], self.embedding_model_name
            )
            indexed_records: list[PublicIndexRecord] = []
            for index, (_chunk, record) in enumerate(pending):
                record = replace(
                    record,
                    embedding=vectors[index],
                    embedding_model=self.embedding_model_name,
                    embedding_dimensions=self.embedding_pipeline.dimensions,
                    index_version=self.embedding_index_version,
                )
                indexed_records.append(record)
                indexed += 1
        else:
            if pending and self.public_index is not None and not self.allow_unembedded:
                raise RuntimeError("embedding pipeline is required before public index upsert")
            indexed_records = [record for _, record in pending]
            indexed = len(pending)

        if pending:
            # Commit the database state before causing an external index side effect.
            # Chunks remain inactive until the index write succeeds, so a failed
            # OpenSearch write is retryable and a failed DB commit cannot leave a
            # ghost record in the index.
            self.session.commit()
            self._index_batch(indexed_records)
            for chunk, _record in pending:
                chunk.active = True

        self.session.commit()
        return IngestReceipt(
            created_chunks=created,
            skipped_chunks=skipped,
            indexed_chunks=indexed,
            source_id=str(source.id),
        )

    def reindex_source(self, source_id: UUID) -> IngestReceipt:
        """Write a source's active chunks back into the public index.

        Deactivation physically removes a source's records from the public index
        -- the public record contract forbids an ineligible record from existing
        at all -- so re-activation has to write them back.  Without this the
        operator re-enables a source, the database says "启用", and retrieval
        silently returns nothing.

        Chunking is skipped, because the stored ``KnowledgeChunk`` rows are the
        source of truth for text, location and hash.  Everything else goes
        through the same seams :meth:`ingest` uses: the version bundle is
        rebuilt from the configured chunk chain and the vectors come from the
        same embedding pipeline.  That reuse is load-bearing rather than
        cosmetic -- records rebuilt with a different version identity would be
        reported by reconciliation as a ``version_migration`` that never
        happened, and records restored without a vector would be invisible to
        the vector path while looking complete.
        """
        source = self.session.get(KnowledgeSource, source_id)
        if source is None:
            raise ValueError("知识源不存在")
        if not source.active or not source.reviewer:
            raise ValueError("知识源未启用或尚未完成医学审核")
        chunks = list(
            self.session.scalars(
                select(KnowledgeChunk)
                .where(
                    KnowledgeChunk.source_id == source.id,
                    KnowledgeChunk.active.is_(True),
                )
                .order_by(KnowledgeChunk.id)
            ).all()
        )
        bundle = self._version_bundle()
        records = [
            # ``location`` 取自存储的列，而不是在此回落到
            # ``section``：ingest 路径写入的正是这个
            # 值，而一次重新激活若产出的记录比一次全新
            # ingest 的*更*可检索，那就是一种日后无人能
            # 推理清楚的不对称。
            PublicIndexRecord(
                id=str(chunk.id),
                text=chunk.text,
                source_id=str(source.id),
                source_title=source.title,
                source_version=source.source_version,
                language=source.language,
                location=chunk.location,
                page_number=chunk.page_number,
                section=chunk.section,
                url=chunk.url,
                content_hash=chunk.content_hash,
                visibility="public",
                retrieval_eligible=True,
                source_active=True,
                deleted=False,
                parser_version=bundle.parser_version,
                cleaner_version=bundle.cleaner_version,
                chunker_version=bundle.chunker_version,
                extractor_version=bundle.extractor_version,
                locator=chunk._locator_object(),
            )
            for chunk in chunks
        ]
        if records and self.embedding_pipeline is not None:
            vectors = self.embedding_pipeline.embed_records(
                [record.text for record in records], self.embedding_model_name
            )
            records = [
                replace(
                    record,
                    embedding=vectors[index],
                    embedding_model=self.embedding_model_name,
                    embedding_dimensions=self.embedding_pipeline.dimensions,
                    index_version=self.embedding_index_version,
                )
                for index, record in enumerate(records)
            ]
        elif records and self.public_index is not None and not self.allow_unembedded:
            raise RuntimeError("embedding pipeline is required before public index upsert")
        self._index_batch(records)
        self.session.commit()
        return IngestReceipt(
            # 这里不创建也不跳过任何东西：这些块行本就
            # 存在，只是重新变得可检索而已。
            created_chunks=0,
            skipped_chunks=0,
            indexed_chunks=len(records) if self.public_index is not None else 0,
            source_id=str(source.id),
        )
