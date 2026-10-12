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

# Re-exported: routes import ``get_session`` from here.
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
    """Return a fully configured visibility-scoped writer or fail closed."""
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
    """Return the configured public-index writer or fail closed.

    Production startup wires this object on ``app.state``; tests may explicitly
    override the dependency with a deterministic writer.
    """
    return _configured_index_writer(request, "public_index_writer", "public")


def get_private_index_writer(request: Request) -> PrivateIndexWriter:
    """Return the configured private-index writer or fail closed.

    Deletion must not report success while the private index is unreachable or
    absent.  Production startup wires this object on ``app.state``; tests may
    explicitly override the dependency with a deterministic writer.
    """

    return _configured_index_writer(request, "private_index_writer", "private")


def get_job_publisher(settings: Settings = Depends(get_settings)) -> JobPublisher:
    return CeleryJobPublisher(settings.redis_url)


def get_embedding_model(settings: Settings = Depends(get_settings)):
    """Build the configured embedding adapter (backend resolved in one place)."""
    return resolve_embedding(settings).model


def get_embedding_pipeline(
    settings: Settings = Depends(get_settings),
) -> EmbeddingPipeline | None:
    """Build the configured batching/validation embedding seam.

    ``dimensions`` comes from the *resolution* rather than ``settings`` so the
    pipeline's validation, the index mapping and the vectors actually produced
    are one number.  ``EMBEDDING_DIMENSIONS`` and the local model's real output
    size are cross-checked inside :func:`resolve_embedding`.
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
    """Return the application-composed orchestrator; production injects it at startup."""
    orchestrator = getattr(request.app.state, "chat_orchestrator", None)
    if orchestrator is None:
        raise RuntimeError("聊天编排器尚未配置")
    return orchestrator


def get_index_orchestrator(request: Request) -> IndexOrchestrator:
    """Return the startup-composed durable index orchestrator.

    No implicit in-memory or private-only fallback is allowed here.  Tests can
    override this dependency with an explicit deterministic orchestrator.
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
