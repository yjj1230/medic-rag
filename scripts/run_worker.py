"""Operational entry point for OpenSearch initialization and the parse worker.

The project previously referenced this module from ``docker-compose.yml`` but the
file was missing from the checkout.  This entry point deliberately keeps the
composition in one place: index dimensions and embedding identity come from
``resolve_embedding`` and public/private writers are created as separate objects.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select

from medical_rag.core.config import Settings
from medical_rag.db.session import create_session_factory
from medical_rag.domain.models import KnowledgeChunk
from medical_rag.indexing.orchestrator import IndexOrchestrator
from medical_rag.indexing.reconciliation import ReconciliationService
from medical_rag.knowledge.ingest_service import (
    KnowledgeIngestService,
    KnowledgeSourceRegistry,
)
from medical_rag.models.contracts import EmbeddingPipeline
from medical_rag.models.factory import resolve_embedding
from medical_rag.parsing.worker import build_parsing_service_factory, create_celery_app
from medical_rag.retrieval.opensearch_store import OpenSearchStore
from medical_rag.retrieval.opensearch_writer import OpenSearchIndexWriter
from medical_rag.storage.object_store import S3ObjectStore


def build_opensearch_client(settings: Settings):
    from opensearchpy import OpenSearch

    return OpenSearch(settings.opensearch_url, timeout=10)


def build_index_writers(settings: Settings, client):
    embedding = resolve_embedding(settings)
    common = {
        "client": client,
        "embedding_dimension": embedding.dimensions,
        "text_analyzer": settings.text_analyzer,
        "search_analyzer": settings.search_analyzer,
    }
    public = OpenSearchIndexWriter(
        index_name=settings.public_index_name,
        alias=f"{settings.public_index_name}-alias",
        visibility="public",
        **common,
    )
    private = OpenSearchIndexWriter(
        index_name=settings.private_index_name,
        alias=f"{settings.private_index_name}-alias",
        visibility="private",
        **common,
    )
    return public, private


def init_indexes(settings: Settings, client=None) -> dict[str, str]:
    resolved_client = client if client is not None else build_opensearch_client(settings)
    public, private = build_index_writers(settings, resolved_client)
    public.ensure_mapping()
    private.ensure_mapping()
    return {"public": public.index_name, "private": private.index_name}


def build_reconciliation_service(settings: Settings, session, client=None) -> ReconciliationService:
    """Compose the DB/index reconciliation seam used by the operator command."""

    resolved_client = client if client is not None else build_opensearch_client(settings)
    public_writer, private_writer = build_index_writers(settings, resolved_client)
    observed_index = OpenSearchStore(
        resolved_client,
        public_index=settings.public_index_name,
        private_index=settings.private_index_name,
    )
    return ReconciliationService(
        session,
        public_index=observed_index,
        private_index=observed_index,
        public_writer=public_writer,
        private_writer=private_writer,
    )


def reconcile_knowledge(
    settings: Settings,
    *,
    scope: str = "public",
    limit: int = 100,
    apply: bool = False,
    service: ReconciliationService | object | None = None,
    client=None,
) -> int:
    """Scan and optionally repair DB/index lifecycle residuals.

    ``0`` means clean, ``1`` means residuals or failed repairs, and ``2``
    means the index could not be observed.  ``--apply`` performs a second scan
    so success is based on the post-repair state rather than on delete calls
    merely returning without an exception.
    """

    if scope not in {"public", "private", "all"}:
        raise ValueError("scope must be public, private, or all")
    if limit <= 0:
        raise ValueError("limit must be positive")

    session = None
    owns_service = service is None
    try:
        if service is None:
            session_factory = create_session_factory(settings)
            session = session_factory()
            service = build_reconciliation_service(settings, session, client)
        try:
            report = service.scan(scope=scope, limit=limit)
        except Exception as exc:
            print(
                f"无法观测 {scope} 索引，未给出判定: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 2

        print(f"scope={report.scope} scanned={report.scanned} residuals={report.residuals}")
        if not report.items:
            return 0
        if not apply:
            for item in report.items:
                print(f"residual id={item.item_id} reason={item.reason} action={item.action}")
            return 1

        repaired = 0
        failed = 0
        for item in report.items:
            receipt = service.repair(item)
            if receipt.success:
                repaired += 1
            else:
                failed += 1
            print(
                f"repair id={item.item_id} action={receipt.action} "
                f"outcome={receipt.outcome}"
            )
            if receipt.success and session is not None:
                session.commit()
        print(f"repaired={repaired} failed={failed}")
        if failed:
            return 1

        try:
            remaining = service.scan(scope=scope, limit=limit)
        except Exception as exc:
            print(
                f"修复后无法复核 {scope} 索引，未给出成功判定: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 2
        print(
            f"post_apply scope={remaining.scope} scanned={remaining.scanned} "
            f"residuals={remaining.residuals}"
        )
        return 0 if not remaining.items else 1
    finally:
        if owns_service and session is not None:
            session.close()


def _parse_reconcile_args(args: list[str]) -> tuple[str, int, bool]:
    scope = "public"
    limit = 100
    apply = False
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--apply":
            apply = True
            index += 1
            continue
        if value in {"--scope", "--limit"}:
            if index + 1 >= len(args):
                raise ValueError(f"{value} requires a value")
            raw = args[index + 1]
            if value == "--scope":
                scope = raw
            else:
                try:
                    limit = int(raw)
                except ValueError as exc:
                    raise ValueError("--limit must be an integer") from exc
            index += 2
            continue
        if value in {"-h", "--help"}:
            print("用法: python scripts/run_worker.py knowledge-reconcile [--scope public] [--limit 100] [--apply]")
            raise SystemExit(0)
        raise ValueError(f"未知参数: {value}")
    if scope not in {"public", "private", "all"}:
        raise ValueError("--scope must be public, private, or all")
    if limit <= 0:
        raise ValueError("--limit must be positive")
    return scope, limit, apply


def _parse_set_active_args(args: list[str]) -> tuple[UUID, bool]:
    source_id: UUID | None = None
    active: bool | None = None
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"--active", "--inactive"}:
            requested = value == "--active"
            if active is not None and active != requested:
                raise ValueError("--active 与 --inactive 不能同时出现")
            active = requested
            index += 1
            continue
        if value in {"--source-id", "--source_id"}:
            if index + 1 >= len(args):
                raise ValueError(f"{value} requires a value")
            value = args[index + 1]
            index += 2
        else:
            index += 1
        if source_id is not None:
            raise ValueError("只能指定一个 source_id")
        try:
            source_id = UUID(value)
        except ValueError as exc:
            raise ValueError("source_id 必须是合法 UUID") from exc
    if source_id is None:
        raise ValueError("必须指定 source_id")
    if active is None:
        raise ValueError("必须指定 --active 或 --inactive")
    return source_id, active


def _build_embedding_pipeline(settings: Settings):
    embedding = resolve_embedding(settings)
    if embedding.model is None:
        return None, embedding
    return (
        EmbeddingPipeline(
            embedding.model,
            dimensions=embedding.dimensions,
            batch_size=settings.embedding_batch_size,
            timeout_seconds=settings.embedding_timeout_seconds,
        ),
        embedding,
    )


def set_knowledge_source_active(
    settings: Settings,
    source_id: UUID,
    active: bool,
    *,
    session=None,
    public_writer=None,
    reindex_service=None,
    client=None,
) -> int:
    """Change a public source's lifecycle and synchronize its index.

    The database transaction is committed first.  If the external index
    operation then fails, the command returns non-zero and leaves a detectable
    residual for ``knowledge-reconcile --apply`` to converge later.
    """

    source_id = UUID(str(source_id))
    owns_session = session is None
    if session is None:
        session = create_session_factory(settings)()
    try:
        if public_writer is None:
            resolved_client = client if client is not None else build_opensearch_client(settings)
            public_writer, _private_writer = build_index_writers(settings, resolved_client)

        source = KnowledgeSourceRegistry(session).set_active(source_id, active)
        chunks = list(
            session.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.source_id == source.id)
                .order_by(KnowledgeChunk.id)
            ).all()
        )

        if not active:
            deleted = 0
            try:
                for chunk in chunks:
                    public_writer.delete_record(str(chunk.id))
                    deleted += 1
            except Exception as exc:
                print(
                    f"知识源已停用，但公共索引清理失败: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                print("请稍后运行 knowledge-reconcile --scope public --apply 收敛残留", file=sys.stderr)
                return 1
            print(f"已停用知识源 {source.id}，从公共索引删除 {deleted} 个块")
            return 0

        if reindex_service is None:
            pipeline, embedding = _build_embedding_pipeline(settings)
            reindex_service = KnowledgeIngestService(
                session,
                public_index=public_writer,
                embedding_pipeline=pipeline,
                embedding_model_name=embedding.model_name,
                embedding_index_version="public-v1",
                allow_unembedded=False,
            )
        try:
            receipt = reindex_service.reindex_source(source.id)
        except Exception as exc:
            print(
                f"知识源已启用，但公共索引恢复失败: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            print("请稍后运行 knowledge-reconcile --scope public --apply 收敛残留", file=sys.stderr)
            return 1
        indexed = getattr(receipt, "indexed_chunks", len(chunks))
        print(f"已启用知识源 {source.id}，写回公共索引 {indexed}/{len(chunks)} 个块")
        return 0
    finally:
        if owns_session:
            session.close()


def _build_runtime(settings: Settings):
    session_factory = create_session_factory(settings)
    client = build_opensearch_client(settings)
    public_writer, private_writer = build_index_writers(settings, client)
    pipeline, embedding = _build_embedding_pipeline(settings)
    if not settings.object_store_access_key or not settings.object_store_secret_key:
        raise RuntimeError("对象存储凭证未配置，无法启动解析 worker")
    object_store = S3ObjectStore(
        endpoint=settings.object_store_endpoint,
        bucket=settings.object_store_bucket,
        access_key=settings.object_store_access_key,
        secret_key=settings.object_store_secret_key.get_secret_value(),
        region=settings.object_store_region,
    )

    def orchestrator_factory() -> IndexOrchestrator:
        return IndexOrchestrator(
            session_factory(),
            public_writer=public_writer,
            private_writer=private_writer,
            embedding_pipeline=pipeline,
            allow_unembedded=pipeline is None,
        )

    service_factory: Callable[[], object] = build_parsing_service_factory(
        settings,
        session_factory,
        object_store,
        index_version=settings.embedding_index_version,
        embedding_model=embedding.model_name,
        allow_unembedded=pipeline is None,
    )
    return service_factory, orchestrator_factory


def run_worker(settings: Settings, celery_args: list[str]) -> None:
    service_factory, orchestrator_factory = _build_runtime(settings)
    app = create_celery_app(
        settings.redis_url,
        service_factory,
        orchestrator_factory,
        worker_id="parse-worker",
        index_limit=100,
    )
    app.worker_main(["worker", *celery_args])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("用法: python scripts/run_worker.py init-indexes")
        print("      python scripts/run_worker.py worker [celery worker options]")
        print("      python scripts/run_worker.py knowledge-reconcile [--scope public] [--limit 100] [--apply]")
        print("      python scripts/run_worker.py knowledge-set-active <source_id> (--active|--inactive)")
        return 0 if args else 2

    settings = Settings.from_env()
    command, rest = args[0], args[1:]
    if command == "init-indexes":
        result = init_indexes(settings)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if command == "worker":
        run_worker(settings, rest)
        return 0
    if command == "knowledge-reconcile":
        try:
            scope, limit, apply = _parse_reconcile_args(rest)
        except SystemExit as exc:
            return int(exc.code or 0)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return reconcile_knowledge(settings, scope=scope, limit=limit, apply=apply)
    if command == "knowledge-set-active":
        try:
            source_id, active = _parse_set_active_args(rest)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return set_knowledge_source_active(settings, source_id, active)
    print(f"未知命令: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
