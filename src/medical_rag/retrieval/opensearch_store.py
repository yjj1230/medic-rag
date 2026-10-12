from __future__ import annotations

from typing import Any
from uuid import UUID

from medical_rag.knowledge.contracts import Citation
from medical_rag.retrieval.contracts import (
    CandidateRetriever,
    RetrievalFilters,
    SearchIndex,
    SearchQuery,
    SearchRecord,
    Visibility,
)

# 连续词组（如「血红蛋白偏高」）的提权系数。中文按词检索依赖 mapping 上的
# 中文分析器；短语提权让完全连续的词组优先于散落单字命中。
_PHRASE_BOOST = 2.0

# P1-5（评审 2026-10-12）：本存储返回的 vector_score 语义标注。
# OpenSearch kNN 在 mapping 未声明 space_type 时默认 L2，``_score`` 实际是
# ``1 / (1 + L2^2)`` 而非余弦相似度（检索调参报告 §2 已实测验证，误差
# <1e-3）。命名沿用调参报告的 ``l2_inv``。**禁止**把该分数当余弦用——
# 真余弦 0.53 与 l2_inv 0.5155 数值巧合不相等，曾导致错误的门禁设计。
# 若将来在 opensearch_writer 的 mapping 中显式声明 ``space_type:
# cosinesimil``，必须同步修改本常量并重建索引（映射不可原地修改）。
VECTOR_SCORE_SPACE = "l2_inv"


def _text_query_clauses(text: str) -> list[dict[str, Any]]:
    """中文文本字段的 OR 查询子句（词级 + 词组提权）。"""

    return [
        {
            "multi_match": {
                "query": text,
                "fields": ["text"],
                "type": "best_fields",
            }
        },
        {"match_phrase": {"text": {"query": text, "boost": _PHRASE_BOOST}}},
    ]


def _text_query(text: str) -> dict[str, Any]:
    """中文文本字段的统一查询形态（词法候选路径使用）。

    单用 ``{"match": {"text": ...}}`` 在缺少中文分析器时退化为单字匹配，
    词级区分度丧失。这里改为 ``multi_match``（词级）+ ``match_phrase``
    （词组提权）的组合，命中其一即算候选。
    """

    return {
        "bool": {
            "should": _text_query_clauses(text),
            "minimum_should_match": 1,
        }
    }


class OpenSearchStore(SearchIndex, CandidateRetriever):
    """Thin OpenSearch adapter; authorization filters are part of the query body."""

    def __init__(
        self,
        client: Any,
        public_index: str = "medical-rag-public",
        private_index: str = "medical-rag-private",
    ) -> None:
        self.client = client
        self.public_index = public_index
        self.private_index = private_index

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
        should: list[dict[str, Any]] = []
        indices: list[str] = []
        if include_public:
            indices.append(self.public_index)
            public_filters: list[dict[str, Any]] = [
                {"term": {"visibility": Visibility.PUBLIC.value}}
            ]
            if query.document_ids:
                public_filters.append(
                    {"terms": {"document_id": [str(value) for value in query.document_ids]}}
                )
            should.append({"bool": {"filter": public_filters}})
        if include_private:
            indices.append(self.private_index)
            private_filters = [
                {"term": {"visibility": Visibility.PRIVATE.value}},
                {"term": {"tenant_id": str(tenant_id)}},
                {"term": {"patient_id": str(patient_id)}},
            ]
            if query.document_ids:
                private_filters.append(
                    {"terms": {"document_id": [str(document_id) for document_id in query.document_ids]}}
                )
            should.append(
                {
                    "bool": {
                        "filter": private_filters
                    }
                }
            )
        if not should:
            return []
        should_queries: list[dict[str, Any]] = _text_query_clauses(query.text)
        if query.embedding:
            should_queries.append(
                {"knn": {"embedding": {"vector": list(query.embedding), "k": limit}}}
            )
        body = {
            "size": limit,
            "query": {
                "bool": {
                    "should": should_queries,
                    "minimum_should_match": 1,
                    # Fail closed for legacy documents that do not carry the
                    # review-gate marker: only an explicit true is searchable.
                    "filter": [
                        {"term": {"retrieval_eligible": True}},
                        {"bool": {"should": should, "minimum_should_match": 1}},
                    ],
                    "must_not": [
                        {"term": {"deleted": True}},
                        {"term": {"source_active": False}},
                        {"term": {"retrieval_eligible": False}},
                    ],
                }
            },
        }
        response = self.client.search(index=indices, body=body)
        return [
            record
            for hit in response.get("hits", {}).get("hits", [])
            if (record := self._record_from_hit(hit, allowed_indices=indices, require_index=True)) is not None
        ]

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
        """Return sibling child-blocks sharing a parent block (reading order).

        Implements the ``search_by_parent`` seam that
        ``retrieval.expansion.expand_with_parents`` calls through
        ``ScopedSiblingLookup``.  ``parent_block_id`` is a deterministic record
        field, so a plain term match on it (lexical path) is the exact, cheapest
        lookup — no embedding needed.  The same public/private tenant/patient
        ACL boundary as ``search`` / ``_visibility_filter`` is enforced here so
        sibling context never crosses patients.

        ``document_id`` additionally binds the lookup to one document:
        ``make_block_id`` guarantees ``parent_block_id`` uniqueness only
        *within a document*, so without this filter another document's
        ``blk-0000`` siblings would be admitted as valid evidence context.
        """
        if not parent_block_id or limit < 1:
            return []
        indices: list[str] = []
        should: list[dict[str, Any]] = []
        if include_public:
            indices.append(self.public_index)
            should.append(
                {"bool": {"filter": [{"term": {"visibility": Visibility.PUBLIC.value}}]}}
            )
        if include_private:
            if tenant_id is None or patient_id is None:
                return []
            indices.append(self.private_index)
            should.append(
                {
                    "bool": {
                        "filter": [
                            {"term": {"visibility": Visibility.PRIVATE.value}},
                            {"term": {"tenant_id": str(tenant_id)}},
                            {"term": {"patient_id": str(patient_id)}},
                        ]
                    }
                }
            )
        if not should:
            return []
        must: list[dict[str, Any]] = [{"term": {"parent_block_id": parent_block_id}}]
        if document_id is not None:
            must.append({"term": {"document_id": str(document_id)}})
        body = {
            "size": limit,
            "query": {
                "bool": {
                    "must": must,
                    "filter": [
                        {"term": {"retrieval_eligible": True}},
                        {"bool": {"should": should, "minimum_should_match": 1}},
                    ],
                    "must_not": [
                        {"term": {"deleted": True}},
                        {"term": {"source_active": False}},
                        {"term": {"retrieval_eligible": False}},
                    ],
                }
            },
        }
        response = self.client.search(index=indices, body=body)
        return [
            record
            for hit in response.get("hits", {}).get("hits", [])
            if (record := self._record_from_hit(hit, allowed_indices=indices, require_index=True)) is not None
        ]

    #: Identity metadata a reconciliation scan compares.  ``text`` is deliberately
    #: absent: reconciliation must be able to report on the index without pulling
    #: passage prose -- public or patient -- into the reporting process.
    SCAN_FIELDS = (
        "visibility",
        "tenant_id",
        "patient_id",
        "document_id",
        "document_version_id",
        "evidence_unit_id",
        "source_id",
        "source_version",
        "content_hash",
        "parser_version",
        "embedding_model",
        "index_version",
        "retrieval_eligible",
        "source_active",
        "deleted",
    )

    def scan(self, scope: str = "public", limit: int = 100) -> list[dict[str, Any]]:
        """Enumerate index metadata for one scope, without reading passage text.

        This is the observation half of reconciliation: the comparison needs to
        know what is *in* the index, and the scan queries the **read alias** so it
        sees exactly what retrieval would serve.

        The seam did not exist before, and its absence was not harmless.  The
        write path holds the physical index name while the read path only knows
        the alias, so neither adapter could list its contents; reconciliation
        wired against one of them observed nothing, and a scan comparing the
        database against nothing reports a capability gap as a clean index.  The
        operator command refuses to report a verdict when this seam is missing
        rather than guessing.

        Returns one mapping per hit carrying :attr:`SCAN_FIELDS` plus ``id``.
        """

        if limit <= 0:
            return []
        index = self.public_index if scope == "public" else self.private_index
        response = self.client.search(
            index=index,
            body={
                "_source": list(self.SCAN_FIELDS),
                "size": limit,
                "query": {"term": {"visibility": scope}},
            },
        )
        hits = (response or {}).get("hits", {}).get("hits", []) or []
        records: list[dict[str, Any]] = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            record = dict(hit.get("_source") or {})
            # ``id`` is the index document id, which is the record identity the
            # scan keys on.  Not every writer repeats it inside ``_source``, and
            # a scan that loses the id skips every hit it reads.
            record["id"] = str(hit.get("_id"))
            records.append(record)
        return records[:limit]

    def _visibility_filter(self, filters: RetrievalFilters) -> tuple[list[str], dict[str, Any]]:
        should: list[dict[str, Any]] = []
        indices: list[str] = []
        if filters.include_public:
            indices.append(self.public_index)
            public_filters: list[dict[str, Any]] = [
                {"term": {"visibility": Visibility.PUBLIC.value}}
            ]
            if filters.document_ids:
                public_filters.append(
                    {"terms": {"document_id": [str(value) for value in filters.document_ids]}}
                )
            should.append({"bool": {"filter": public_filters}})
        if filters.include_private:
            indices.append(self.private_index)
            private_filters: list[dict[str, Any]] = [
                {"term": {"visibility": Visibility.PRIVATE.value}},
                {"term": {"tenant_id": str(filters.tenant_id)}},
                {"term": {"patient_id": str(filters.patient_id)}},
            ]
            if filters.document_ids:
                private_filters.append(
                    {"terms": {"document_id": [str(value) for value in filters.document_ids]}}
                )
            should.append({"bool": {"filter": private_filters}})
        return indices, {"bool": {"should": should, "minimum_should_match": 1}}

    def _candidate_search(
        self,
        *,
        mode: str,
        text: str | None,
        embedding: tuple[float, ...] | None,
        filters: RetrievalFilters,
        limit: int,
    ) -> list[SearchRecord]:
        if limit < 1:
            return []
        indices, acl_filter = self._visibility_filter(filters)
        if not indices:
            return []
        if mode == "lexical":
            candidate_query: dict[str, Any] = _text_query(text or "")
        else:
            if not embedding:
                return []
            candidate_query = {"knn": {"embedding": {"vector": list(embedding), "k": limit}}}
        body = {
            "size": limit,
            "query": {
                "bool": {
                    "must": [candidate_query],
                    "filter": [
                        {"term": {"retrieval_eligible": True}},
                        acl_filter,
                    ],
                    "must_not": [
                        {"term": {"deleted": True}},
                        {"term": {"source_active": False}},
                        {"term": {"retrieval_eligible": False}},
                    ],
                }
            },
        }
        response = self.client.search(index=indices, body=body)
        return [
            record
            for hit in response.get("hits", {}).get("hits", [])
            if (
                record := self._record_from_hit(
                    hit, score_mode=mode, allowed_indices=indices, require_index=True
                )
            )
            is not None
        ]

    def search_lexical(self, text: str, filters: RetrievalFilters, limit: int) -> list[SearchRecord]:
        return self._candidate_search(
            mode="lexical", text=text, embedding=None, filters=filters, limit=limit
        )

    def search_vector(
        self, embedding: tuple[float, ...], filters: RetrievalFilters, limit: int
    ) -> list[SearchRecord]:
        return self._candidate_search(
            mode="vector", text=None, embedding=embedding, filters=filters, limit=limit
        )

    def _record_from_hit(
        self,
        hit: dict[str, Any],
        score_mode: str | None = None,
        allowed_indices: list[str] | None = None,
        require_index: bool = False,
    ) -> SearchRecord | None:
        source = hit.get("_source", {})
        visibility = Visibility(source.get("visibility", Visibility.PUBLIC.value))
        index_name = hit.get("_index")
        if allowed_indices and index_name is None and bool(source.get("retrieval_eligible", False)):
            # An explicitly eligible hit without provenance cannot be bound to
            # the public/private ACL boundary; fail closed.
            return None
        if require_index and allowed_indices and not any(
            index_name == configured or str(index_name).startswith(configured + "-")
            for configured in allowed_indices
        ):
            return None
        if allowed_indices and index_name:
            is_public_index = index_name == self.public_index or str(index_name).startswith(
                self.public_index + "-"
            )
            is_private_index = index_name == self.private_index or str(index_name).startswith(
                self.private_index + "-"
            )
            if is_public_index and visibility is not Visibility.PUBLIC:
                return None
            if is_private_index and visibility is not Visibility.PRIVATE:
                return None
        citation = Citation(
            source_id=str(source.get("source_id", source.get("document_id", hit.get("_id", "")))),
            title=str(source.get("source_title", source.get("title", ""))),
            version=str(source.get("source_version", source.get("version", ""))),
            location=source.get("location"),
            url=source.get("url"),
        )
        return SearchRecord(
            id=str(source.get("id", hit.get("_id", ""))),
            text=str(source.get("text", "")),
            visibility=visibility,
            citation=citation,
            tenant_id=UUID(source["tenant_id"]) if source.get("tenant_id") else None,
            patient_id=UUID(source["patient_id"]) if source.get("patient_id") else None,
            document_id=UUID(source["document_id"]) if source.get("document_id") else None,
            # P1-5（评审 2026-10-12）：分数只在**自己的检索模式**下读取。
            # 旧实现 `source.get("bm25_score", ...)` 在文档 body 残留同名
            # 字段时（历史写入），词法字段会漏进 vector 模式的记录（反之亦
            # 然），污染 RRF 诊断。现在按 score_mode 显式分流。
            bm25_score=(
                float(source.get("bm25_score") or hit.get("_score", 0.0) or 0.0)
                if score_mode in (None, "lexical")
                else 0.0
            ),
            vector_score=(
                float(source.get("vector_score") or hit.get("_score", 0.0) or 0.0)
                if score_mode == "vector"
                else 0.0
            ),
            # 分数语义标注：OpenSearch kNN 默认 space_type=l2，_score 是
            # 1/(1+L2^2) 而非余弦（检索调参报告 §2 实测验证）。只有 vector
            # 模式的记录才携带该标注；None 表示语义未知，不得用于阈值比较。
            vector_score_space=VECTOR_SCORE_SPACE if score_mode == "vector" else None,
            # Legacy hits without explicit lifecycle markers fail closed.
            deleted=bool(source.get("deleted", False)),
            source_active=bool(source.get("source_active", False)),
            # Old index documents that predate the review gate are not safe to
            # expose by default; only an explicit true marker is eligible.
            retrieval_eligible=bool(source.get("retrieval_eligible", False)),
            index_version=source.get("index_version"),
            embedding_model=source.get("embedding_model"),
            index_name=index_name,
            parent_block_id=source.get("parent_block_id"),
        )
