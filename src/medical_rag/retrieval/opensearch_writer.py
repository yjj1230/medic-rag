"""带显式公开/私有边界的生产 OpenSearch 写入适配器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from medical_rag.knowledge.contracts import IndexRecord, PublicIndexRecord
from medical_rag.retrieval.contracts import PrivateIndexRecord


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    attempted: int
    succeeded: int
    failed: int
    index_name: str
    alias: str


class OpenSearchWriteError(RuntimeError):
    def __init__(self, message: str, receipt: WriteReceipt | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# 分析器名会直接进入 mapping body，必须是纯标识符，避免拼进任意 JSON。
_SAFE_ANALYZER = re.compile(r"^[a-z][a-z0-9_]*$")

# 中文文本字段的分析器。
#
# 默认值使用 IK 中文分词插件（``ik_max_word`` 建索引 / ``ik_smart`` 查询）：
# OpenSearch 默认的 ``standard`` 分析器会把中文按**单字**切分，词级区分度丧失，
# BM25 对中文近乎失效，整个「dense + sparse」混合检索会塌陷为单路弱信号。
#
# ⚠️ 依赖：``ik_max_word``/``ik_smart`` 需要集群安装 analysis-ik 插件
# （docker-compose 里需为 opensearch 节点安装 ``opensearch-analysis-ik``）。
# 集群没有该插件时请在组装根显式回退：``text_analyzer="cjk"``
# （``cjk`` 是 OpenSearch **内置**分析器，对中文做二元切分，无需任何插件，
# 明显优于 ``standard`` 的单字切分），或 ``text_analyzer=None`` 明确接受
# OpenSearch 默认行为。回退必须是显式配置，不允许静默降级。
DEFAULT_TEXT_ANALYZER = "ik_max_word"
DEFAULT_SEARCH_ANALYZER = "ik_smart"


class OpenSearchIndexWriter:
    """针对某个物理 OpenSearch 索引的幂等批量写入器。

    该写入器绝不修改别名。公开与私有索引必须使用各自独立的实例；
    可见性是一条显式的安全边界。
    """

    def __init__(
        self,
        index_name: str,
        alias: str,
        client: Any,
        visibility: str | None = None,
        *,
        scope: str | None = None,
        embedding_dimension: int = 1536,
        text_analyzer: str | None = DEFAULT_TEXT_ANALYZER,
        search_analyzer: str | None = DEFAULT_SEARCH_ANALYZER,
    ) -> None:
        self._validate_name(index_name, "index_name")
        self._validate_name(alias, "alias")
        if index_name == alias:
            raise ValueError("physical index and alias must be different")
        if client is None:
            raise ValueError("OpenSearch client is required")
        if embedding_dimension <= 0:
            raise ValueError("embedding_dimension must be positive")
        if visibility is not None and scope is not None and visibility != scope:
            raise ValueError("visibility and scope must agree")
        resolved_visibility = visibility if visibility is not None else scope
        if resolved_visibility not in {"public", "private"}:
            raise ValueError("visibility must be public or private")
        self.index_name = index_name
        self.alias = alias
        self.client = client
        self.embedding_dimension = embedding_dimension
        self.scope = resolved_visibility
        self.visibility = resolved_visibility
        self.text_analyzer = self._validate_analyzer(text_analyzer, "analyzer")
        self.search_analyzer = self._validate_analyzer(search_analyzer, "search_analyzer")

    @staticmethod
    def _validate_analyzer(value: str | None, field: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not _SAFE_ANALYZER.fullmatch(value):
            raise ValueError(f"unsafe {field} name")
        return value

    def _text_field_mapping(self) -> dict[str, Any]:
        field: dict[str, Any] = {"type": "text"}
        if self.text_analyzer is not None:
            field["analyzer"] = self.text_analyzer
        if self.search_analyzer is not None:
            field["search_analyzer"] = self.search_analyzer
        return field

    @staticmethod
    def _validate_name(value: str, field: str) -> None:
        if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
            raise ValueError(f"unsafe {field}")

    @staticmethod
    def _normalize(value: IndexRecord | PublicIndexRecord | PrivateIndexRecord) -> IndexRecord:
        if isinstance(value, IndexRecord):
            return value
        if isinstance(value, (PublicIndexRecord, PrivateIndexRecord)):
            return value.to_index_record()
        raise TypeError("OpenSearch writer accepts normalized index records only")

    @staticmethod
    def _doc(record: IndexRecord) -> dict[str, Any]:
        payload = record.to_dict()
        if isinstance(payload.get("embedding"), tuple):
            payload["embedding"] = list(payload["embedding"])
        # OpenSearch 不需要一个纯 Python 字段，而公开文档
        # 不得包含患者 ACL 取值。
        if record.visibility == "public":
            payload.pop("tenant_id", None)
            payload.pop("patient_id", None)
        return payload

    def _validate_record(self, record: IndexRecord) -> None:
        if record.visibility != self.scope:
            raise ValueError(f"writer visibility must be {self.scope}")
        if self.scope == "public" and (record.tenant_id is not None or record.patient_id is not None):
            raise ValueError("public writer rejects patient ACL fields")
        if self.scope == "private" and (record.tenant_id is None or record.patient_id is None):
            raise ValueError("private writer requires ACL fields")

    def _existing_properties(self) -> dict[str, Any] | None:
        """返回物理索引当前的字段映射，索引不存在时返回 ``None``。"""
        indices = getattr(self.client, "indices", None)
        if indices is None or not indices.exists(index=self.index_name):
            return None
        mapping = indices.get_mapping(index=self.index_name)
        body = mapping.get(self.index_name) or next(iter(mapping.values()), {})
        properties = (body.get("mappings") or {}).get("properties")
        return properties if isinstance(properties, dict) else {}

    def _assert_mapping_compatible(self, existing: dict[str, Any]) -> None:
        """拒绝与既有索引不兼容的写入，并给出可执行的修复指令。

        ``knn_vector.dimension`` 与 ``text`` 的 ``analyzer`` 都是**建索引时固定、
        之后不可修改**的字段参数。原先的 ``ensure_mapping`` 直接调
        ``put_mapping``，由 OpenSearch 抛出
        ``Cannot update parameter [dimension] from [1536] to [512]``——这句话既不
        说明该改配置还是该重建索引，也不说明是哪个索引，切换向量模型时几乎必然
        踩到，且错误在深层调用栈里被包装成通用写入失败。

        前置检查把决策点提前：不兼容就立刻停，并说清现值、期望值与重建命令。
        这里**不自动删除重建**——对一个承载患者证据与医学知识库的索引，静默删除
        是不可接受的失败模式；重建必须是操作员的显式动作。
        """
        conflicts: list[str] = []
        embedding = existing.get("embedding") or {}
        current_dimension = embedding.get("dimension")
        if current_dimension is not None and int(current_dimension) != self.embedding_dimension:
            conflicts.append(
                f"向量维度 {current_dimension} -> {self.embedding_dimension}"
            )
        text_field = existing.get("text") or {}
        # 缺失 key 表示该字段在创建时没有指定 analyzer（即 OpenSearch 默认的
        # ``standard``，对中文按单字切分）。因此「无 key」与「显式 None」等价。
        current_analyzer = text_field.get("analyzer")
        if current_analyzer != self.text_analyzer:
            conflicts.append(
                f"文本分析器 {current_analyzer or 'default(standard)'} -> "
                f"{self.text_analyzer or 'default(standard)'}"
            )
        if not conflicts:
            return
        raise OpenSearchWriteError(
            f"索引 {self.index_name} 的既有映射与新配置不兼容（"
            + "；".join(conflicts)
            + "）。这些参数在索引创建后无法原地修改，必须删除并重建该索引："
            f"在执行重建前请确认索引内容可重新生成（公共知识库可由 knowledge-ingest "
            f"重建；私有索引需重新上传文档），随后删除 {self.index_name} 并重新运行 "
            f"init-indexes / knowledge-ingest。"
        )

    def ensure_mapping(self) -> None:
        indices = getattr(self.client, "indices", None)
        if indices is None:
            raise OpenSearchWriteError("OpenSearch client has no indices API")
        body = {
            "settings": {"index.knn": True},
            "mappings": {"properties": {
                "id": {"type": "keyword"},
                "text": self._text_field_mapping(),
                "visibility": {"type": "keyword"},
                "tenant_id": {"type": "keyword"},
                "patient_id": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "document_version_id": {"type": "keyword"},
                "evidence_unit_id": {"type": "keyword"},
                "source_id": {"type": "keyword"},
                "source_title": {"type": "text"},
                "source_version": {"type": "keyword"},
                "language": {"type": "keyword"},
                "location": {"type": "keyword"},
                "page_number": {"type": "integer"},
                "section": {"type": "keyword"},
                "sheet_name": {"type": "keyword"},
                "cell_range": {"type": "keyword"},
                "source_anchor": {"type": "keyword"},
                "locator": {
                    "type": "object",
                    "dynamic": "strict",
                    "properties": {
                        "locator_type": {"type": "keyword"},
                        "page_number": {"type": "integer"},
                        "page_total": {"type": "integer"},
                        "section_path": {"type": "keyword"},
                        "block_id": {"type": "keyword"},
                        "reading_order": {"type": "integer"},
                        "chunk_index": {"type": "integer"},
                        "char_start": {"type": "integer"},
                        "char_end": {"type": "integer"},
                        "bbox": {"type": "float"},
                        "sheet_name": {"type": "keyword"},
                        "cell_range": {"type": "keyword"},
                        "source_anchor": {"type": "keyword"},
                    },
                },
                "parent_block_id": {"type": "keyword"},
                # Task 5 溯源镜像：``retrieval/indexing.py`` 会写入
                # 这两个字段，因此必须在此声明 —— 未声明的
                # 字段会被动态映射为 ``text``，这会让对 chunk_index
                # 的序号排序 / 过滤变得不可靠。
                "chunk_index": {"type": "integer"},
                "section_path": {"type": "keyword"},
                "url": {"type": "keyword"},
                "confidence": {"type": "float"},
                "content_hash": {"type": "keyword"},
                "parser_version": {"type": "keyword"},
                "embedding_model": {"type": "keyword"},
                "embedding_version": {"type": "keyword"},
                "embedding_dimensions": {"type": "integer"},
                "index_version": {"type": "keyword"},
                "retrieval_eligible": {"type": "boolean"},
                "source_active": {"type": "boolean"},
                "deleted": {"type": "boolean"},
                "embedding": {"type": "knn_vector", "dimension": self.embedding_dimension},
                "created_at": {"type": "date"},
                "updated_at": {"type": "date"},
            }},
        }
        if not indices.exists(index=self.index_name):
            indices.create(index=self.index_name, body=body)
        else:
            existing = self._existing_properties() or {}
            self._assert_mapping_compatible(existing)
            indices.put_mapping(index=self.index_name, body=body["mappings"])

    def upsert(self, records: list[IndexRecord | PublicIndexRecord | PrivateIndexRecord]) -> WriteReceipt:
        normalized = [self._normalize(value) for value in records]
        for record in normalized:
            self._validate_record(record)
        receipt = WriteReceipt(len(normalized), 0, 0, self.index_name, self.alias)
        if not normalized:
            return receipt
        body: list[dict[str, Any]] = []
        for record in normalized:
            # ``doc_as_upsert`` 只是*更新体*参数：在 action 元数据里
            # 重复它会被 OpenSearch 拒绝，报
            # ``illegal_argument_exception: unknown parameter [doc_as_upsert]``
            # （已对照 OpenSearch 2.18 验证），因此它在下方只设置一次。
            body.append({"update": {
                "_index": self.index_name,
                "_id": record.id,
            }})
            body.append({"doc": self._doc(record), "doc_as_upsert": True})
        try:
            response = self.client.bulk(index=self.index_name, body=body)
        except Exception as exc:
            raise OpenSearchWriteError("OpenSearch bulk write failed", receipt) from exc
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise OpenSearchWriteError("OpenSearch bulk response was malformed", receipt)
        items = response["items"]
        failed = 0
        for item in items:
            action = next(iter(item.values()), {}) if isinstance(item, dict) and item else {}
            if not isinstance(action, dict):
                failed += 1
                continue
            try:
                status = int(action.get("status", 200))
            except (TypeError, ValueError):
                status = 500
            if status >= 300 or "error" in action:
                failed += 1
        if bool(response.get("errors")) or failed or len(items) != len(normalized):
            failed = max(failed, len(normalized) if not items else failed)
            final = WriteReceipt(receipt.attempted, receipt.attempted - failed, failed, self.index_name, self.alias)
            raise OpenSearchWriteError("OpenSearch bulk write rejected", final)
        return WriteReceipt(len(normalized), len(normalized), 0, self.index_name, self.alias)

    def delete_document(self, document_id: UUID) -> None:
        try:
            response = self.client.delete_by_query(
                index=self.index_name,
                body={"query": {"term": {"document_id": str(document_id)}}},
                conflicts="proceed",
            )
        except Exception as exc:
            raise OpenSearchWriteError("OpenSearch document deletion failed") from exc
        if isinstance(response, dict) and (response.get("timed_out") or response.get("failures")):
            raise OpenSearchWriteError("OpenSearch document deletion rejected")

    def delete_record(self, record_id: str | UUID) -> None:
        """按其索引 ID 幂等地移除一条确定性记录。"""

        try:
            response = self.client.delete(index=self.index_name, id=str(record_id), ignore=[404])
        except TypeError:
            response = self.client.delete(index=self.index_name, id=str(record_id))
        except Exception as exc:
            raise OpenSearchWriteError("OpenSearch record deletion failed") from exc
        if isinstance(response, dict) and response.get("timed_out"):
            raise OpenSearchWriteError("OpenSearch record deletion rejected")


__all__ = ["OpenSearchIndexWriter", "OpenSearchWriteError", "WriteReceipt"]
