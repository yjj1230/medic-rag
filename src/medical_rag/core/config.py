from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices
from pydantic import Field
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Environments in which developer conveniences (insecure identity headers, an
# unset JWT secret) are tolerated.  Anything else is treated as production and
# must be configured explicitly.
_DEV_ENVIRONMENTS = frozenset({"local", "dev", "test"})

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

# Dependencies that carry patient data.  A non-development deployment must not
# send or store PHI across plaintext HTTP to a non-loopback host, so these are
# fatal rather than advisory:
#   * OCR receives whole scanned report pages (images and extracted text);
#   * OpenSearch holds the patient evidence corpus verbatim;
#   * the object store holds the original uploaded reports.
_PHI_EGRESS_SETTINGS = (
    ("OCR_SERVICE_URL", "ocr_service_url"),
    ("OPENSEARCH_URL", "opensearch_url"),
    ("OBJECT_STORE_ENDPOINT", "object_store_endpoint"),
)


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "local"
    database_url: str = Field(
        default="postgresql+psycopg://medical_rag:medical_rag@localhost:5432/medical_rag"
    )
    opensearch_url: str = "http://localhost:9200"
    object_store_endpoint: str = "http://localhost:9000"
    object_store_bucket: str = "medical-rag"
    object_store_access_key: str | None = None
    object_store_secret_key: SecretStr | None = None
    object_store_region: str = "us-east-1"
    redis_url: str = "redis://localhost:6379/0"
    model_gateway_url: str = "http://121.40.34.96:18097/v1"
    model_provider_id: str = "lan-chat"
    model_name: str = "gpt-5.5"
    model_wire_api: Literal["chat_completions", "responses"] = "chat_completions"
    model_gateway_api_key: SecretStr | None = None
    model_data_region: str | None = None
    model_zero_retention: bool = True
    model_request_timeout_seconds: float = 30.0
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, gt=0)
    embedding_batch_size: int = Field(default=64, gt=0)
    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    embedding_index_version: str = "private-v1"
    # --- 向量化后端 ---------------------------------------------------------
    # ``gateway`` 走模型网关的 /embeddings；``local`` 走进程内 ONNX 模型；
    # ``none`` 显式关闭向量化（只跑 BM25）。默认 gateway 保持既有部署行为不变。
    #
    # 之所以需要显式开关：实测该网关只提供对话模型、不接受任何 embedding 请求，
    # 因此原先唯一的向量化路径实际不可用，而索引里没有任何向量这件事只能靠人
    # 去读索引才发现。把后端变成配置项，切回网关只需改这一个值。
    embedding_backend: Literal["gateway", "local", "none"] = "gateway"
    # 本地 ONNX 模型目录（含 config.json / tokenizer.json / model_optimized.onnx）。
    # 维度由该目录的 config.json 决定，必须与 EMBEDDING_DIMENSIONS 一致。
    local_embedding_model_path: str | None = None
    local_embedding_model_name: str = "BAAI/bge-small-zh-v1.5"
    # 留空 = 交给 onnxruntime 按 CPU 核数自行决定。
    local_embedding_threads: int | None = Field(default=None, gt=0)
    consent_policy_version: str = "v1"
    query_rewrite_enabled: bool = True
    query_pipeline_history_size: int = 4
    query_llm_rewrite_enabled: bool = False
    intent_llm_classify_enabled: bool = True
    # Remote PaddleOCR-VL backend (empty URL = remote OCR disabled).  ``password``
    # is Basic-auth-prepped (httpx.BasicAuth) even though the service currently
    # has no auth, so credentials can be wired without a code change later.
    ocr_service_url: str = ""
    ocr_username: str | None = None
    ocr_password: SecretStr | None = None
    ocr_timeout_seconds: float = Field(default=90.0, gt=0)

    # --- retrieval ----------------------------------------------------------
    retrieval_top_k: int = Field(default=8, gt=0)
    # Left unset, RetrievalService falls back to internal magic numbers
    # (candidate_limit or max(top_k*5, 50); rerank_limit or len(fused)), which no
    # operator can see or change.  Set them explicitly in a deployment.
    retrieval_candidate_limit: int | None = Field(default=None, gt=0)
    retrieval_rerank_limit: int | None = Field(default=None, gt=0)
    # Cross-encoder reranking is off unless a model is named here.  Previously it
    # was never wired at all, so the reranking stage silently did nothing.
    rerank_model: str | None = None
    # Strict calibrated gating is enabled only when an artifact is supplied.
    # Without one the fused-score gate degenerates (a single retrieval path can
    # never exceed 0.5 after RRF) and the gate is plain top_k truncation.
    calibration_artifact_path: str | None = None
    # Token budget for parent/child context expansion; unset falls back to the
    # expansion module's internal default.
    context_token_budget: int | None = Field(default=None, gt=0)

    # --- authentication -----------------------------------------------------
    # Dedicated signing secret.  Previously the database URL was reused as the
    # HS256 secret, so any leaked (or default) DSN allowed token forgery.
    jwt_secret: SecretStr | None = None
    jwt_issuer: str = "medical-rag"
    jwt_audience: str = "medical-rag-api"
    access_token_ttl_minutes: int = Field(default=60, gt=0)
    # Trusting X-Tenant-ID / X-Patient-ID / X-User-ID is a complete
    # authentication bypass.  It is off by default and refused outside dev.
    allow_insecure_identity_headers: bool = False

    # --- serving ------------------------------------------------------------
    # Loopback by default: a developer machine must not expose an API that
    # handles patient data to the surrounding network.  Containers set
    # API_HOST=0.0.0.0 explicitly (see the Dockerfile).
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, gt=0, lt=65536)
    api_log_level: str = "info"

    @property
    def is_dev_environment(self) -> bool:
        return self.environment.strip().lower() in _DEV_ENVIRONMENTS

    @property
    def is_production(self) -> bool:
        """True when developer conveniences must not be tolerated.

        Defined as the exact complement of :attr:`is_dev_environment` so callers
        can never disagree with :meth:`validate_runtime`.  Matching a literal
        ``{"prod", "production"}`` set here once meant ``staging``, ``uat`` or a
        misspelled ``Prodution`` took the permissive branch while
        ``validate_runtime`` treated them as production.
        """
        return not self.is_dev_environment

    # 索引 ``text`` 字段的分析器。默认值需要集群安装 analysis-ik 插件；
    # 没有该插件时用 ``cjk``（内置、二元切分、对中文明显优于 standard 的单字切分）
    # 或 ``none``/``default`` 显式接受 OpenSearch 默认行为。
    #
    # 这两个必须与 worker 入口的 ``RAG_TEXT_ANALYZER`` / ``RAG_SEARCH_ANALYZER``
    # 保持一致：入口读的是**进程环境变量**，而 .env 只被本 Settings 读取、不会
    # 导出成环境变量。因此这里放同样的字段并显式绑定 ``RAG_*`` 别名，让 .env 成为
    # 唯一的配置入口；入口在环境变量缺位时回落到本配置，两边不会再不一致。
    #
    # 别名是必需的：字段名 ``text_analyzer`` 默认只对应 ``TEXT_ANALYZER``，
    # 因此 ``.env`` 里的 ``RAG_TEXT_ANALYZER`` 原先会被当作未知键忽略。
    text_analyzer: str = Field(
        default="ik_max_word", validation_alias=AliasChoices("RAG_TEXT_ANALYZER", "text_analyzer")
    )
    search_analyzer: str = Field(
        default="ik_smart",
        validation_alias=AliasChoices("RAG_SEARCH_ANALYZER", "search_analyzer"),
    )

    @classmethod
    @lru_cache(maxsize=1)
    def from_env(cls) -> "Settings":
        return cls()

    def validate_runtime(self) -> None:
        """Refuse to start a non-development deployment that is unsafe.

        Raises ``ValueError`` listing every problem so an operator fixes them in
        one pass instead of discovering them one restart at a time.
        """
        if self.is_dev_environment:
            return
        problems: list[str] = []
        if self.jwt_secret is None:
            problems.append(
                "JWT_SECRET 未配置：非开发环境必须提供独立的令牌签名密钥"
                "（不得复用 DATABASE_URL）"
            )
        elif len(self.jwt_secret.get_secret_value()) < 32:
            problems.append(
                "JWT_SECRET 过短：HS256 建议至少 32 字节，"
                "否则签名可被暴力破解（RFC 7518 §3.2）"
            )
        if self.allow_insecure_identity_headers:
            problems.append(
                "ALLOW_INSECURE_IDENTITY_HEADERS 不能在非开发环境启用："
                "该开关允许调用方通过请求头伪造身份"
            )
        if _is_plaintext_remote(self.model_gateway_url):
            problems.append(
                f"MODEL_GATEWAY_URL 必须使用 HTTPS（当前 {self.model_gateway_url}）："
                "患者病历与查询文本会以明文经过公网"
            )
        # PHI must not leave the host in the clear on any path, not just the
        # model gateway: OCR, the evidence index and the object store all carry
        # patient data.  Warning-only handling let a deployment ship with
        # reports crossing the network unencrypted.
        for name, url in self.phi_egress_targets():
            problems.append(
                f"{name} 使用明文 HTTP 且指向非本机地址（{url}）："
                "患者报告影像/正文会以明文经过网络，请改用 HTTPS"
            )
        if problems:
            raise ValueError("配置不安全，拒绝启动：\n- " + "\n- ".join(problems))

    def phi_egress_targets(self) -> list[tuple[str, str]]:
        """Configured PHI-carrying dependencies reachable over plaintext HTTP."""
        return [
            (name, url)
            for name, attribute in _PHI_EGRESS_SETTINGS
            if _is_plaintext_remote(url := getattr(self, attribute))
        ]

    def plaintext_phi_egress_warnings(self) -> list[str]:
        """Development-only advisory notices for PHI leaving over plaintext.

        Non-development deployments never reach this point with such a target:
        :meth:`validate_runtime` refuses to start instead.
        """
        return [
            f"{name} 使用明文 HTTP 且指向非本机地址（{url}）"
            for name, url in self.phi_egress_targets()
        ]


def _is_plaintext_remote(url: str) -> bool:
    """True when ``url`` is plaintext HTTP to somewhere other than loopback."""
    if not url:
        return False
    lowered = url.strip().lower()
    if not lowered.startswith("http://"):
        return False
    authority = lowered.removeprefix("http://").split("/", 1)[0]
    host = authority.rsplit("@", 1)[-1]  # drop any userinfo
    if host.startswith("["):  # IPv6 literal
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    return host not in _LOOPBACK_HOSTS


def get_settings() -> Settings:
    """FastAPI dependency returning the process settings.

    Lives here (rather than in ``api.dependencies``) so that ``auth`` can depend
    on it without importing the API layer.
    """
    return Settings.from_env()

