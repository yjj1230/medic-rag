from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.domain.models import Base, KnowledgeChunk
from medical_rag.knowledge.contracts import (
    KnowledgeChunkDraft,
    SourceDocument,
    SourceRegistration,
)
from medical_rag.knowledge.ingest_service import (
    KnowledgeIngestService,
    KnowledgeSourceRegistry,
)


class OneChunker:
    def split(self, _source: SourceDocument) -> list[KnowledgeChunkDraft]:
        return [
            KnowledgeChunkDraft(
                text="高血压患者应定期监测血压。",
                content_hash="hash-1",
                source_version="v1",
                language="zh-CN",
                location="p1",
            )
        ]


class Embeddings:
    dimensions = 2

    def embed_records(self, texts: list[str], _model: str) -> list[tuple[float, ...]]:
        return [(float(index), 1.0) for index, _text in enumerate(texts)]


class RecordingIndex:
    def __init__(self) -> None:
        self.records: list[object] = []

    def upsert(self, records: list[object]) -> None:
        self.records.extend(records)


class FailingIndex:
    def upsert(self, _records: list[object]) -> None:
        raise RuntimeError("index unavailable")


def _make_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_ingest_does_not_write_index_when_database_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session()
    source = KnowledgeSourceRegistry(session).register(
        SourceRegistration(
            institution="WHO",
            title="高血压指南",
            source_version="v1",
            language="zh-CN",
            scope="成人健康教育",
            publication_date=date(2026, 1, 1),
            reviewer="medical-reviewer",
            active=True,
        )
    )
    session.commit()
    index = RecordingIndex()

    def fail_commit() -> None:
        raise RuntimeError("database commit failed")

    monkeypatch.setattr(session, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="database commit failed"):
        KnowledgeIngestService(
            session,
            chunker=OneChunker(),
            public_index=index,
            embedding_pipeline=Embeddings(),
        ).ingest(
            source.id,
            SourceDocument(
                title="高血压指南",
                source_version="v1",
                language="zh-CN",
                content="高血压患者应定期监测血压。",
            ),
        )

    assert index.records == []


def test_failed_index_write_leaves_chunk_inactive_for_retry() -> None:
    session = _make_session()
    source = KnowledgeSourceRegistry(session).register(
        SourceRegistration(
            institution="WHO",
            title="高血压指南",
            source_version="v1",
            language="zh-CN",
            scope="成人健康教育",
            publication_date=date(2026, 1, 1),
            reviewer="medical-reviewer",
            active=True,
        )
    )
    session.commit()
    content = SourceDocument(
        title="高血压指南",
        source_version="v1",
        language="zh-CN",
        content="高血压患者应定期监测血压。",
    )

    with pytest.raises(RuntimeError, match="index unavailable"):
        KnowledgeIngestService(
            session,
            chunker=OneChunker(),
            public_index=FailingIndex(),
            embedding_pipeline=Embeddings(),
        ).ingest(source.id, content)

    session.rollback()
    chunk = session.scalar(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id))
    assert chunk is not None
    assert chunk.active is False

    index = RecordingIndex()
    receipt = KnowledgeIngestService(
        session,
        chunker=OneChunker(),
        public_index=index,
        embedding_pipeline=Embeddings(),
    ).ingest(source.id, content)

    assert receipt.indexed_chunks == 1
    assert len(index.records) == 1
    session.refresh(chunk)
    assert chunk.active is True
