"""启动期门禁自检（T4）回归测试。

背景
----
拒答门禁有三条可能失效的链路，前两条由 ``test_rejection_gate.py`` 覆盖：
1. 门禁代码本身失效（``GatePolicy`` 解析错误）；
2. 校准件与索引身份不符（fail-closed）。

还有第三条，也是最隐蔽的一条：**配置了 ``CALIBRATION_ARTIFACT_PATH``，但
装配失败被非生产环境的 ``log-and-continue`` 分支吞掉，实例照常启动并静默
退回 ``TOP_K``（永不拒答）**。``api/app.py`` 的 ``_assert_configured_gate_is_active``
必须在 lifespan 的 ``try/except`` **之外**执行才能拦住它。

本文件锁定该自检的三档拦截与「未配置即放行」的不变量。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from medical_rag.api.app import _assert_configured_gate_is_active
from medical_rag.core.config import Settings
from medical_rag.observability.metrics import CalibrationArtifact
from medical_rag.retrieval.contracts import GatePolicy
from medical_rag.retrieval.service import RetrievalService

CALIBRATION_PATH = "config/calibration/cosine-gate-2026-09-26.json"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"


def _settings(calibration_artifact_path: str | None) -> Settings:
    """构造不读 ``.env`` 的 Settings，保证测试与开发机配置无关。"""

    return Settings(_env_file=None, calibration_artifact_path=calibration_artifact_path)


def _app(orchestrator: object | None):
    return SimpleNamespace(state=SimpleNamespace(chat_orchestrator=orchestrator))


def _cosine_artifact() -> CalibrationArtifact:
    return CalibrationArtifact(
        index_version="artifact-2026-09-26",
        embedding_model=EMBEDDING_MODEL,
        rrf_k=60,
        threshold=0.42,
        eval_dataset_version="cosine-gate-2026-09-26",
        cosine_threshold=0.50,
        score_space="l2_inv",
        public_index_version="public-v1",
        private_index_version="private-v1",
    )


def _fused_artifact() -> CalibrationArtifact:
    return CalibrationArtifact(
        index_version="artifact-2026-09-26",
        embedding_model=EMBEDDING_MODEL,
        rrf_k=60,
        threshold=0.42,
        eval_dataset_version="cosine-gate-2026-09-26",
    )


def test_unconfigured_path_skips_the_check_entirely():
    """未配置路径 = TOP_K 是预期状态：不得因为「编排器没装好」就拒绝启动。"""

    _assert_configured_gate_is_active(_app(None), _settings(None))


def test_configured_path_without_assembled_orchestrator_refuses_to_start():
    """配了路径却没走到装配（装配异常被吞）——正是最该拦的情形。"""

    with pytest.raises(RuntimeError, match="未装配"):
        _assert_configured_gate_is_active(_app(None), _settings(CALIBRATION_PATH))


def test_configured_path_with_retriever_missing_artifact_refuses_to_start():
    orchestrator = SimpleNamespace(retriever=SimpleNamespace(calibration_artifact=None))

    with pytest.raises(RuntimeError, match="未持有标定产物"):
        _assert_configured_gate_is_active(
            _app(orchestrator), _settings(CALIBRATION_PATH)
        )


def test_configured_path_with_non_cosine_gate_refuses_to_start():
    """融合分数阈值门禁（非余弦绝对门禁）同样不放行。"""

    service = RetrievalService(
        SimpleNamespace(),
        calibration_artifact=_fused_artifact(),
        embedding_model_name=EMBEDDING_MODEL,
    )
    try:
        assert service.gate_policy is GatePolicy.CALIBRATED
        with pytest.raises(RuntimeError, match="COSINE_ABSOLUTE"):
            _assert_configured_gate_is_active(
                _app(SimpleNamespace(retriever=service)), _settings(CALIBRATION_PATH)
            )
    finally:
        service.close()


def test_configured_path_with_cosine_gate_starts_normally():
    """真实装配：正确 artifact → COSINE_ABSOLUTE → 自检放行。"""

    service = RetrievalService(
        SimpleNamespace(),
        calibration_artifact=_cosine_artifact(),
        embedding_model_name=EMBEDDING_MODEL,
    )
    try:
        assert service.gate_policy is GatePolicy.COSINE_ABSOLUTE
        _assert_configured_gate_is_active(
            _app(SimpleNamespace(retriever=service)), _settings(CALIBRATION_PATH)
        )
    finally:
        service.close()
