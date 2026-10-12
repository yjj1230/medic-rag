"""检索门禁回归测试的共享夹具。

这一套测试用确定性的假混合索引驱动**真实的** ``RetrievalService``，因此
不需要 OpenSearch、也不需要本地向量模型即可覆盖拒答门禁（2026-10-12 评测
把「无关查询拒答准确率」量化为 0.00 的 P0 缺陷）全部分支。

设计要点：

* 门禁作用于 ``search_lexical`` / ``search_vector`` **之后**、RRF 融合
  **之前**，所以一个返回固定候选列表的假索引就足以确定性地走遍每条分支；
* 夹具只构造记录与上下文，不做任何断言——断言留在测试文件里，保证
  「一个测试只讲一件事」。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from medical_rag.auth.policy import AccessContext
from medical_rag.knowledge.contracts import Citation
from medical_rag.observability.metrics import CalibrationArtifact
from medical_rag.retrieval.contracts import SearchRecord, SearchQuery, Visibility

# 与 .env 的 LOCAL_EMBEDDING_MODEL_NAME / 部署校准件
# config/calibration/cosine-gate-2026-09-26.json 的 embedding_model 保持一致。
# 校准身份校验要求三者逐字节相等，改名会让严格门禁整条链路失效。
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
PUBLIC_INDEX_VERSION = "public-v1"
PRIVATE_INDEX_VERSION = "private-v1"
EVAL_DATASET_VERSION = "cosine-gate-2026-09-26"
# 部署校准件里的候选级余弦阈值（l2_inv 空间）。
COSINE_THRESHOLD = 0.50

TENANT_ID = uuid4()
PATIENT_ID = uuid4()
USER_ID = uuid4()
QUERY_EMBEDDING = (0.11, 0.22, 0.33)


class FakeHybridIndex:
    """确定性替代 OpenSearch 的双路候选接缝。

    ``search_lexical`` / ``search_vector`` 的存在本身即让 ``RetrievalService``
    走混合检索路径并启用候选级门禁；返回内容由测试显式给定。
    """

    def __init__(
        self,
        lexical: list[SearchRecord] | None = None,
        vector: list[SearchRecord] | None = None,
        *,
        lexical_error: bool = False,
        vector_error: bool = False,
    ) -> None:
        self._lexical = list(lexical or [])
        self._vector = list(vector or [])
        self._lexical_error = lexical_error
        self._vector_error = vector_error
        self.calls: list[tuple[str, object, int]] = []

    def search_lexical(
        self, text: str, filters: object, limit: int
    ) -> list[SearchRecord]:
        self.calls.append(("lexical", text, limit))
        if self._lexical_error:
            raise RuntimeError("lexical backend unavailable")
        return list(self._lexical)[:limit]

    def search_vector(
        self, embedding: object, filters: object, limit: int
    ) -> list[SearchRecord]:
        self.calls.append(("vector", embedding, limit))
        if self._vector_error:
            raise RuntimeError("vector backend unavailable")
        return list(self._vector)[:limit]


class SingleCallIndex:
    """旧式单入口索引：没有双路接缝，无法承载候选级门禁。"""

    def search(self, query: object, **kwargs: object) -> list[SearchRecord]:
        return []


def build_record(
    record_id: str,
    *,
    vector_score: float = 0.0,
    bm25_score: float = 1.0,
    visibility: Visibility = Visibility.PUBLIC,
    index_version: str = PUBLIC_INDEX_VERSION,
    embedding_model: str | None = EMBEDDING_MODEL,
    vector_score_space: str | None = "l2_inv",
    tenant_id: UUID | None = None,
    patient_id: UUID | None = None,
    text: str | None = None,
    citation_ok: bool = True,
) -> SearchRecord:
    """构造一条公开/私有检索记录。

    默认值是「可被检索的公开记录」：``patient_id=None``（``_is_authorized``
    对公开记录要求无患者归属）且引用四要素齐全（``_has_citation`` 要求
    ``source_id``/``title``/``version``/``location`` 全非空）。
    """

    citation = (
        Citation(source_id=f"src-{record_id}", title="临床指南", version="v1", location="p1")
        if citation_ok
        else Citation(source_id="", title="", version="", location=None)
    )
    return SearchRecord(
        id=record_id,
        text=text if text is not None else f"text-{record_id}",
        visibility=visibility,
        citation=citation,
        tenant_id=tenant_id,
        patient_id=patient_id,
        bm25_score=bm25_score,
        vector_score=vector_score,
        vector_score_space=vector_score_space,
        index_version=index_version,
        embedding_model=embedding_model,
    )


@pytest.fixture
def context() -> AccessContext:
    return AccessContext(tenant_id=TENANT_ID, patient_id=PATIENT_ID, user_id=USER_ID)


@pytest.fixture
def make_record():
    """记录工厂：``make_record("id", vector_score=0.6)``。"""

    def _make(record_id: str, **overrides: object) -> SearchRecord:
        return build_record(record_id, **overrides)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def make_query():
    """查询工厂：默认带查询向量，因此向量路会真实发起。"""

    def _make(
        text: str = "阿司匹林 常规 剂量",
        *,
        top_k: int = 5,
        embedding: tuple[float, ...] | None = QUERY_EMBEDDING,
        include_private: bool = False,
        **overrides: object,
    ) -> SearchQuery:
        return SearchQuery(
            text=text,
            patient_id=None,
            top_k=top_k,
            include_public=True,
            include_private=include_private,
            embedding=embedding,
            **overrides,  # type: ignore[arg-type]
        )

    return _make


@pytest.fixture
def embedding_model_name() -> str:
    return EMBEDDING_MODEL


@pytest.fixture
def cosine_threshold() -> float:
    return COSINE_THRESHOLD


@pytest.fixture
def hybrid_index():
    return FakeHybridIndex


@pytest.fixture
def single_call_index():
    return SingleCallIndex


@pytest.fixture
def make_service():
    """构造 RetrievalService 并在测试结束后关闭线程池。"""

    created = []

    def _make(index: object, **kwargs: object):
        from medical_rag.retrieval.service import RetrievalService

        service = RetrievalService(index, **kwargs)  # type: ignore[arg-type]
        created.append(service)
        return service

    yield _make
    for service in created:
        service.close()


@pytest.fixture
def cosine_artifact() -> CalibrationArtifact:
    """启用候选级余弦门禁的校准件（与部署件同形）。"""

    return CalibrationArtifact(
        index_version="artifact-2026-09-26",
        embedding_model=EMBEDDING_MODEL,
        rrf_k=60,
        threshold=0.42,
        eval_dataset_version=EVAL_DATASET_VERSION,
        cosine_threshold=COSINE_THRESHOLD,
        score_space="l2_inv",
        public_index_version=PUBLIC_INDEX_VERSION,
        private_index_version=PRIVATE_INDEX_VERSION,
    )


@pytest.fixture
def fused_artifact() -> CalibrationArtifact:
    """未启用余弦门禁的校准件（融合分数阈值路径）。"""

    return CalibrationArtifact(
        index_version="artifact-2026-09-26",
        embedding_model=EMBEDDING_MODEL,
        rrf_k=60,
        threshold=0.42,
        eval_dataset_version=EVAL_DATASET_VERSION,
    )
