from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from medical_rag.core.config import Settings
from medical_rag.domain.models import Base, KnowledgeChunk
from medical_rag.indexing.reconciliation import (
    ReconciliationItem,
    ReconciliationReport,
    RepairReceipt,
)
from medical_rag.knowledge.contracts import SourceRegistration
from medical_rag.knowledge.ingest_service import KnowledgeSourceRegistry


@dataclass
class FakeReconciliationService:
    scans: list[ReconciliationReport]
    repaired: list[str]

    def scan(self, scope: str, limit: int) -> ReconciliationReport:
        assert scope == "public"
        assert limit == 10
        return self.scans.pop(0)

    def repair(self, item: ReconciliationItem) -> RepairReceipt:
        self.repaired.append(item.item_id)
        return RepairReceipt(
            item_id=item.item_id,
            scope=item.scope,
            action=item.action,
            success=True,
            outcome="repaired",
        )


def _residual_report() -> ReconciliationReport:
    item = ReconciliationItem(
        item_id="chunk-1",
        scope="public",
        reason="inactive",
        action="delete",
        record_id="chunk-1",
    )
    return ReconciliationReport(scope="public", scanned=1, items=(item,))


def test_reconcile_knowledge_apply_repairs_and_rechecks(capsys) -> None:
    from scripts.run_worker import reconcile_knowledge

    service = FakeReconciliationService(
        scans=[
            _residual_report(),
            ReconciliationReport(scope="public", scanned=0, items=()),
        ],
        repaired=[],
    )
    settings = Settings(_env_file=None)

    exit_code = reconcile_knowledge(
        settings,
        scope="public",
        limit=10,
        apply=True,
        service=service,
    )

    assert exit_code == 0
    assert service.repaired == ["chunk-1"]
    output = capsys.readouterr().out
    assert "scanned=1 residuals=1" in output
    assert "repaired=1" in output
    assert "scanned=0 residuals=0" in output


def test_reconcile_knowledge_dry_run_does_not_repair(capsys) -> None:
    from scripts.run_worker import reconcile_knowledge

    service = FakeReconciliationService(scans=[_residual_report()], repaired=[])
    settings = Settings(_env_file=None)

    exit_code = reconcile_knowledge(
        settings,
        scope="public",
        limit=10,
        apply=False,
        service=service,
    )

    assert exit_code == 1
    assert service.repaired == []
    assert "scanned=1 residuals=1" in capsys.readouterr().out


def test_reconcile_knowledge_returns_two_when_observation_fails(capsys) -> None:
    from scripts.run_worker import reconcile_knowledge

    class Unavailable:
        def scan(self, **_kwargs):
            raise ConnectionError("OpenSearch unavailable")

    exit_code = reconcile_knowledge(
        Settings(_env_file=None),
        scope="public",
        limit=10,
        service=Unavailable(),
    )

    assert exit_code == 2
    assert "无法观测 public 索引" in capsys.readouterr().err


def test_set_knowledge_source_active_inactive_commits_db_then_deletes_chunks() -> None:
    from scripts.run_worker import set_knowledge_source_active

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    source = KnowledgeSourceRegistry(session).register(
        SourceRegistration(
            institution="WHO",
            title="高血压指南",
            source_version="v1",
            language="zh-CN",
            scope="成人健康教育",
            publication_date=date(2026, 1, 1),
            reviewer="reviewer",
            active=True,
        )
    )
    session.add_all(
        [
            KnowledgeChunk(
                source_id=source.id,
                content_hash=f"hash-{index}",
                text=f"医学内容 {index}",
                active=True,
            )
            for index in range(2)
        ]
    )
    session.commit()

    class Writer:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_record(self, record_id: str) -> None:
            self.deleted.append(str(record_id))

    writer = Writer()
    exit_code = set_knowledge_source_active(
        Settings(_env_file=None),
        source.id,
        False,
        session=session,
        public_writer=writer,
    )

    assert exit_code == 0
    session.refresh(source)
    assert source.active is False
    chunks = session.scalars(select(KnowledgeChunk)).all()
    assert all(chunk.active is False for chunk in chunks)
    assert set(writer.deleted) == {str(chunk.id) for chunk in chunks}


def test_set_knowledge_source_active_active_calls_reindex_after_db_commit() -> None:
    from scripts.run_worker import set_knowledge_source_active

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    source = KnowledgeSourceRegistry(session).register(
        SourceRegistration(
            institution="WHO",
            title="高血压指南",
            source_version="v1",
            language="zh-CN",
            scope="成人健康教育",
            publication_date=date(2026, 1, 1),
            reviewer="reviewer",
            active=False,
        )
    )
    session.commit()

    class Reindex:
        def __init__(self) -> None:
            self.source_id: UUID | None = None

        def reindex_source(self, source_id: UUID):
            self.source_id = source_id

    reindex = Reindex()
    exit_code = set_knowledge_source_active(
        Settings(_env_file=None),
        source.id,
        True,
        session=session,
        public_writer=object(),
        reindex_service=reindex,
    )

    assert exit_code == 0
    session.refresh(source)
    assert source.active is True
    assert reindex.source_id == source.id
