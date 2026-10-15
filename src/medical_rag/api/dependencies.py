from fastapi import Depends, Request
from sqlalchemy.orm import Session

from medical_rag.auth.policy import AccessContext
from medical_rag.auth.service import current_access_context
from medical_rag.consent.service import ConsentService
from medical_rag.core.config import Settings, get_settings
from medical_rag.db.session import get_session
from medical_rag.domain.enums import ConsentPurpose
from medical_rag.domain.models import Consent
from medical_rag.ingest.upload_service import CeleryJobPublisher, JobPublisher
from medical_rag.indexing.orchestrator import IndexOrchestrator, ProductionComposition
from medical_rag.models.contracts import EmbeddingPipeline
from medical_rag.models.factory import resolve_embedding
from medical_rag.retrieval.contracts import PrivateIndexWriter
from medical_rag.storage.object_store import ObjectStore, S3ObjectStore

# 重新导出：路由从这里导入 ``get_session``。
__all__ = [
    "get_chat_orchestrator",
    "get_embedding_model",
    "get_embedding_pipeline",
    "get_index_orchestrator",
    "get_job_publisher",
    "get_object_store",
    "get_private_index_writer",
    "get_public_index_writer",
    "get_session",
    "require_external_model_consent",
    "validate_production_composition",
]


def require_external_model_consent(
    context: AccessContext = Depends(current_access_context),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Consent:
    return ConsentService(
        session,
        policy_version=settings.consent_policy_version,
    ).require_active_consent(context, ConsentPurpose.EXTERNAL_MODEL)


def get_object_store(settings: Settings = Depends(get_settings)) -> ObjectStore:
    if not settings.object_store_access_key or not settings.object_store_secret_key:
        raise RuntimeError("对象存储凭证未配置")
    return S3ObjectStore(
        endpoint=settings.object_store_endpoint,
        bucket=settings.object_store_bucket,
        access_key=settings.object_store_access_key,
        secret_key=settings.object_store_secret_key.get_secret_value(),
        region=settings.object_store_region,
    )


def _configured_index_writer(request: Request, state_name: str, visibility: str):
    """返回一个完整配置、按可见性限定作用域的写入器，否则 fail closed。"""
    writer = getattr(request.app.state, state_name, None)
    required = ("upsert", "ensure_mapping")
    if visibility == "private":
        required += ("delete_document",)
    if writer is None or any(not callable(getattr(writer, name, None)) for name in required):
        raise RuntimeError(f"{visibility} index writer is not configured")
    writer_visibility = getattr(writer, "visibility", None)
    if writer_visibility is None:
        writer_visibility = getattr(writer, "scope", None)
    if writer_visibility != visibility:
        raise RuntimeError(f"{visibility} index writer has an unsafe visibility")
    return writer


def get_public_index_writer(request: Request):
    """返回已配置的公开索引写入器，否则 fail closed。

    生产启动会把该对象接到 ``app.state`` 上；测试可以用确定性的写入器显式
    覆盖此依赖。
    """
    return _configured_index_writer(request, "public_index_writer", "public")


def get_private_index_writer(request: Request) -> PrivateIndexWriter:
    """返回已配置的私有索引写入器，否则 fail closed。

    在私有索引不可达或缺失时，删除操作不得报告成功。生产启动会把该对象接到
    ``app.state`` 上；测试可以用确定性的写入器显式覆盖此依赖。
    """

    return _configured_index_writer(request, "private_index_writer", "private")


def get_job_publisher(settings: Settings = Depends(get_settings)) -> JobPublisher:
    return CeleryJobPublisher(settings.redis_url)


def get_embedding_model(settings: Settings = Depends(get_settings)):
    """构建已配置的向量化适配器（后端只在一处解析）。"""
    return resolve_embedding(settings).model


def get_embedding_pipeline(
    settings: Settings = Depends(get_settings),
) -> EmbeddingPipeline | None:
    """构建已配置的批处理/校验向量化接缝。

    ``dimensions`` 来自*解析结果*而非 ``settings``，这样管线校验、索引映射和实际
    产出的向量用的是同一个数字。``EMBEDDING_DIMENSIONS`` 与本地模型的真实输出
    维度会在 :func:`resolve_embedding` 内部交叉校验。
    """
    embedding = resolve_embedding(settings)
    if embedding.model is None:
        return None
    return EmbeddingPipeline(
        embedding.model,
        dimensions=embedding.dimensions,
        batch_size=settings.embedding_batch_size,
        timeout_seconds=settings.embedding_timeout_seconds,
    )


def get_chat_orchestrator(request: Request):
    """返回应用组合的编排器；生产在启动时注入它。"""
    orchestrator = getattr(request.app.state, "chat_orchestrator", None)
    if orchestrator is None:
        raise RuntimeError("聊天编排器尚未配置")
    return orchestrator


def get_index_orchestrator(request: Request) -> IndexOrchestrator:
    """返回启动时组合的持久化索引编排器。

    这里不允许任何隐式的内存兜底或仅私有兜底。测试可以用显式、确定性的
    编排器覆盖此依赖。
    """
    orchestrator = getattr(request.app.state, "index_orchestrator", None)
    if not isinstance(orchestrator, IndexOrchestrator):
        raise RuntimeError("索引编排器尚未配置")
    return orchestrator


def validate_production_composition(request: Request) -> ProductionComposition:
    composition = getattr(request.app.state, "production_composition", None)
    if not isinstance(composition, ProductionComposition):
        raise RuntimeError("生产索引依赖未配置")
    composition.validate()
    return composition
