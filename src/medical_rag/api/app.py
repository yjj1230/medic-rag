"""HTTP 应用工厂。

启动阶段必须诚实回答一个问题：*本进程能否处理检索请求？* 一个以半接线状态启动、
对每个调用方都返回 503 的进程，比一个干脆拒绝启动的进程更糟——因为编排器会一直把
它留在轮转池里，而运维还在追查所谓的"数据库问题"。因此 lifespan 对每个非开发环境
一律 fail closed，就绪探针会真正探测检索索引，而不是假定它可用。
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

# 存放已构建检索索引的 ``app.state`` 属性，好让就绪探针去探测该依赖，
# 而不是信任一个启动时记录的布尔值。
SEARCH_INDEX_STATE = "search_index"


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 HTTP 应用；外部资源在 lifespan 中接线。"""
    resolved_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from medical_rag.api.composition import build_chat_orchestrator, build_search_index
        from medical_rag.db.session import create_session_factory
        from medical_rag.indexing.orchestrator import ProductionComposition

        # 在任何路径上都拒绝提供不安全的部署：这段代码位于
        # try/except 之外，因此不安全的配置一定会在启动时中止。
        resolved_settings.validate_runtime()
        for warning in resolved_settings.plaintext_phi_egress_warnings():
            logger.warning("%s", warning)

        try:
            # 每进程一个引擎：请求依赖从这个工厂读取，
            # 而不是每个请求都新建（并丢弃）一个连接池。
            # 见 ``db.session``。
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
                # 索引与对账属于 worker 进程：
                # parse -> index 链路由 ``scripts/run_worker.py`` 接线，
                # 没有任何 API 路由消费这个组合
                # （``validate_production_composition`` 没有调用方）。在这里强制要求它
                # 曾让每个生产部署都拒绝启动 ——
                # 包括本项目自己的 ``medical-rag-api`` 入口 ——
                # 却什么也没守住。注入的组合图仍会在上方被校验，
                # 且 api.dependencies 仍会按请求 fail closed。
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
            # 在非开发环境吞掉这个异常，会让进程启动后每个请求都失败。
            # ``is_production`` 与 ``validate_runtime`` 使用的环境判定
            # 恰好互补，因此 ``staging`` / ``uat`` /
            # 拼错的环境名再也不能走
            # 宽松分支。
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
        """仅存活探针：进程还活着。不涉及任何依赖。"""
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness(request: Request) -> JSONResponse:
        """就绪探针：应用确实能提供检索服务。

        ``/health`` 返回 ``ok`` 而检索索引却已静默退化为空的内存索引，这在评审时
        是有误导性的；而一个只检查工厂*是否存在*的就绪端点，也只会在每次查询都
        失败时依旧报告 "ready"。因此这里会探测两个依赖：

        * 数据库，因为每个需要鉴权的路由都要用到它；
        * 检索索引，因为索引不可达的 RAG 服务回答不了任何问题 —— 把它报告成
          "引导正常"会把流量导向一个只能返回降级答案的实例。
        """
        problems: list[str] = []
        session_factory = getattr(request.app.state, SESSION_FACTORY_STATE, None)
        if session_factory is None:
            problems.append("数据库会话工厂未初始化")
        else:
            try:
                with session_factory() as session:
                    session.execute(text("SELECT 1"))
            except Exception as exc:  # noqa: BLE001 - 任何失败都意味着"未就绪"
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
    """检索索引的可达性，用部署环境的语言表达。

    降级（内存）索引在非开发环境是致命的，但在本地是预期行为，因此两种情况
    会给出不同的报告，免得本地的情况去惊动别人。真实集群会被真探测：向客户端
    询问其版本号，这是一次廉价的往返调用，当集群宕机、不可达或配置错误时会失败。
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
    except Exception as exc:  # noqa: BLE001 - 任何失败都意味着"未就绪"
        problems.append(f"检索索引不可用: {type(exc).__name__}")
    return problems
