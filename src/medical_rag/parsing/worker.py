from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from medical_rag.core.config import Settings
from medical_rag.parsing.adapters import ParserAdapters, build_parser_adapters
from medical_rag.parsing.service import ParsingService
from medical_rag.storage.object_store import ObjectStore


class ParsingWorker:
    def __init__(
        self,
        service_factory: Callable[[], ParsingService],
        orchestrator_factory: Callable[[], object] | None = None,
        *,
        worker_id: str = "parse-worker",
        index_limit: int = 100,
    ) -> None:
        self.service_factory = service_factory
        self.orchestrator_factory = orchestrator_factory
        self.worker_id = worker_id
        self.index_limit = index_limit

    def run(self, job_id: UUID):
        service = self.service_factory()
        try:
            result = service.process(job_id)
            # Parsing commits durable IndexJobs/Outbox rows.  Indexing is
            # handed off to the lease-aware orchestrator when configured;
            # explicit synchronous ParsingService writers remain supported for
            # deterministic tests and local callers.
            if self.orchestrator_factory is not None:
                orchestrator = self.orchestrator_factory()
                run_once = getattr(orchestrator, "run_once", None)
                if not callable(run_once):
                    raise RuntimeError("index orchestrator is not configured")
                run_once(self.worker_id, self.index_limit)
            return result
        finally:
            if service.session is not None:
                service.session.close()

    def recover_stale_jobs(self, timeout_minutes: int = 30) -> int:
        service = self.service_factory()
        try:
            return service.recover_stale_jobs(timeout_minutes)
        finally:
            if service.session is not None:
                service.session.close()


def create_celery_app(
    broker_url: str,
    service_factory: Callable[[], ParsingService],
    orchestrator_factory: Callable[[], object] | None = None,
    *,
    worker_id: str = "parse-worker",
    index_limit: int = 100,
):
    from celery import Celery

    app = Celery("medical_rag", broker=broker_url)

    @app.task(name="medical_rag.parse_document")
    def parse_document(job_id: str):
        return ParsingWorker(
            service_factory,
            orchestrator_factory,
            worker_id=worker_id,
            index_limit=index_limit,
        ).run(UUID(job_id)).to_payload()

    return app


def build_parsing_service_factory(
    settings: Settings,
    session_factory: Callable[[], Session],
    object_store: ObjectStore,
    **kwargs: object,
) -> Callable[[], ParsingService]:
    """Composition-root factory for :class:`ParsingWorker`'s ``service_factory``.

    Uses :func:`build_parser_adapters` so env config actually activates remote
    OCR: a non-empty ``ocr_service_url`` wires the remote :class:`RemoteOCRParser`
    (images) plus the per-PDF backend factory (scanned-PDF backfill); an empty
    URL (default) keeps the deterministic native-only adapters and the remote
    backend disabled.  Each call opens a fresh short session via
    ``session_factory``.
    """
    adapters: ParserAdapters = build_parser_adapters(settings)

    def factory() -> ParsingService:
        return ParsingService(
            session=session_factory(),
            object_store=object_store,
            adapters=adapters,
            **kwargs,
        )

    return factory
