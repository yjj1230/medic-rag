"""应用组装根：把配置、DB、检索、模型网关组装成聊天编排器。

create_app 只负责 HTTP 层；外部资源的装配集中在该模块，由 lifespan 在启动时
调用。任何外部依赖缺失（如 opensearch-py 未安装）都会以可运行的降级形式呈现，
不阻断应用启动。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from medical_rag.auth.policy import AccessContext
from medical_rag.chat.orchestrator import ChatOrchestrator
from medical_rag.consent.service import ConsentService
from medical_rag.core.config import Settings
from medical_rag.domain.enums import ConsentPurpose
from medical_rag.domain.models import Consent
from medical_rag.models.gateway import ModelGateway
from medical_rag.models.factory import resolve_embedding
from medical_rag.models.http_adapters import (
    OpenAIChatCompletionsAdapter,
    OpenAIRerankerAdapter,
)
from medical_rag.observability.metrics import CalibrationArtifact
from medical_rag.query.context_completion import ContextCompletionStep
from medical_rag.query.intent import IntentRouter
from medical_rag.query.llm_intent import LLMIntentClassifier
from medical_rag.query.llm_rewrite import LLMRewriteStep
from medical_rag.query.normalize import NormalizeStep
from medical_rag.query.pipeline import QueryPipeline
from medical_rag.retrieval.contracts import InMemorySearchIndex, SearchIndex
from medical_rag.retrieval.service import RetrievalService

logger = logging.getLogger(__name__)


class _ScopedConsentService:
    """按调用打开会话的 ConsentService 代理。

    RetrievalService 是长生命周期单例，不能持有某个请求级 Session；该代理在
    每次调用时从 factory 获取短会话，语义与 ConsentService 一致。
    """

    def __init__(self, session_factory: Callable[[], Session], policy_version: str) -> None:
        self._session_factory = session_factory
        self._policy_version = policy_version

    def require_active_consent(self, context: AccessContext, purpose: ConsentPurpose) -> Consent:
        with self._session_factory() as session:
            return ConsentService(session, policy_version=self._policy_version).require_active_consent(
                context, purpose
            )


def build_search_index(settings: Settings) -> SearchIndex:
    """构建检索索引，构建不了就快速失败。

    降级为空的内存索引只是开发期的便利：在非开发环境，它会把"知识库什么都不返回"
    变成一次静默的可用性故障，而运维会把它误判为数据问题。
    """
    try:
        from opensearchpy import OpenSearch  # type: ignore[import-not-found]
    except ImportError:
        if not settings.is_dev_environment:
            raise RuntimeError(
                "opensearch-py 未安装，无法构建检索索引：非开发环境拒绝以降级模式启动"
            ) from None
        logger.warning("opensearch-py 未安装，检索使用空内存索引（仅适用于本地开发）")
        return InMemorySearchIndex()
    from medical_rag.retrieval.opensearch_store import OpenSearchStore

    return OpenSearchStore(
        OpenSearch(settings.opensearch_url, timeout=10),
        public_index=settings.public_index_name,
        private_index=settings.private_index_name,
    )


# 为使用旧私有名的调用方和测试保留。
_build_search_index = build_search_index


def _build_embedding_model(settings: Settings):
    """构造向量化模型（后端、身份、维度由 ``resolve_embedding`` 统一解析）。

    这里不再直接构造 ``OpenAIEmbeddingAdapter``：后端选择、模型身份与维度的
    一致性校验都收敛在 ``models.factory``，避免组装根各自拼装导致
    「索引按 A 维度建、模型产出 B 维度」这类静默漂移。
    """
    return resolve_embedding(settings).model


def _build_reranker(settings: Settings):
    """构建交叉编码器重排器；未配置时返回 ``None``。"""
    if not settings.rerank_model or settings.model_gateway_api_key is None:
        return None
    return OpenAIRerankerAdapter(
        base_url=settings.model_gateway_url,
        api_key=settings.model_gateway_api_key.get_secret_value(),
        model=settings.rerank_model,
    )


def load_calibration_artifact(settings: Settings) -> CalibrationArtifact | None:
    """加载启用严格门禁的校准工件。

    ``None``（未配置路径）意味着检索门禁只是朴素的 ``top_k`` 截断：本项目没有标注
    评测集，凭空发明一个融合分数阈值是在调参一个魔法数字，而不是在标定它。

    已*配置*却读不出或解析不了的工件会直接抛错而非回退，因为把严格门禁的部署静默
    降级为无门禁截断并不是一种安全的失败方式。
    """
    path = settings.calibration_artifact_path
    if not path:
        return None
    artifact_path = Path(path)
    if artifact_path.is_dir():
        raise ValueError(f"calibration artifact path is a directory, not a file: {artifact_path}")
    if not artifact_path.is_file():
        raise FileNotFoundError(f"calibration artifact not found: {artifact_path}")
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"calibration artifact is not valid JSON: {artifact_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"calibration artifact must be a JSON object: {artifact_path}")
    try:
        return CalibrationArtifact(**payload)
    except TypeError as exc:
        raise ValueError(
            f"calibration artifact is incomplete or has unknown fields: {artifact_path}: {exc}"
        ) from exc
    except ValueError as exc:
        raise ValueError(f"calibration artifact is invalid: {artifact_path}: {exc}") from exc


def build_chat_orchestrator(
    settings: Settings,
    session_factory: Callable[[], Session],
    search_index: SearchIndex | None = None,
) -> ChatOrchestrator:
    embedding = resolve_embedding(settings)
    embedding_model = embedding.model
    # 调用方（应用 lifespan）可以自行构建索引，这样可以
    # 观察它是否降级；把它传进来可以避免重复构建。
    resolved_index = search_index if search_index is not None else build_search_index(settings)
    calibration_artifact = load_calibration_artifact(settings)
    retriever = RetrievalService(
        resolved_index,
        consent_service=_ScopedConsentService(session_factory, settings.consent_policy_version),
        embedding_model=embedding_model,
        # 身份与维度取自解析结果而非 ``settings``：本地后端下模型身份是
        # ``BAAI/bge-small-zh-v1.5``、维度是 512，用 ``settings.embedding_model``
        # （网关模型名）会让检索侧按错误的身份做维度校验与标定匹配。
        embedding_model_name=embedding.model_name,
        embedding_dimensions=embedding.dimensions,
        # 缺少这些，重排阶段在生产中根本不会运行，
        # 每个上限都会静默回落到构造函数默认值。
        reranker=_build_reranker(settings),
        candidate_limit=settings.retrieval_candidate_limit,
        rerank_limit=settings.retrieval_rerank_limit,
        calibration_artifact=calibration_artifact,
    )
    chat_model = OpenAIChatCompletionsAdapter(
        base_url=settings.model_gateway_url,
        api_key=settings.model_gateway_api_key or "",
        model=settings.model_name,
        provider_id=settings.model_provider_id,
    )
    gateway = ModelGateway(
        session_factory,
        chat_model,
        provider_id=settings.model_provider_id,
        allowed_models={settings.model_name},
        policy_version=settings.consent_policy_version,
        data_region=settings.model_data_region,
        zero_retention=settings.model_zero_retention,
    )
    steps = [
        NormalizeStep(),
        IntentRouter(
            llm_classifier=LLMIntentClassifier(gateway, model=settings.model_name)
            if settings.intent_llm_classify_enabled
            else None,
            enable_llm=settings.intent_llm_classify_enabled,
        ),
        ContextCompletionStep(history_size=settings.query_pipeline_history_size),
    ]
    if settings.query_llm_rewrite_enabled:
        steps.append(LLMRewriteStep(gateway, enabled=True, model=settings.model_name))
    pipeline = QueryPipeline(steps, history_size=settings.query_pipeline_history_size)
    return ChatOrchestrator(
        retriever=retriever,
        model_gateway=gateway,
        session_factory=session_factory,
        policy_version=settings.consent_policy_version,
        query_pipeline=pipeline,
        history_size=settings.query_pipeline_history_size,
        top_k=settings.retrieval_top_k,
        model_name=settings.model_name,
        # 把同一个检索索引接为父/子兄弟块查找的接缝，这样
        # Task-9 的上下文扩展在生产中才真正运行（此前
        # 一直停在 None 默认值上被静默禁用）。
        expansion_index=resolved_index,
        context_token_budget=settings.context_token_budget,
    )
