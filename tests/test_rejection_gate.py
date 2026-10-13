"""拒答门禁（相关性准入）回归测试。

背景
----
2026-10-12 的 120 条评测把「无关查询拒答准确率」量化为 **0.00**：五组消融
配置（``gate=top_k``）对 30 条无关查询全部硬返回完整 top_k，从不拒答。这是
检索层唯一的 P0 生产级缺陷。

修复思路（09-24 spec）是把门禁显式化为 ``GatePolicy``，并新增**候选级**余弦
门禁 ``COSINE_ABSOLUTE``：只保留单路 ``vector_score >= cosine_threshold``
的向量候选，并要求词法候选与之取交集（纯关键词命中不得绕过门禁），再进入
RRF 融合。本文件即该修复的回归网。

为什么这些测试值钱
------------------
* 它们锁定「无关查询 → 空证据 + ``insufficient_evidence``」这条行为契约，
  而这条契约此前**从无自动化测试**（``tests/`` 目录整体丢失）；
* 门禁是纯函数式的：``extract_gate`` 读 ``vector_score >= threshold``，因此
  用固定候选列表即可确定性复现，无需 OpenSearch；
* 关键反例（词法绕门、向量路不可用时跳过门禁、校准身份不符时 fail-closed）
  都各有一条测试，避免「修好主路径、埋掉边界」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from medical_rag.observability.metrics import CalibrationArtifact
from medical_rag.retrieval.contracts import GatePolicy
from medical_rag.retrieval.service import (
    REASON_CALIBRATION_MISMATCH,
    REASON_INSUFFICIENT_EVIDENCE,
    REASON_RETRIEVAL_UNAVAILABLE,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYED_ARTIFACT = REPO_ROOT / "config" / "calibration" / "cosine-gate-2026-09-26.json"


# ---------------------------------------------------------------------------
# 1. 门禁策略解析：从配置到 GatePolicy 的映射必须显式且唯一
# ---------------------------------------------------------------------------


def test_gate_policy_is_cosine_absolute_when_artifact_enables_cosine_gate(
    hybrid_index, cosine_artifact, make_service, embedding_model_name
):
    """带 ``cosine_threshold`` 的校准件必须解析为 COSINE_ABSOLUTE 策略。"""

    service = make_service(
        hybrid_index(),
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    assert service.gate_policy is GatePolicy.COSINE_ABSOLUTE
    assert service.cosine_threshold == pytest.approx(0.50)
    assert service.score_space == "l2_inv"


def test_gate_policy_is_top_k_without_any_gate_configuration(
    hybrid_index, make_service
):
    """未配置任何门禁时**不存在硬门禁**，入选范围只由 top_k 决定。"""

    service = make_service(hybrid_index())

    assert service.gate_policy is GatePolicy.TOP_K
    assert service.cosine_threshold is None
    assert service.fused_score_threshold is None


def test_explicit_cosine_threshold_selects_cosine_absolute(
    hybrid_index, make_service
):
    service = make_service(
        hybrid_index(), cosine_threshold=0.48, score_space="cosinesimil"
    )

    assert service.gate_policy is GatePolicy.COSINE_ABSOLUTE
    assert service.score_space == "cosinesimil"


def test_fused_artifact_without_cosine_gate_selects_calibrated(
    hybrid_index, fused_artifact, make_service, embedding_model_name
):
    service = make_service(
        hybrid_index(),
        calibration_artifact=fused_artifact,
        embedding_model_name=embedding_model_name,
    )

    assert service.gate_policy is GatePolicy.CALIBRATED


# ---------------------------------------------------------------------------
# 2. 构造期 fail-fast：语义不可判定的组合一律拒绝
# ---------------------------------------------------------------------------


def test_cosine_threshold_without_score_space_is_rejected(hybrid_index):
    """阈值缺分数空间 = 不知道单位（0.5 在 l2_inv/cosinesimil 恰好同值，其他值不同）。"""

    from medical_rag.retrieval.service import RetrievalService

    with pytest.raises(ValueError):
        RetrievalService(hybrid_index(), cosine_threshold=0.5)


def test_cosine_threshold_combined_with_rank_gate_is_rejected(hybrid_index):
    from medical_rag.retrieval.service import RetrievalService

    with pytest.raises(ValueError):
        RetrievalService(
            hybrid_index(),
            cosine_threshold=0.5,
            score_space="cosinesimil",
            rank_gate=3,
        )


def test_unknown_score_space_is_rejected(hybrid_index):
    from medical_rag.retrieval.service import RetrievalService

    with pytest.raises(ValueError):
        RetrievalService(hybrid_index(), cosine_threshold=0.5, score_space="euclidean")


def test_calibration_artifact_embedding_model_mismatch_is_rejected(
    hybrid_index, cosine_artifact
):
    from medical_rag.retrieval.service import RetrievalService

    with pytest.raises(ValueError):
        RetrievalService(
            hybrid_index(),
            calibration_artifact=cosine_artifact,
            embedding_model_name="text-embedding-3-small",
        )


def test_calibration_artifact_rrf_mismatch_is_rejected(
    hybrid_index, cosine_artifact, embedding_model_name
):
    from medical_rag.retrieval.ranking import HybridRanker
    from medical_rag.retrieval.service import RetrievalService

    with pytest.raises(ValueError):
        RetrievalService(
            hybrid_index(),
            calibration_artifact=cosine_artifact,
            embedding_model_name=embedding_model_name,
            ranker=HybridRanker(rrf_k=30),
        )


# ---------------------------------------------------------------------------
# 3. ⭐ 核心缺陷回归：无关查询必须拒答
# ---------------------------------------------------------------------------


def test_unrelated_query_is_refused_by_cosine_gate(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    """所有向量候选低于阈值 → 证据集为空，reason 为 insufficient_evidence。

    这正是修复前恒为「返回完整 top_k」的那条路径。
    """

    index = hybrid_index(
        lexical=[make_record("lex-1", vector_score=0.10, bm25_score=9.9)],
        vector=[
            make_record("vec-1", vector_score=0.20),
            make_record("vec-2", vector_score=0.31),
        ],
    )
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert result.items == []
    assert result.reason == REASON_INSUFFICIENT_EVIDENCE
    assert result.diagnostics["gate_policy"] == GatePolicy.COSINE_ABSOLUTE.value
    assert result.diagnostics["vector_candidates_gated_out"] == 2
    assert result.diagnostics["vector_gated_candidates"] == 0


def test_control_disabling_the_gate_would_return_unrelated_candidates(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    """变异对照：同一批无关候选，门禁一旦失效就全部返回。

    这条测试用来证明上面那条拒答测试**不是空转**：候选池非空、夹具有效，
    拒答确实由门禁生效所致（对应修复前 ``gate=top_k`` 的真实行为）。
    """

    index = hybrid_index(
        lexical=[make_record("lex-1", vector_score=0.10)],
        vector=[
            make_record("vec-1", vector_score=0.20),
            make_record("vec-2", vector_score=0.31),
        ],
    )
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )
    assert service.gate_policy is GatePolicy.COSINE_ABSOLUTE

    # 模拟修复前行为：门禁失效（TOP_K = 无硬门禁）。
    service.gate_policy = GatePolicy.TOP_K

    result = service.retrieve(make_query(), context)

    assert {item.id for item in result.items} == {"lex-1", "vec-1", "vec-2"}
    assert result.reason is None


def test_relevant_candidate_survives_the_gate(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    index = hybrid_index(
        vector=[make_record("vec-1", vector_score=0.62)],
    )
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert [item.id for item in result.items] == ["vec-1"]
    assert result.reason is None
    assert result.diagnostics["status"] == "ok"


def test_keyword_only_hit_cannot_bypass_the_cosine_gate(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    """词法强命中但向量不相关的文档必须被剔除（09-24 §4.2）。

    只过滤向量路会让「纯关键词命中」的不相关文档绕过门禁——这恰是医疗场景
    最危险的一类幻觉来源（术语撞词但语义无关）。
    """

    index = hybrid_index(
        lexical=[
            make_record("lex-only", vector_score=0.05, bm25_score=12.0),
            make_record("vec-1", vector_score=0.62),
        ],
        vector=[make_record("vec-1", vector_score=0.62)],
    )
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    returned = [item.id for item in result.items]
    assert returned == ["vec-1"]
    assert "lex-only" not in returned
    assert result.diagnostics["lexical_gated_out"] == 1


def test_threshold_boundary_is_inclusive(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    """``vector_score == threshold`` 视为通过（>= 语义），且只有它一条。"""

    index = hybrid_index(
        vector=[
            make_record("at-threshold", vector_score=0.50),
            make_record("below", vector_score=0.4999),
        ],
    )
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert [item.id for item in result.items] == ["at-threshold"]


# ---------------------------------------------------------------------------
# 4. 降级：门禁不得把「向量路不可用」放大成「永远拒答」
# ---------------------------------------------------------------------------


def test_gate_is_skipped_when_query_has_no_embedding(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    """无查询向量 → 向量路未发起 → 跳过门禁并照常返回词法结果。

    否则每次 embedding 抖动都会被放大成「全部拒答」，把可降级故障变成完全
    不可用。代价（该状态下恢复「永不拒答」）由 ``gate_skipped_reason`` 显式
    标记，供监控告警。
    """

    index = hybrid_index(lexical=[make_record("lex-1", vector_score=0.05)])
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(embedding=None), context)

    assert [item.id for item in result.items] == ["lex-1"]
    assert result.diagnostics["gate_skipped_reason"] == "vector_path_unavailable"


def test_gate_is_skipped_when_vector_backend_errors(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    index = hybrid_index(
        lexical=[make_record("lex-1", vector_score=0.05)], vector_error=True
    )
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert [item.id for item in result.items] == ["lex-1"]
    assert result.diagnostics["gate_skipped_reason"] == "vector_path_unavailable"
    assert result.diagnostics["vector_error"] is True


def test_calibrated_artifact_fails_closed_on_legacy_single_call_index(
    make_query, single_call_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    """有校准件但无双路接缝：走**校准身份**分支降级（先于门禁分支判定）。"""

    service = make_service(
        single_call_index(),
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert result.items == []
    assert result.reason == REASON_CALIBRATION_MISMATCH
    assert result.diagnostics["calibration_artifact_mismatch"] is True
    assert (
        result.diagnostics["calibration_artifact_reason"]
        == "hybrid_candidate_seams_required"
    )


def test_explicit_cosine_gate_fails_closed_on_legacy_single_call_index(
    make_query, single_call_index, make_service, context,
):
    """无校准件但显式配了余弦门禁：走**门禁**分支降级，绝不静默退回 top_k。"""

    service = make_service(
        single_call_index(), cosine_threshold=0.5, score_space="l2_inv"
    )

    result = service.retrieve(make_query(), context)

    assert result.items == []
    assert result.reason == REASON_CALIBRATION_MISMATCH
    assert (
        result.diagnostics["gate_skipped_reason"] == "hybrid_candidate_seams_required"
    )


# ---------------------------------------------------------------------------
# 5. 校准身份：内容与索引版本不符即 fail-closed
# ---------------------------------------------------------------------------


def test_calibration_version_mismatch_fails_closed(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    index = hybrid_index(
        vector=[make_record("vec-1", vector_score=0.62, index_version="public-v2")]
    )
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert result.items == []
    assert result.reason == REASON_CALIBRATION_MISMATCH
    assert result.diagnostics["calibration_artifact_mismatch"] is True
    assert result.diagnostics["calibration_mismatch_count"] == 1
    # 诊断面只报身份标识，绝不泄漏正文（PHI 不变量 I8）。
    detail = result.diagnostics["calibration_mismatch_records"][0]
    assert "text" not in detail
    assert set(detail) == {"id", "visibility", "index_version", "embedding_model"}


def test_embedding_model_mismatch_fails_closed(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    index = hybrid_index(
        vector=[
            make_record("vec-1", vector_score=0.62, embedding_model="some-other-model")
        ]
    )
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert result.reason == REASON_CALIBRATION_MISMATCH
    assert result.diagnostics["calibration_mismatch_count"] == 1


# ---------------------------------------------------------------------------
# 6. 可观测性：vector_score 语义与故障类型必须能区分
# ---------------------------------------------------------------------------


def test_vector_score_space_is_surfaced_before_the_gate(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    """语义标注必须反映**原始**候选（门禁前的 l2_inv），否则消费方无从校验。"""

    index = hybrid_index(vector=[make_record("vec-1", vector_score=0.20)])
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert result.diagnostics["vector_score_space"] == "l2_inv"


def test_vector_score_space_is_unknown_for_semantics_free_records(
    make_record, make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    index = hybrid_index(
        vector=[make_record("vec-1", vector_score=0.62, vector_score_space=None)]
    )
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert result.diagnostics["vector_score_space"] == "unknown"


def test_score_space_mismatch_between_mapping_and_gate_fails_closed(
    make_record,
    make_query,
    hybrid_index,
    cosine_artifact,
    make_service,
    embedding_model_name,
    context,
    monkeypatch,
):
    index = hybrid_index(vector=[make_record("vec-1", vector_score=0.62)])
    monkeypatch.setattr(index, "resolve_vector_score_space", lambda: "cosinesimil", raising=False)
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert result.items == []
    assert result.reason == REASON_CALIBRATION_MISMATCH
    assert result.diagnostics["mapping_score_space"] == "cosinesimil"
    assert result.diagnostics["score_space_match"] is False


def test_mapping_unavailable_fails_closed_for_strict_gate(
    make_record,
    make_query,
    hybrid_index,
    cosine_artifact,
    make_service,
    embedding_model_name,
    context,
    monkeypatch,
):
    index = hybrid_index(vector=[make_record("vec-1", vector_score=0.62)])
    monkeypatch.setattr(index, "resolve_vector_score_space", lambda: "mapping_unavailable", raising=False)
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert result.items == []
    assert result.reason == REASON_CALIBRATION_MISMATCH
    assert result.diagnostics["mapping_score_space"] == "mapping_unavailable"
    assert result.diagnostics["score_space_match"] is False


def test_retrieval_unavailable_is_distinct_from_insufficient_evidence(
    make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    """两路都报错 = 服务不可用，不能呈现成「没有足够医学证据」。"""

    index = hybrid_index(lexical_error=True, vector_error=True)
    service = make_service(
        index,
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert result.items == []
    assert result.reason == REASON_RETRIEVAL_UNAVAILABLE


def test_empty_candidate_pool_reports_insufficient_evidence(
    make_query, hybrid_index, cosine_artifact, make_service,
    embedding_model_name, context,
):
    service = make_service(
        hybrid_index(),
        calibration_artifact=cosine_artifact,
        embedding_model_name=embedding_model_name,
    )

    result = service.retrieve(make_query(), context)

    assert result.reason == REASON_INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# 7. 校准件本身的字段不变量
# ---------------------------------------------------------------------------


def _artifact(**overrides: object) -> CalibrationArtifact:
    base: dict[str, object] = {
        "index_version": "artifact-2026-09-26",
        "embedding_model": "BAAI/bge-small-zh-v1.5",
        "rrf_k": 60,
        "threshold": 0.42,
        "eval_dataset_version": "cosine-gate-2026-09-26",
    }
    base.update(overrides)
    return CalibrationArtifact(**base)  # type: ignore[arg-type]


def test_artifact_rejects_cosine_threshold_without_score_space():
    with pytest.raises(ValueError):
        _artifact(cosine_threshold=0.5)


def test_artifact_rejects_score_space_without_cosine_threshold():
    with pytest.raises(ValueError):
        _artifact(score_space="l2_inv")


def test_artifact_rejects_unknown_score_space():
    with pytest.raises(ValueError):
        _artifact(cosine_threshold=0.5, score_space="euclidean")


def test_artifact_rejects_unpaired_visibility_index_versions():
    with pytest.raises(ValueError):
        _artifact(public_index_version="public-v1")


def test_artifact_rejects_out_of_range_threshold():
    with pytest.raises(ValueError):
        _artifact(threshold=1.5)


def test_artifact_uses_cosine_gate_flag():
    assert _artifact(cosine_threshold=0.5, score_space="l2_inv").uses_cosine_gate is True
    assert _artifact().uses_cosine_gate is False


def test_artifact_expected_index_version_falls_back_and_fails_closed():
    legacy = _artifact()
    assert legacy.expected_index_version_for("public") == "artifact-2026-09-26"

    paired = _artifact(
        public_index_version="public-v1", private_index_version="private-v1"
    )
    assert paired.expected_index_version_for("public") == "public-v1"
    assert paired.expected_index_version_for("private") == "private-v1"
    # 未知可见性 fail-closed：返回 None 让调用方判定为「破坏标定」。
    assert paired.expected_index_version_for("unknown") is None


# ---------------------------------------------------------------------------
# 8. 部署件解析（承重事实：线上门禁配置必须能被代码读入）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not DEPLOYED_ARTIFACT.exists(), reason="部署校准件不在仓库中"
)
def test_deployed_calibration_artifact_parses_and_enables_cosine_gate():
    payload = json.loads(DEPLOYED_ARTIFACT.read_text(encoding="utf-8"))

    artifact = CalibrationArtifact(**payload)

    assert artifact.uses_cosine_gate is True
    assert artifact.score_space in {"l2_inv", "cosinesimil"}
    assert artifact.embedding_model == "BAAI/bge-small-zh-v1.5"
    assert 0.0 <= artifact.cosine_threshold <= 1.0
