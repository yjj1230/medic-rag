from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices
from pydantic import Field
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 容忍开发便利（不安全的身份请求头、未设置的 JWT 密钥）的环境。
# 除此之外一律视为生产环境，
# 必须显式配置。
_DEV_ENVIRONMENTS = frozenset({"local", "dev", "test"})

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

# 承载患者数据的依赖。非开发部署不得通过明文 HTTP
# 向非本机地址发送或存储 PHI，因此这些是
# 致命错误而非仅作提示：
#   * OCR 接收整页扫描报告（图像与抽取文本）；
#   * OpenSearch 逐字保存患者证据语料；
#   * 对象存储保存原始上传的报告。
_PHI_EGRESS_SETTINGS = (
    ("OCR_SERVICE_URL", "ocr_service_url"),
    ("OPENSEARCH_URL", "opensearch_url"),
    ("OBJECT_STORE_ENDPOINT", "object_store_endpoint"),
)


class Settings(BaseSettings):
    """从环境变量加载的运行时配置。"""

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
    # 远端 PaddleOCR-VL 后端（URL 留空 = 关闭远端 OCR）。``password``
    # 已按 Basic 认证预处理（httpx.BasicAuth），尽管该服务当前
    # 还没有鉴权，这样日后接入凭据无需改代码。
    ocr_service_url: str = ""
    ocr_username: str | None = None
    ocr_password: SecretStr | None = None
    ocr_timeout_seconds: float = Field(default=90.0, gt=0)

    # --- 检索 ---------------------------------------------------------------
    retrieval_top_k: int = Field(default=8, gt=0)
    # 留空时，RetrievalService 会回落到内部的魔法数字
    # （candidate_limit 或 max(top_k*5, 50)；rerank_limit 或 len(fused)），
    # 运维既看不到也改不了。请在部署中显式设置它们。
    retrieval_candidate_limit: int | None = Field(default=None, gt=0)
    retrieval_rerank_limit: int | None = Field(default=None, gt=0)
    # 除非在此指定模型，否则交叉编码器重排处于关闭状态。此前它
    # 根本没有接线，所以重排阶段静默地什么也没做。
    rerank_model: str | None = None
    # 只有提供了工件时才启用严格校准门禁。
    # 没有工件时融合分数门禁会退化（单条检索路径在 RRF 后
    # 永远无法超过 0.5），门禁就退化为朴素的 top_k 截断。
    calibration_artifact_path: str | None = None
    # 父/子上下文扩展的 token 预算；未设置则回落到
    # 扩展模块的内部默认值。
    context_token_budget: int | None = Field(default=None, gt=0)
    # OpenSearch 的物理索引名。用 RAG_* 别名绑定到 docker-compose/.env，
    # 允许新数据库使用全新的索引命名空间，避免误读旧 SQLite 对应的残留索引。
    public_index_name: str = Field(
        default="medical-rag-public",
        validation_alias=AliasChoices("RAG_PUBLIC_INDEX_NAME", "public_index_name"),
    )
    private_index_name: str = Field(
        default="medical-rag-private",
        validation_alias=AliasChoices("RAG_PRIVATE_INDEX_NAME", "private_index_name"),
    )

    # --- 鉴权 ---------------------------------------------------------------
    # 专用的签名密钥。此前数据库 URL 被复用为
    # HS256 密钥，于是任何泄漏（或默认）的 DSN 都可用于伪造令牌。
    jwt_secret: SecretStr | None = None
    jwt_issuer: str = "medical-rag"
    jwt_audience: str = "medical-rag-api"
    access_token_ttl_minutes: int = Field(default=60, gt=0)
    # 信任 X-Tenant-ID / X-Patient-ID / X-User-ID 等于
    # 完全绕过鉴权。它默认关闭，且在非开发环境被拒绝。
    allow_insecure_identity_headers: bool = False

    # --- 服务 ---------------------------------------------------------------
    # 默认仅回环：开发机不得把处理患者数据的 API
    # 暴露给周边网络。容器显式设置
    # API_HOST=0.0.0.0（见 Dockerfile）。
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, gt=0, lt=65536)
    api_log_level: str = "info"

    @property
    def is_dev_environment(self) -> bool:
        return self.environment.strip().lower() in _DEV_ENVIRONMENTS

    @property
    def is_production(self) -> bool:
        """当不得再容忍开发便利时为 True。

        定义为 :attr:`is_dev_environment` 的精确补集，这样调用方
        永远不会与 :meth:`validate_runtime` 产生分歧。此处曾匹配字面量
        ``{"prod", "production"}`` 集合，导致 ``staging``、``uat`` 或
        拼错的 ``Prodution`` 走了宽松分支，而
        ``validate_runtime`` 却把它们当作生产环境。
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
        """拒绝启动不安全的非开发部署。

        抛出 ``ValueError`` 并列出全部问题，让运维一次修完，
        而不是每重启一次才发现一个。
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
        # PHI 不得在任何路径上明文离开主机，不只是
        # 模型网关：OCR、证据索引和对象存储都承载
        # 患者数据。仅告警式的处理会让部署在报告
        # 未加密穿越网络的情况下照样上线。
        for name, url in self.phi_egress_targets():
            problems.append(
                f"{name} 使用明文 HTTP 且指向非本机地址（{url}）："
                "患者报告影像/正文会以明文经过网络，请改用 HTTPS"
            )
        if problems:
            raise ValueError("配置不安全，拒绝启动：\n- " + "\n- ".join(problems))

    def phi_egress_targets(self) -> list[tuple[str, str]]:
        """已配置的、可通过明文 HTTP 到达的承载 PHI 的依赖。"""
        return [
            (name, url)
            for name, attribute in _PHI_EGRESS_SETTINGS
            if _is_plaintext_remote(url := getattr(self, attribute))
        ]

    def plaintext_phi_egress_warnings(self) -> list[str]:
        """仅开发环境适用的、PHI 明文外发的提示信息。

        非开发部署不会带着这样的目标走到这里：
        此时 :meth:`validate_runtime` 会直接拒绝启动。
        """
        return [
            f"{name} 使用明文 HTTP 且指向非本机地址（{url}）"
            for name, url in self.phi_egress_targets()
        ]


def _is_plaintext_remote(url: str) -> bool:
    """当 ``url`` 是指向非本机地址的明文 HTTP 时为 True。"""
    if not url:
        return False
    lowered = url.strip().lower()
    if not lowered.startswith("http://"):
        return False
    authority = lowered.removeprefix("http://").split("/", 1)[0]
    host = authority.rsplit("@", 1)[-1]  # 去掉任何 userinfo
    if host.startswith("["):  # IPv6 字面量
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    return host not in _LOOPBACK_HOSTS


def get_settings() -> Settings:
    """返回进程配置的 FastAPI 依赖。

    放在这里（而不是 ``api.dependencies``），
    是为了让 ``auth`` 无需导入 API 层就能依赖它。
    """
    return Settings.from_env()

