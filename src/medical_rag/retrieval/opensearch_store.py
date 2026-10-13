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
from medical_rag.provenance.contracts import SourceLocator

# 连续词组（如「血红蛋白偏高」）的提权系数。中文按词检索依赖 mapping 上的
# 中文分析器；短语提权让完全连续的词组优先于散落单字命中。
_PHRASE_BOOST = 2.0

# P1-5（评审 2026-10-12）：本存储返回的 vector_score 语义标注由物理
# mapping 的 ``embedding.method.space_type`` 决定。只接受 ``l2_inv`` 和
# ``cosinesimil``；缺失或不可读时保持未知，避免把 OpenSearch 默认行为猜成
# 可用于绝对门禁的单位。
_SCORE_SPACES = frozenset({"l2_inv", "cosinesimil"})
MAPPING_UNAVAILABLE = "mapping_unavailable"


def _locator_from_source(payload: object) -> SourceLocator | None:
    if not isinstance(payload, dict):
        return None
    values = dict(payload)
    values["section_path"] = tuple(values.get("section_path") or ())
    if values.get("bbox") is not None:
        values["bbox"] = tuple(values["bbox"])
    allowed = SourceLocator.__dataclass_fields__
    return SourceLocator(**{key: value for key, value in values.items() if key in allowed})


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
    """轻量 OpenSearch 适配器；授权过滤是查询体的一部分。"""

    def __init__(
        self,
        client: Any,
        public_index: str = "medical-rag-public",
        private_index: str = "medical-rag-private",
        vector_score_space: str | None = None,
    ) -> None:
        if vector_score_space is not None and vector_score_space not in _SCORE_SPACES:
            raise ValueError(f"vector_score_space must be one of {sorted(_SCORE_SPACES)}")
        self.client = client
        self.public_index = public_index
        self.private_index = private_index
        self.vector_score_space = vector_score_space
        self._resolved_vector_score_space: str | None = None
        self._mapping_score_space_resolved = False

    def resolve_vector_score_space(self) -> str:
        """Return the vector score space declared by the physical mapping.

        The explicit constructor value is an operator supplied expectation.  The
        physical mapping is always read and compared against it.  Only
        ``embedding.method.space_type`` values in the supported score-space set are
        accepted; a missing, divergent, or inaccessible mapping returns
        ``mapping_unavailable`` so callers can fail closed instead of guessing.
        """

        if self._mapping_score_space_resolved:
            return self._resolved_vector_score_space or MAPPING_UNAVAILABLE
        self._mapping_score_space_resolved = True
        try:
            indices = getattr(self.client, "indices", None)
            get_mapping = getattr(indices, "get_mapping", None)
            if not callable(get_mapping):
                return MAPPING_UNAVAILABLE
            spaces: set[str] = set()
            for index_name in dict.fromkeys((self.public_index, self.private_index)):
                mapping = get_mapping(index=index_name)
                if not isinstance(mapping, dict):
                    return MAPPING_UNAVAILABLE
                body = mapping if "mappings" in mapping else mapping.get(index_name)
                if not isinstance(body, dict):
                    body = next(
                        (value for value in mapping.values() if isinstance(value, dict)), {}
                    )
                properties = (body.get("mappings") or {}).get("properties")
                embedding = properties.get("embedding") if isinstance(properties, dict) else None
                method = embedding.get("method") if isinstance(embedding, dict) else None
                space_type = method.get("space_type") if isinstance(method, dict) else None
                if space_type not in _SCORE_SPACES:
                    return MAPPING_UNAVAILABLE
                spaces.add(space_type)
            if len(spaces) != 1:
                return MAPPING_UNAVAILABLE
            resolved_space = next(iter(spaces))
            self._resolved_vector_score_space = resolved_space
            if self.vector_score_space is None:
                self.vector_score_space = resolved_space
            return resolved_space
        except Exception:
            return MAPPING_UNAVAILABLE

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
                    # 对不带复核门禁标记的遗留文档 fail closed：
                    # 只有显式的 true 才可检索。
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
        """返回共享某一父块的兄弟子块（按阅读顺序）。

        实现 ``retrieval.expansion.expand_with_parents`` 经
        ``ScopedSiblingLookup`` 调用的 ``search_by_parent`` 接缝。
        ``parent_block_id`` 是一个确定性记录字段，因此对它做普通词项匹配
        （词法路径）就是精确、最廉价的查找 —— 无需向量化。与 ``search`` /
        ``_visibility_filter`` 相同的公开/私有租户/患者 ACL 边界在此被强制执行，
        因此兄弟上下文绝不跨患者。

        ``document_id`` 另把查找绑定到一份文档：``make_block_id`` 只保证
        ``parent_block_id`` 在*一份文档内*唯一，因此没有这个过滤，
        另一份文档的 ``blk-0000`` 兄弟块会被当作有效证据上下文纳入。
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

    #: 对账扫描比较的标识元数据。``text`` 有意
    #: 缺席：对账必须能在不把正文
    #: （无论公开还是患者）拉进上报进程的前提下报告索引情况。
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
        "locator",
        "retrieval_eligible",
        "source_active",
        "deleted",
    )

    def scan(self, scope: str = "public", limit: int = 100) -> list[dict[str, Any]]:
        """枚举某一作用域的索引元数据，不读取正文。

        这是对账的观测侧：比较需要知道索引里*有什么*，而扫描查询**读别名**，
        因此它看到的正是检索会提供的内容。

        该接缝此前不存在，而其缺失并非无害。写入路径持有物理索引名，读路径却只
        知道别名，因此两个适配器都无法列出其内容；对账接到其中之一时什么都观测
        不到，而一个拿数据库与"空"做比较的扫描会把能力缺口报成干净的索引。
        当该接缝缺失时，运维命令拒绝给出结论，而不是靠猜。

        每个命中返回一个映射，携带 :attr:`SCAN_FIELDS` 外加 ``id``。
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
            # ``id`` 是索引文档 id，也就是扫描作为键的
            # 记录标识。并非每个写入器都会在 ``_source`` 里重复它，而
            # 一个丢掉 id 的扫描会跳过它读到的每一个命中。
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
        resolved_space = self.resolve_vector_score_space() if score_mode == "vector" else None
        visibility = Visibility(source.get("visibility", Visibility.PUBLIC.value))
        index_name = hit.get("_index")
        if allowed_indices and index_name is None and bool(source.get("retrieval_eligible", False)):
            # 一条显式合格却没有溯源的命中无法绑定到
            # 公开/私有 ACL 边界；fail closed。
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
        locator = _locator_from_source(source.get("locator"))
        citation = Citation(
            source_id=str(source.get("source_id", source.get("document_id", hit.get("_id", "")))),
            title=str(source.get("source_title", source.get("title", ""))),
            version=str(source.get("source_version", source.get("version", ""))),
            location=source.get("location") or (locator.display_location() if locator else None),
            url=source.get("url"),
            chunk_id=str(source.get("id", hit.get("_id", ""))),
            document_id=source.get("document_id"),
            document_version_id=source.get("document_version_id"),
            evidence_unit_id=source.get("evidence_unit_id"),
            locator=locator,
        )
        return SearchRecord(
            id=str(source.get("id", hit.get("_id", ""))),
            text=str(source.get("text", "")),
            visibility=visibility,
            citation=citation,
            tenant_id=UUID(source["tenant_id"]) if source.get("tenant_id") else None,
            patient_id=UUID(source["patient_id"]) if source.get("patient_id") else None,
            document_id=UUID(source["document_id"]) if source.get("document_id") else None,
            document_version_id=(
                UUID(source["document_version_id"])
                if source.get("document_version_id")
                else None
            ),
            evidence_unit_id=(
                UUID(source["evidence_unit_id"])
                if source.get("evidence_unit_id")
                else None
            ),
            locator=locator,
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
            # 只有 vector 模式的记录才携带映射宣告的分数空间；映射不可用时
            # 保留 None，迫使严格门禁拒绝比较未知单位的数值。
            vector_score_space=(
                resolved_space
                if resolved_space in _SCORE_SPACES
                else None
            ),
            # 不带显式生命周期标记的遗留命中 fail closed。
            deleted=bool(source.get("deleted", False)),
            source_active=bool(source.get("source_active", False)),
            # 早于复核门禁的旧索引文档默认
            # 不安全，不可暴露；只有显式的 true 标记才合格。
            retrieval_eligible=bool(source.get("retrieval_eligible", False)),
            index_version=source.get("index_version"),
            embedding_model=source.get("embedding_model"),
            index_name=index_name,
            parent_block_id=source.get("parent_block_id"),
        )
