"""HTTP application factory.

Startup has to answer one question honestly: *can this process serve a
retrieval request?*  A process that boots with a half-wired composition and
returns 503 to every caller is worse than one that refuses to start, because
orchestrators keep it in rotation while operators chase a "database problem".
The lifespan therefore fails closed for every non-development environment, and
readiness probes the retrieval index rather than assuming it.
"""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from medical_rag.api.routes.auth import router as auth_router, _login_router
from medical_rag.api.routes.documents import router as documents_router
from medical_rag.api.routes.chat import router as chat_router
from medical_rag.core.config import Settings
from medical_rag.core.errors import (
    AccessDenied,
    AuthenticationRequired,
    ConsentRequired,
    MedicalRagError,
)
from medical_rag.db.session import SESSION_FACTORY_STATE
from medical_rag.retrieval.contracts import InMemorySearchIndex

logger = logging.getLogger(__name__)

# ``app.state`` attribute holding the built search index, so readiness can probe
# the dependency instead of trusting a boolean recorded at startup.
SEARCH_INDEX_STATE = "search_index"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the HTTP application; external resources are wired in lifespan."""
    resolved_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from medical_rag.api.composition import build_chat_orchestrator, build_search_index
        from medical_rag.db.session import create_session_factory
        from medical_rag.indexing.orchestrator import ProductionComposition

        # Refuse to serve an unsafe deployment, on every path: this sits outside
        # the try/except so an unsafe configuration always aborts startup.
        resolved_settings.validate_runtime()
        for warning in resolved_settings.plaintext_phi_egress_warnings():
            logger.warning("%s", warning)

        try:
            # One engine per process: the request dependency reads this factory
            # from ``app.state`` instead of building (and discarding) a pool per
            # request.  See ``db.session``.
            session_factory = create_session_factory(resolved_settings)
            setattr(app.state, SESSION_FACTORY_STATE, session_factory)
            composition = getattr(app.state, "production_composition", None)
            if composition is not None:
                if not isinstance(composition, ProductionComposition):
                    raise RuntimeError("invalid production composition")
                composition.validate()
                if composition.index_orchestrator is not None:
                    app.state.index_orchestrator = composition.index_orchestrator
            elif resolved_settings.is_production:
                # Indexing and reconciliation belong to the worker process: the
                # parse -> index graph is wired by ``scripts/run_worker.py``, and
                # no API route consumes this composition
                # (``validate_production_composition`` has no callers).  Demanding
                # it here made every production deployment refuse to start --
                # including this project's own ``medical-rag-api`` entrypoint --
                # while guarding nothing.  An injected graph is still validated
                # above, and api.dependencies still fails closed per request.
                logger.warning(
                    "未注入生产索引组装：索引、对账与死信由 worker 进程负责"
                    "（scripts/run_worker.py）"
                )
            search_index = build_search_index(resolved_settings)
            setattr(app.state, SEARCH_INDEX_STATE, search_index)
            app.state.search_index_degraded = isinstance(search_index, InMemorySearchIndex)
            app.state.chat_orchestrator = build_chat_orchestrator(
                resolved_settings, session_factory, search_index=search_index
            )
        except Exception:
            logger.exception("failed to compose chat orchestrator at startup")
            # Swallowing this outside development started a process whose every
            # request failed.  ``is_production`` is the exact complement of the
            # environment predicate ``validate_runtime`` uses, so ``staging`` /
            # ``uat`` / a misspelled environment can no longer take the
            # permissive branch.
            if resolved_settings.is_production:
                raise

        # --- 门禁口径自检：必须在 try/except 之外（09-28 enablement spec §4.3）---
        # 放进 try 内会被非生产环境的 log-and-continue 分支吞掉，于是"配了
        # artifact 却退回 TOP_K"的实例照常启动——正是本自检要拦的情形。
        _assert_configured_gate_is_active(app, resolved_settings)

        yield

    app = FastAPI(title="Medical RAG", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.include_router(auth_router)
    app.include_router(_login_router)
    app.include_router(documents_router)
    app.include_router(chat_router)

    @app.exception_handler(AuthenticationRequired)
    async def authentication_required_handler(
        request: Request, exc: AuthenticationRequired
    ) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(AccessDenied)
    async def access_denied_handler(request: Request, exc: AccessDenied) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ConsentRequired)
    async def consent_required_handler(request: Request, exc: ConsentRequired) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(MedicalRagError)
    async def medical_rag_error_handler(request: Request, exc: MedicalRagError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness only: the process is up.  Says nothing about dependencies."""
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness(request: Request) -> JSONResponse:
        """Readiness: the application can actually serve retrievals.

        ``/health`` returning ``ok`` while the search index silently degraded to
        an empty in-memory index was actively misleading during review, and a
        readiness endpoint that only checks that a factory *exists* still reports
        "ready" while every query fails.  Both dependencies are therefore probed:

        * the database, because every authenticated route needs it; and
        * the retrieval index, because a RAG service whose index is unreachable
          cannot answer anything -- reporting it as bootstrapping-ok sent traffic
          to an instance that could only return degraded answers.
        """
        problems: list[str] = []
        session_factory = getattr(request.app.state, SESSION_FACTORY_STATE, None)
        if session_factory is None:
            problems.append("数据库会话工厂未初始化")
        else:
            try:
                with session_factory() as session:
                    session.execute(text("SELECT 1"))
            except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
                problems.append(f"数据库不可用: {type(exc).__name__}")
        if getattr(request.app.state, "chat_orchestrator", None) is None:
            problems.append("聊天编排器未初始化")
        problems.extend(_probe_search_index(request))
        if problems:
            return JSONResponse(
                status_code=503, content={"status": "not_ready", "problems": problems}
            )
        return JSONResponse(status_code=200, content={"status": "ready"})

    return app


def _assert_configured_gate_is_active(app: FastAPI, settings: Settings) -> None:
    """配置了标定产物时，必须真的装配出余弦绝对门禁，否则拒绝启动。

    未配置路径时不做任何检查（``TOP_K`` 正是预期状态）——因此本函数对
    "从未启用门禁"的部署行为逐字节不变。

    条件用"路径非空"而不是"artifact 非 None"：``artifact`` 只有在
    ``RetrievalService`` 构造成功后才存在；若以 ``artifact is not None`` 为条件，
    那么"配了路径 + 装配失败 + 非生产环境"这一组合会**跳过自检**——恰好漏掉
    最该拦的情形。
    """
    if not settings.calibration_artifact_path:
        return
    from medical_rag.retrieval.contracts import GatePolicy

    orchestrator = getattr(app.state, "chat_orchestrator", None)
    retriever = getattr(orchestrator, "retriever", None)
    if retriever is None:
        # 配置了 artifact 却没走到装配（例如非生产环境里装配失败被吞掉）
        # => 不能假装启动成功。
        raise RuntimeError(
            "已配置 CALIBRATION_ARTIFACT_PATH 但检索编排器未装配："
            f"{settings.calibration_artifact_path}"
        )
    artifact = getattr(retriever, "calibration_artifact", None)
    if artifact is None:
        raise RuntimeError(
            "已配置 CALIBRATION_ARTIFACT_PATH 但检索服务未持有标定产物"
            f"：{settings.calibration_artifact_path}"
        )
    if getattr(retriever, "gate_policy", None) is not GatePolicy.COSINE_ABSOLUTE:
        raise RuntimeError(
            "calibration artifact 未选出 COSINE_ABSOLUTE 门禁；"
            f"实际={getattr(retriever, 'gate_policy', None)}"
            "（配 artifact 的检索服务不得静默退回 TOP_K 截断）"
        )
    logger.info(
        "cosine gate enabled: threshold=%s score_space=%s public=%s private=%s",
        artifact.cosine_threshold,
        artifact.score_space,
        artifact.public_index_version,
        artifact.private_index_version,
    )


def _probe_search_index(request: Request) -> list[str]:
    """Reachability of the retrieval index, in the language of the deployment.

    A degraded (in-memory) index is fatal outside development but expected
    locally, so the two cases report differently instead of the local case
    waking people up.  A live cluster is probed for real: the client is asked
    for its version, which is a single cheap round trip that fails when the
    cluster is down, unreachable or misconfigured.
    """
    problems: list[str] = []
    settings = getattr(request.app.state, "settings", None)
    degraded = bool(getattr(request.app.state, "search_index_degraded", False))
    if degraded:
        if settings is not None and settings.is_production:
            problems.append("检索索引处于降级状态（非开发环境不可接受）")
        return problems

    index = getattr(request.app.state, SEARCH_INDEX_STATE, None)
    if index is None:
        problems.append("检索索引未初始化")
        return problems
    client = getattr(index, "client", None)
    if client is None:
        return problems
    try:
        client.info()
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        problems.append(f"检索索引不可用: {type(exc).__name__}")
    return problems
