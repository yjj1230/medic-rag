"""Two-level cosine gate behavior."""

from __future__ import annotations

from medical_rag.observability.metrics import CalibrationArtifact


def _artifact(cosine: float = 0.50, vector_only: float | None = 0.75) -> CalibrationArtifact:
    return CalibrationArtifact(
        index_version="artifact-2026-10-13",
        embedding_model="BAAI/bge-small-zh-v1.5",
        rrf_k=60,
        threshold=0.42,
        eval_dataset_version="two-level-gate-tests",
        cosine_threshold=cosine,
        score_space="l2_inv",
        vector_only_cosine_threshold=vector_only,
        public_index_version="public-v1",
        private_index_version="private-v1",
    )


def test_lexical_vector_intersection_uses_base_threshold(
    make_record, make_query, hybrid_index, make_service, embedding_model_name, context
):
    index = hybrid_index(
        lexical=[make_record("both", vector_score=0.60)],
        vector=[make_record("both", vector_score=0.60)],
    )
    service = make_service(
        index,
        calibration_artifact=_artifact(),
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert [item.id for item in result.items] == ["both"]
    assert result.diagnostics["lexical_vector_intersection"] == 1
    assert result.diagnostics["vector_only_candidates"] == 0


def test_vector_only_candidate_requires_stricter_threshold(
    make_record, make_query, hybrid_index, make_service, embedding_model_name, context
):
    index = hybrid_index(
        vector=[
            make_record("below-strict", vector_score=0.60),
            make_record("above-strict", vector_score=0.80),
        ]
    )
    service = make_service(
        index,
        calibration_artifact=_artifact(vector_only=0.75),
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert [item.id for item in result.items] == ["above-strict"]
    assert result.diagnostics["vector_only_candidates"] == 2
    assert result.diagnostics["vector_only_gated_out"] == 1
    assert result.diagnostics["vector_only_gate_threshold"] == 0.75
    assert result.diagnostics["vector_only_threshold_fallback"] is False


def test_lexical_only_candidate_is_removed(
    make_record, make_query, hybrid_index, make_service, embedding_model_name, context
):
    index = hybrid_index(
        lexical=[
            make_record("lexical-only", vector_score=0.99),
            make_record("both", vector_score=0.60),
        ],
        vector=[make_record("both", vector_score=0.60)],
    )
    service = make_service(
        index,
        calibration_artifact=_artifact(),
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert [item.id for item in result.items] == ["both"]
    assert result.diagnostics["lexical_vector_intersection"] == 1


def test_legacy_artifact_falls_back_to_base_threshold(
    make_record, make_query, hybrid_index, make_service, embedding_model_name, context
):
    index = hybrid_index(vector=[make_record("vector-only", vector_score=0.60)])
    service = make_service(
        index,
        calibration_artifact=_artifact(vector_only=None),
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert [item.id for item in result.items] == ["vector-only"]
    assert result.diagnostics["vector_only_gate_threshold"] == 0.50
    assert result.diagnostics["vector_only_threshold_fallback"] is True
