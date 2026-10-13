from __future__ import annotations

from typing import Any

from medical_rag.retrieval.opensearch_store import OpenSearchStore


class _MappingIndices:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self.mapping = mapping
        self.requested: list[str] = []

    def get_mapping(self, *, index: str) -> dict[str, Any]:
        self.requested.append(index)
        return self.mapping


class _MappingClient:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self.indices = _MappingIndices(mapping)


def _mapping(space_type: str | None) -> dict[str, Any]:
    embedding: dict[str, Any] = {"type": "knn_vector", "dimension": 3}
    if space_type is not None:
        embedding["method"] = {"space_type": space_type}
    return {"medical-rag-public": {"mappings": {"properties": {"embedding": embedding}}}}


def test_resolve_vector_score_space_reads_cosinesimil_mapping_without_source_text() -> None:
    client = _MappingClient(_mapping("cosinesimil"))
    store = OpenSearchStore(client, public_index="medical-rag-public")

    assert store.resolve_vector_score_space() == "cosinesimil"
    assert client.indices.requested == ["medical-rag-public", "medical-rag-private"]


def test_explicit_score_space_is_checked_against_physical_mapping() -> None:
    store = OpenSearchStore(
        _MappingClient(_mapping("l2_inv")),
        vector_score_space="cosinesimil",
    )

    assert store.resolve_vector_score_space() == "mapping_unavailable"


def test_resolve_vector_score_space_fails_closed_when_public_and_private_diverge() -> None:
    class _DivergentIndices:
        def get_mapping(self, *, index: str) -> dict[str, Any]:
            return _mapping("cosinesimil" if index.endswith("public") else "l2_inv")

    class _DivergentClient:
        indices = _DivergentIndices()

    store = OpenSearchStore(_DivergentClient())

    assert store.resolve_vector_score_space() == "mapping_unavailable"


def test_resolve_vector_score_space_fails_closed_when_mapping_method_is_missing() -> None:
    store = OpenSearchStore(_MappingClient(_mapping(None)))

    assert store.resolve_vector_score_space() == "mapping_unavailable"


def test_vector_records_carry_resolved_mapping_score_space() -> None:
    store = OpenSearchStore(_MappingClient(_mapping("cosinesimil")))

    record = store._record_from_hit(  # noqa: SLF001 - seam under test
        {
            "_id": "chunk-1",
            "_index": "medical-rag-public",
            "_score": 0.81,
            "_source": {"visibility": "public", "retrieval_eligible": True},
        },
        score_mode="vector",
        allowed_indices=["medical-rag-public"],
        require_index=True,
    )

    assert record is not None
    assert record.vector_score_space == "cosinesimil"
