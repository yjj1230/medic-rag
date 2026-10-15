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
            # 解析会提交持久化的 IndexJobs/Outbox 行。索引
            # 在配置后交接给可感知租约的编排器；
            # 显式的同步 ParsingService 写入器仍为确定性的
            # 测试和本地调用方提供支持。
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
    """:class:`ParsingWorker` 的 ``service_factory`` 的组合根工厂。

    使用 :func:`build_parser_adapters`，让环境配置真正激活远端 OCR：
    非空的 ``ocr_service_url`` 会接线远端 :class:`RemoteOCRParser`（图像）
    外加逐 PDF 后端工厂（扫描 PDF 回填）；URL 为空（默认）则保持确定性的
    原生适配器，远端后端关闭。每次调用都通过 ``session_factory`` 新开一个短会话。
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
