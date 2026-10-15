from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from medical_rag.knowledge.contracts import Citation, IndexRecord, PublicIndexRecord
from medical_rag.auth.policy import AccessContext
from medical_rag.provenance.contracts import SourceLocator


class Visibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class GatePolicy(StrEnum):
    """候选如何被纳入证据集。

    该策略是显式的，而不是由某个魔法默认阈值隐含：``CALIBRATED`` 使用为某一次
    物理索引构建测得的阈值，``EXPLICIT_THRESHOLD``/``RANK_GATE`` 是运维在*融合*
    候选集上提供的门禁，``COSINE_ABSOLUTE`` 是对单路向量分数的候选级门禁
    （在融合之前施加），而 ``TOP_K`` 意为"无硬门禁"：入选范围仅由 ``top_k``
    限定。未校准的部署不得静默丢弃大部分候选 —— 那正是 ``top_k=8`` 的请求
    退化成 1-2 条证据的原因。
    """

    CALIBRATED = "calibrated"
    EXPLICIT_THRESHOLD = "explicit_threshold"
    RANK_GATE = "rank_gate"
    TOP_K = "top_k"
    # 候选级绝对阈值门禁：按单路 ``vector_score`` 做相关性准入（融合前）。
    # 与作用在融合后 RRF 归一化分数上的 ``CALIBRATED``/``EXPLICIT_THRESHOLD``
    # 互斥（09-24 spec §5.1）。
    COSINE_ABSOLUTE = "cosine_absolute"


@dataclass(frozen=True, slots=True)
class PrivateIndexRecord:
    """写入私有索引的一条隐私限定作用域的证据记录。

    即便 ``parser_version`` 未作为可搜索字段暴露，它也参与确定性幂等键。
    把它保留在记录上，意味着重新解析或模型变更都能安全地产生一个新的索引版本。
    """

    id: str
    text: str
    tenant_id: UUID
    patient_id: UUID
    document_id: UUID
    document_version_id: UUID
    evidence_unit_id: UUID
    source_title: str
    source_version: str
    locator: SourceLocator | None = None
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    source_anchor: str | None = None
    confidence: float | None = None
    retrieval_eligible: bool = True
    deleted: bool = False
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    index_version: str = "private-v1"
    parser_version: str = "parser-v1"
    content_hash: str | None = None
    embedding_version: str | None = None
    source_active: bool = True
    visibility: str = "private"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # --- Task 5：溯源镜像（增量式；经 to_index_record 转发）---
    raw_text: str | None = None
    clean_text: str | None = None
    section_path: tuple[str, ...] | None = None
    parent_block_id: str | None = None
    chunk_index: int | None = None
    quality_flags: tuple[str, ...] | None = None
    cleaner_version: str | None = None
    chunker_version: str | None = None
    extractor_version: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("private index record id cannot be empty")
        if not self.text.strip():
            raise ValueError("private index record text cannot be empty")
        if self.tenant_id is None:
            raise ValueError("private index record tenant_id is required")
        if self.patient_id is None:
            raise ValueError("private index record patient_id is required")
        if self.visibility != Visibility.PRIVATE.value:
            raise ValueError("private index records must use private visibility")
        for name in ("retrieval_eligible", "source_active", "deleted"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"private index record {name} must be a boolean")

    def to_index_record(self) -> IndexRecord:
        """把这条隐私限定作用域的记录规范化为共享契约。"""

        derived_anchor = self.source_anchor or (
            self.locator.build_anchor() if self.locator is not None else None
        )
        derived_location = derived_anchor or (
            f"p{self.page_number}" if self.page_number is not None else None
        )
        if self.locator is not None and not derived_location:
            derived_location = self.locator.display_location()

        return IndexRecord(
            id=self.id,
            text=self.text,
            visibility=self.visibility,
            tenant_id=self.tenant_id,
            patient_id=self.patient_id,
            document_id=self.document_id,
            document_version_id=self.document_version_id,
            evidence_unit_id=self.evidence_unit_id,
            locator=self.locator,
            # 派生出的引用标识/定位器。``RetrievalService._has_citation``
            # 只暴露其引用具备 source_id、title、
            # version *和* location 的记录，而本记录以
            # 不同的名称持有文档标识和
            # 定位器（``document_id``/``source_anchor``）。没有这层派生，
            # 患者证据会被写入索引却永远
            # 无法被检索。对每条
            # 可检索的私有记录而言 ``source_anchor`` 都保证非空，因此页面兜底只
            # 覆盖那些本来就更早被过滤掉的记录。
            source_id=str(self.document_id),
            location=derived_location,
            source_title=self.source_title,
            source_version=self.source_version,
            page_number=self.page_number,
            sheet_name=self.sheet_name,
            cell_range=self.cell_range,
            source_anchor=derived_anchor,
            confidence=self.confidence,
            content_hash=self.content_hash,
            parser_version=self.parser_version,
            embedding=self.embedding,
            embedding_model=self.embedding_model,
            embedding_version=self.embedding_version,
            embedding_dimensions=self.embedding_dimensions,
            index_version=self.index_version,
            retrieval_eligible=self.retrieval_eligible,
            source_active=self.source_active,
            deleted=self.deleted,
            created_at=self.created_at,
            updated_at=self.updated_at,
            raw_text=self.raw_text,
            clean_text=self.clean_text,
            section_path=self.section_path,
            parent_block_id=self.parent_block_id,
            chunk_index=self.chunk_index,
            quality_flags=self.quality_flags,
            cleaner_version=self.cleaner_version,
            chunker_version=self.chunker_version,
            extractor_version=self.extractor_version,
        )

    def to_dict(self) -> dict[str, object]:
        return self.to_index_record().to_dict()

    @property
    def idempotency_key(self) -> tuple[UUID, UUID, str, str | None]:
        """返回安全重试所需的稳定写入键。"""

        return (
            self.document_version_id,
            self.evidence_unit_id,
            self.parser_version,
            self.embedding_model,
        )


class PrivateIndexWriter(Protocol):
    """患者私有索引实现的持久化接缝。"""

    def upsert(self, records: list[PrivateIndexRecord | IndexRecord]) -> None: ...

    def delete_document(self, document_id: UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str
    patient_id: UUID | None
    top_k: int = 10
    include_public: bool = True
    include_private: bool = True
    embedding: tuple[float, ...] | None = None
    document_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("检索文本不能为空")
        if self.top_k < 1 or self.top_k > 100:
            raise ValueError("top_k 必须在 1 到 100 之间")


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    """两条候选路径共享的租户/患者与可见性边界。"""

    tenant_id: UUID
    patient_id: UUID
    include_public: bool = True
    include_private: bool = True
    document_ids: tuple[UUID, ...] = ()


class CandidateRetriever(Protocol):
    """分离的词法与向量候选检索接缝。"""

    def search_lexical(
        self, text: str, filters: RetrievalFilters, limit: int
    ) -> list["SearchRecord"]: ...

    def search_vector(
        self, embedding: tuple[float, ...], filters: RetrievalFilters, limit: int
    ) -> list["SearchRecord"]: ...


@dataclass(frozen=True, slots=True)
class SearchRecord:
    id: str
    text: str
    visibility: Visibility
    citation: Citation
    tenant_id: UUID | None = None
    patient_id: UUID | None = None
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    evidence_unit_id: UUID | None = None
    locator: SourceLocator | None = None
    bm25_score: float = 0.0
    # P1-5（评审 2026-10-12）：``vector_score`` 的语义**不是余弦相似度**。
    # OpenSearch kNN 索引未声明 ``space_type`` 时默认 L2，``_score`` 实际是
    # ``1 / (1 + L2^2)``（检索调参报告 §2 已实测验证，三例误差 <1e-3）。
    # 未来任何人拿它做绝对阈值前，必须先看 ``vector_score_space``；
    # 「真余弦 0.53 ↔ l2_inv 0.5155」的数值巧合曾导致错误的门禁设计。
    vector_score: float = 0.0
    # ``l2_inv``（OpenSearch 默认 L2 距离的倒数变换）或 ``cosine``。
    # 由 opensearch_store._record_from_hit 按 score_mode 填充；内存索引等
    # 非生产实现保持 None，表示语义未知、不得用于阈值比较。
    vector_score_space: str | None = None
    deleted: bool = False
    source_active: bool = True
    retrieval_eligible: bool = True
    # ``fused_score`` 是原始 RRF 分数；``calibrated_score`` 是
    # 适合做阈值的归一化值。两者都与
    # 原始 BM25/向量分数保持分离。
    fused_score: float | None = None
    calibrated_score: float | None = None
    index_version: str | None = None
    embedding_model: str | None = None
    index_name: str | None = None
    parser_version: str | None = None
    content_hash: str | None = None
    parent_block_id: str | None = None

    @property
    def score(self) -> float:
        if self.fused_score is not None:
            return self.fused_score
        return (self.bm25_score * 0.7) + (self.vector_score * 0.3)


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    text: str
    citation: Citation
    score: float
    visibility: Visibility
    tenant_id: UUID | None = None
    patient_id: UUID | None = None
    document_id: UUID | None = None
    parent_block_id: str | None = None
    locator: SourceLocator | None = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    items: list[Evidence]
    reason: str | None = None
    # 诊断信息有意做成增量式，使既有的按位置调用方
    # （items, reason）保持原样可用。
    diagnostics: dict[str, object] = field(default_factory=dict)


class SearchIndex(Protocol):
    def search(
        self,
        query: SearchQuery,
        *,
        tenant_id: UUID,
        patient_id: UUID,
        include_public: bool,
        include_private: bool,
        limit: int,
    ) -> list[SearchRecord]: ...


class Retriever(Protocol):
    def search(self, query: SearchQuery, context: AccessContext) -> list[Evidence]: ...


# 重排器唯一的返回类型：``RankedPassage``（index + score）行列表，按 Provider
# 顺序返回。
#
# 此前 ``retrieval.contracts.Reranker`` 声明 ``list[int]``，而
# ``models.contracts.RerankerModel`` 与 ``OpenAIRerankerAdapter`` 返回
# ``list[RankedPassage]``：同一个组件有两个互相矛盾的契约，调用方只能靠
# ``hasattr`` 猜返回的到底是什么。这里把返回类型固定成一个具名类型，并在
# :func:`coerce_rerank_order` 里集中做形状归一。
#
# 之所以是 ``list`` 子类而不是 ``RerankResult = list[RankedPassage]`` 别名：
# ``models.contracts`` 与 ``retrieval.contracts`` 互相导入（前者取 ``Evidence``，
# 后者本应取 ``RankedPassage``），别名会在 ``models.contracts`` 先被导入时触发
# 循环导入。子类在运行期就是 ``list``，因此 ``RerankerModel`` 的
# ``list[RankedPassage]`` 声明依然成立，无需改动模型层。
class RerankResult(list):
    """重排结果：``RankedPassage`` 行的列表（Provider 顺序，未归一）。"""

    __slots__ = ()

    def order(self) -> tuple[int, ...]:
        """规范顺序的候选下标（分数降序）。"""

        return coerce_rerank_order(self)


class Reranker(Protocol):
    def rank(self, query: str, passages: list[str]) -> RerankResult: ...


def coerce_rerank_order(order: object) -> tuple[int, ...]:
    """把重排器返回值归一为「规范顺序的候选下标」。

    接受三种形态（历史适配器、当前适配器、测试替身）：
    * 带 ``index``/``score`` 的行（``RankedPassage``/``RerankResult``）：按
      ``score`` 降序即规范顺序；
    * 整数下标序列：原样保留（调用方已给出顺序）；
    * ``None`` / 空：表示「不改变顺序」。

    非整数、无法解释的元素被跳过而不是抛错——重排是可选增强，不能因为某个
    适配器多返回一个字段就让整次检索失败。
    """

    if order is None:
        return ()
    values = list(order) if isinstance(order, (list, tuple)) else []
    rows = [
        item for item in values if hasattr(item, "index") and hasattr(item, "score")
    ]
    if rows and len(rows) == len(values):
        try:
            return tuple(
                int(row.index)
                for row in sorted(values, key=lambda item: float(item.score), reverse=True)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("重排结果包含无法解释的 index/score") from exc
    return tuple(
        item for item in values if isinstance(item, int) and not isinstance(item, bool)
    )


class InMemorySearchIndex:
    """确定性的测试/开发索引，其可见性边界与 OpenSearch 相同。"""

    def __init__(self, records: list[SearchRecord] | None = None) -> None:
        self.records = list(records or [])

    def upsert(self, records: list[IndexRecord | PublicIndexRecord]) -> None:
        """接受规范化记录，同时保留遗留的 append 接缝。"""

        for value in records:
            normalized = value if isinstance(value, IndexRecord) else value.to_index_record()
            if normalized.visibility != Visibility.PUBLIC.value:
                raise ValueError("public search index accepts only public records")
            self.index_public_chunk(normalized)
            latest = self.records.pop()
            self.records[:] = [item for item in self.records if item.id != latest.id]
            self.records.append(latest)

    def delete_record(self, record_id: str | UUID) -> None:
        """按索引 ID 幂等地移除一条记录。

        对账删除接缝，镜像 :meth:`OpenSearchIndexWriter.delete_record`
        （ID 缺失是空操作，而非错误）。没有它，内存索引只能摄入并提供公开记录，
        却永远无法让对账修复一条残留项，因此在开发/测试中一个已停用的知识源
        会永远保持可检索。
        """
        target = str(record_id)
        self.records[:] = [item for item in self.records if item.id != target]

    def index_public_chunk(self, record: PublicIndexRecord | IndexRecord) -> None:
        normalized = record if isinstance(record, IndexRecord) else record.to_index_record()
        self.records.append(
            SearchRecord(
                id=normalized.id,
                text=normalized.text,
                visibility=Visibility.PUBLIC,
                citation=Citation(
                    source_id=normalized.source_id or normalized.id,
                    title=normalized.source_title or "",
                    version=normalized.source_version or "",
                    location=(
                        normalized.location
                        or (normalized.locator.display_location() if normalized.locator else None)
                    ),
                    url=normalized.url,
                    chunk_id=normalized.id,
                    document_id=str(normalized.document_id) if normalized.document_id else None,
                    document_version_id=(
                        str(normalized.document_version_id)
                        if normalized.document_version_id
                        else None
                    ),
                    evidence_unit_id=(
                        str(normalized.evidence_unit_id)
                        if normalized.evidence_unit_id
                        else None
                    ),
                    locator=normalized.locator,
                ),
                document_id=normalized.document_id,
                document_version_id=(
                    UUID(str(normalized.document_version_id))
                    if normalized.document_version_id
                    else None
                ),
                evidence_unit_id=(
                    UUID(str(normalized.evidence_unit_id))
                    if normalized.evidence_unit_id
                    else None
                ),
                locator=normalized.locator,
                bm25_score=1.0,
                vector_score=1.0,
                deleted=normalized.deleted,
                source_active=normalized.source_active,
                retrieval_eligible=normalized.retrieval_eligible,
                index_version=normalized.index_version,
                embedding_model=normalized.embedding_model,
                parser_version=normalized.parser_version,
                content_hash=normalized.content_hash,
                parent_block_id=normalized.parent_block_id,
            )
        )

    def search(
        self,
        query: SearchQuery,
        *,
        tenant_id: UUID,
        patient_id: UUID,
        include_public: bool,
        include_private: bool,
        limit: int,
    ) -> list[SearchRecord]:
        query_terms = {term.lower() for term in query.text.split() if term.strip()}
        candidates: list[SearchRecord] = []
        for record in self.records:
            if record.deleted or not record.source_active or not record.retrieval_eligible:
                continue
            if record.visibility == Visibility.PUBLIC:
                if not include_public:
                    continue
                if query.document_ids and record.document_id not in query.document_ids:
                    continue
            elif record.visibility == Visibility.PRIVATE:
                if not include_private:
                    continue
                if record.tenant_id != tenant_id or record.patient_id != patient_id:
                    continue
                if query.document_ids and record.document_id not in query.document_ids:
                    continue
            else:
                continue
            lexical = sum(1 for term in query_terms if term in record.text.lower())
            score = record.score + min(0.2, lexical * 0.05)
            candidates.append(
                SearchRecord(
                    id=record.id,
                    text=record.text,
                    visibility=record.visibility,
                    citation=record.citation,
                    tenant_id=record.tenant_id,
                    patient_id=record.patient_id,
                    document_id=record.document_id,
                    document_version_id=record.document_version_id,
                    evidence_unit_id=record.evidence_unit_id,
                    locator=record.locator,
                    bm25_score=score / 0.7 if record.bm25_score or record.vector_score else score,
                    vector_score=record.vector_score,
                    deleted=record.deleted,
                    source_active=record.source_active,
                    retrieval_eligible=record.retrieval_eligible,
                    fused_score=record.fused_score,
                    calibrated_score=record.calibrated_score,
                    index_version=record.index_version,
                    embedding_model=record.embedding_model,
                    index_name=record.index_name,
                    parser_version=record.parser_version,
                    content_hash=record.content_hash,
                    parent_block_id=record.parent_block_id,
                )
            )
        candidates.sort(key=lambda item: (-item.score, item.id))
        return candidates[:limit]

    def search_by_parent(
        self,
        parent_block_id: str,
        *,
        limit: int = 20,
        tenant_id: UUID | None = None,
        patient_id: UUID | None = None,
        include_public: bool = True,
        include_private: bool = True,
        document_id: UUID | None = None,
    ) -> list[SearchRecord]:
        """返回共享同一父块的兄弟子块（按阅读顺序）。

        Task 9 父/子扩展接缝。在 OpenSearch 适配器上，这通过 ``parent_block_id``
        过滤外加相同的租户/患者 ACL 边界完成；这里则由内存索引扫描其记录列表。
        没有 ``parent_block_id``、或父块不匹配的记录被排除，且 ACL/生命周期门禁
        （已删除、未激活、不合格、租户/患者）会被强制执行，因此兄弟扩展绝不跨
        作用域泄漏。

        ``document_id``（给定时）把查找限定在一份文档内：``parent_block_id``
        只在*一份文档内*唯一，否则两份文档的 ``blk-0000`` 会被混进同一个上下文。
        """

        siblings = []
        for record in self.records:
            if record.parent_block_id != parent_block_id:
                continue
            if document_id is not None and record.document_id != document_id:
                continue
            if record.deleted or not record.source_active or not record.retrieval_eligible:
                continue
            if record.visibility == Visibility.PUBLIC:
                if not include_public:
                    continue
            elif record.visibility == Visibility.PRIVATE:
                if not include_private:
                    continue
                if record.tenant_id != tenant_id or record.patient_id != patient_id:
                    continue
            else:
                continue
            siblings.append(record)
        # 阅读顺序：按稳定 id 排序以保证确定性。
        return sorted(siblings, key=lambda r: r.id)[:limit]
