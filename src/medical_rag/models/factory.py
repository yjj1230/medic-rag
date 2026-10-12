"""向量化后端解析：把「用哪个后端、模型是谁、多少维」收敛到唯一一处。

为什么需要这一层
----------------
向量化身份有三个必须始终一致的量：

* **后端**（模型网关 / 本地 ONNX / 不启用）——决定向量从哪里来；
* **模型身份**（``text-embedding-3-small`` / ``BAAI/bge-small-zh-v1.5``）——会被
  写进每条索引记录的 ``embedding_model``，对账与溯源都读它；
* **向量维度**——会被写进 OpenSearch 的 ``knn_vector.dimension``，**索引创建后
  不可修改**。

这三者原先分散在 ``api/composition.py``、``api/dependencies.py`` 与
``scripts/run_worker.py`` 三处各自读取 ``settings``：切后端时只要漏改一处，
就会出现「索引按 1536 建、模型产出 512」或「向量由本地模型产出、记录却写着
网关模型名」这类**静默**错误。静默错误正是本项目最不愿接受的失败模式。

``resolve_embedding`` 因此是这三个量的唯一来源，调用方只消费它的结果，不再各自
拼装。维度不一致在这里就报错，而不是等到写入索引时抛出一句无法定位的
``dimension mismatch``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from medical_rag.core.config import Settings
from medical_rag.models.cached_embedding import CachedEmbeddingModel
from medical_rag.models.contracts import EmbeddingModel, ProviderConfigurationError
from medical_rag.models.http_adapters import OpenAIEmbeddingAdapter
from medical_rag.models.local_embedding import (
    DEFAULT_LOCAL_MODEL_NAME,
    LocalFastEmbedModel,
)

logger = logging.getLogger(__name__)

#: 受支持的向量化后端。
EMBEDDING_BACKENDS = ("gateway", "local", "none")


@dataclass(frozen=True, slots=True)
class EmbeddingComposition:
    """一次解析得到的向量化组装结果（后端 + 模型 + 身份 + 维度）。

    ``model`` 为 ``None`` 表示**没有可用的向量化后端**：调用方应据此走
    「只写 BM25、不写向量」的降级路径，而不是把 ``None`` 当成错误。这是本地
    试运行（网关凭据未就绪）的既有语义，此处原样保留。
    """

    backend: str
    model: EmbeddingModel | None
    model_name: str
    dimensions: int

    @property
    def enabled(self) -> bool:
        return self.model is not None


def resolve_embedding(settings: Settings) -> EmbeddingComposition:
    """按 ``EMBEDDING_BACKEND`` 解析出向量化组装结果。

    ``gateway``
        模型网关的 ``/embeddings``（``OpenAIEmbeddingAdapter``）。缺少 API key
        时返回 ``model=None``，保持「本地无凭据仍可只跑 BM25」的既有行为。
    ``local``
        进程内 ONNX 模型（``LocalFastEmbedModel``）。维度以模型目录的
        ``config.json`` 为准，并与 ``EMBEDDING_DIMENSIONS`` 交叉校验。
    ``none``
        显式关闭向量化（等价于只跑 BM25，但由配置表达而不是靠缺凭据）。
    """
    backend = (settings.embedding_backend or "").strip().lower()
    if backend not in EMBEDDING_BACKENDS:
        raise ProviderConfigurationError(
            f"未知的 EMBEDDING_BACKEND: {settings.embedding_backend!r}；"
            f"可选值为 {' / '.join(EMBEDDING_BACKENDS)}"
        )

    if backend == "none":
        logger.warning("EMBEDDING_BACKEND=none: 不产出向量，检索仅走 BM25")
        return EmbeddingComposition(
            backend=backend,
            model=None,
            model_name=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )

    if backend == "local":
        return _resolve_local(settings)

    return _resolve_gateway(settings)


def _resolve_gateway(settings: Settings) -> EmbeddingComposition:
    """解析模型网关后端；无凭据时降级为「不产出向量」。"""
    if settings.model_gateway_api_key is None:
        # 与改动前的行为一致：本地试运行没有网关凭据时，索引仍可写（BM25 可检索），
        # 但要说清楚为什么没有向量，避免又被误诊为「知识库是空的」。
        logger.warning(
            "EMBEDDING_BACKEND=gateway 但未配置 MODEL_GATEWAY_API_KEY："
            "本次不产出向量，检索仅走 BM25。若需本地向量请设 EMBEDDING_BACKEND=local"
        )
        return EmbeddingComposition(
            backend="gateway",
            model=None,
            model_name=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    adapter = OpenAIEmbeddingAdapter(
        base_url=settings.model_gateway_url,
        api_key=settings.model_gateway_api_key,
        model=settings.embedding_model,
        timeout_seconds=settings.embedding_timeout_seconds,
    )
    return EmbeddingComposition(
        backend="gateway",
        model=CachedEmbeddingModel(adapter),
        model_name=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )


def _resolve_local(settings: Settings) -> EmbeddingComposition:
    """解析本地 ONNX 后端，并把「配置维度」与「模型真实维度」对齐校验。"""
    model = LocalFastEmbedModel(
        settings.local_embedding_model_path or "",
        model_name=settings.local_embedding_model_name or DEFAULT_LOCAL_MODEL_NAME,
        threads=settings.local_embedding_threads,
    )
    if model.dimensions != settings.embedding_dimensions:
        # 必须报错而不是「以模型为准」：``EMBEDDING_DIMENSIONS`` 同时决定
        # OpenSearch 的 knn_vector.dimension，而该值在索引创建后不可修改。
        # 悄悄采用模型的维度会让操作员以为自己设的值生效了，直到写入时才以
        # 一句 dimension mismatch 失败；静默改写显式配置同样不可接受。
        raise ProviderConfigurationError(
            f"向量维度不一致：EMBEDDING_DIMENSIONS={settings.embedding_dimensions}，"
            f"而本地模型 {model.model_version} 实际输出 {model.dimensions} 维"
            f"（{model.model_path}）。请把 EMBEDDING_DIMENSIONS 改为 "
            f"{model.dimensions}；若索引已按其他维度创建，还需重建索引。"
        )
    return EmbeddingComposition(
        backend="local",
        model=CachedEmbeddingModel(model),
        # 用模型自身的身份，而不是 ``EMBEDDING_MODEL``：后者是网关模型的配置项，
        # 切到本地后端时不应被复用，否则索引记录会声称向量来自一个没跑过的模型。
        model_name=model.model_version,
        dimensions=model.dimensions,
    )
