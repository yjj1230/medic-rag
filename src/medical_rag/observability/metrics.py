"""确定性检索评测与隐私安全指标。

评测辅助函数只操作标识符和元数据。尤其是，检索结果的正文绝不会被复制进
记录器，因此本模块用于患者数据诊断是安全的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

# 校准后的绝对阈值可能所处的分数空间。``l2_inv`` 是
# OpenSearch ``knn_vector`` 的默认行为（未设 ``space_type`` → ``l2``，
# ``_score`` = ``1 / (1 + L2²)``）；``cosinesimil`` 才是真余弦相似度。
# 两者仅在 0.5 处数值重合，因此一个阈值若不含
# 它所处的分数空间就没有意义（09-24 spec §2.4）。
_SCORE_SPACES = frozenset({"l2_inv", "cosinesimil"})

# 用于按索引分派校准的可见性取值。保持为普通
# 字符串，使本模块不依赖 ``retrieval.contracts``（后者
# 从本模块导入），从而不会引入循环导入。
_VISIBILITY_PUBLIC = "public"
_VISIBILITY_PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """一个合成、确定性的检索评测用例。"""

    id: str
    question: str
    scope: str
    tenant_id: str | None = None
    patient_id: str | None = None
    gold_evidence_ids: tuple[str, ...] = ()
    expected_status: str = "answered"
    forbidden_evidence_ids: tuple[str, ...] = ()
    deleted_evidence_ids: tuple[str, ...] = ()
    review_required_evidence_ids: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "EvaluationCase":
        """从已解码的 JSON 对象构建用例，并做严格的基础校验。"""
        case_id = str(data["id"])
        question = str(data["question"])
        if not question.strip():
            raise ValueError("evaluation question cannot be empty")
        return cls(
            id=case_id,
            question=question,
            scope=str(data.get("scope", "public")),
            tenant_id=None if data.get("tenant_id") is None else str(data["tenant_id"]),
            patient_id=None if data.get("patient_id") is None else str(data["patient_id"]),
            gold_evidence_ids=tuple(str(value) for value in data.get("gold_evidence_ids", ())),
            expected_status=str(data.get("expected_status", "answered")),
            forbidden_evidence_ids=tuple(
                str(value) for value in data.get("forbidden_evidence_ids", ())
            ),
            deleted_evidence_ids=tuple(
                str(value) for value in data.get("deleted_evidence_ids", ())
            ),
            review_required_evidence_ids=tuple(
                str(value) for value in data.get("review_required_evidence_ids", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    """单用例的检索质量与零容忍安全度量。"""

    hit_rate: float
    recall_at_k: float
    mrr: float
    permission_violations: int = 0
    deletion_residuals: int = 0
    review_required_leakage: int = 0
    # 本次评测实际使用的有序列表截断点（见
    # ``evaluate_retrieval``）；超出它的命中不计入。
    k: int = 0
    # 二元相关性 nDCG@k：度量的是*排序*质量，这是召回率无法反映的。
    ndcg_at_k: float = 0.0


def _ndcg_at_k(ranked: Sequence[str], gold: set[str]) -> float:
    """基于有序证据 id 列表的二元相关性 nDCG@k。

    重复的 id 至多计入一次：把同一份相关文档计两次会让检索器通过重复命中
    来抬高自己的分数。
    """
    if not gold or not ranked:
        return 0.0
    seen: set[str] = set()
    dcg = 0.0
    for rank, value in enumerate(ranked, 1):
        if value in gold and value not in seen:
            seen.add(value)
            dcg += 1.0 / math.log2(rank + 1)
    ideal_hits = min(len(ranked), len(gold))
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    if idcg == 0:
        return 0.0
    return min(1.0, dcg / idcg)


def evaluate_retrieval(
    case: EvaluationCase, evidence_ids: Iterable[str], *, k: int | None = None
) -> RetrievalMetrics:
    """对某个用例的有序证据 ID 做评测。

    当返回列表中存在至少一个 gold 项时 ``hit_rate`` 为 1。对于无证据用例，
    当没有返回证据时为 1。同一约定使空 gold 用例可被用作显式的拒答测试。

    ``k`` 是有序列表的截断点。它默认等于所提供的 id 数量（旧有行为，因此既有
    调用方的数值保持不变）。超过截断点的命中被忽略，且 ``recall_at_k`` 除以
    ``min(k, |gold|)``，从而使在截断点内找齐每一个可达相关项即为满分，而不是
    一个分数值。
    """
    returned = tuple(str(value) for value in evidence_ids)
    cutoff = len(returned) if k is None else max(0, int(k))
    top = returned[:cutoff]
    gold = set(case.gold_evidence_ids)
    if gold:
        relevant = set(top) & gold
        hit = 1.0 if relevant else 0.0
        denominator = min(cutoff, len(gold))
        recall = len(relevant) / denominator if denominator else 0.0
        first_rank = next((rank for rank, value in enumerate(top, 1) if value in gold), None)
        mrr = 0.0 if first_rank is None else 1.0 / first_rank
        ndcg = _ndcg_at_k(top, gold)
    else:
        hit = recall = 1.0 if not top else 0.0
        mrr = hit
        ndcg = hit

    # 统计出现次数：重复的残留项/违规仍是各自独立的
    # 返回命中，不能被集合去重掩盖。
    forbidden = set(case.forbidden_evidence_ids)
    deleted = set(case.deleted_evidence_ids)
    review_required = set(case.review_required_evidence_ids)
    permission_violations = sum(value in forbidden for value in top)
    deletion_residuals = sum(value in deleted for value in top)
    review_required_leakage = sum(value in review_required for value in top)
    return RetrievalMetrics(
        hit_rate=hit,
        recall_at_k=recall,
        mrr=mrr,
        permission_violations=permission_violations,
        deletion_residuals=deletion_residuals,
        review_required_leakage=review_required_leakage,
        k=cutoff,
        ndcg_at_k=ndcg,
    )


@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    """绑定到某一次物理索引构建的不可变校准元数据。

    该工件有意只携带标识符与门禁参数；评测证据仍是一份独立、隐私安全的指标载荷。

    ``embedding_model`` 必须与已配置的 ``EMBEDDING_MODEL`` 完全相等。
    ``RetrievalService`` 会把它同时与查询侧模型名和每条返回记录做比较，因此像
    ``text-embedding-3-small@2026-01`` 这样带版本固定的拼写永远无法匹配，会让
    严格门禁路径不可用。本 docstring 此前恰好推荐了那种拼写，
    与该检查自相矛盾。
    """

    index_version: str
    embedding_model: str
    rrf_k: int
    # 语义不变：RRF 归一化后的融合分数阈值。下方的
    # 余弦门禁是一个*独立*字段，正是为了让已部署工件里
    # 既有的 ``threshold=0.42`` 不会从"RRF
    # 0.42"悄悄变成"向量分数 0.42"的含义（09-24 spec §5.2）。
    threshold: float
    eval_dataset_version: str
    # --- 余弦相关性门禁（09-24 / 09-28 spec，可选；同时存在即启用）---------
    # 阈值所处的分数空间（``l2_inv`` / ``cosinesimil``）。与阈值成对出现：
    # 缺一即拒，杜绝「有阈值却不知道其单位」。
    cosine_threshold: float | None = None
    score_space: str | None = None
    # 按可见性分派的索引版本（09-28 spec）。成对出现；均缺省时回退到全局
    # ``index_version``，因此旧 artifact 行为逐字节不变。
    public_index_version: str | None = None
    private_index_version: str | None = None
    # Optional stricter threshold for candidates returned only by the vector
    # path.  When omitted, the base cosine threshold applies to those rows too.
    vector_only_cosine_threshold: float | None = None

    def __post_init__(self) -> None:
        for name in ("index_version", "embedding_model", "eval_dataset_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.rrf_k, bool) or not isinstance(self.rrf_k, int) or self.rrf_k < 1:
            raise ValueError("rrf_k must be a positive integer")
        if not isinstance(self.threshold, (int, float)) or isinstance(self.threshold, bool):
            raise ValueError("threshold must be numeric")
        if not 0.0 <= float(self.threshold) <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        # 余弦阈值与其分数空间必须成对：单给其一都会让门禁的语义不可判定。
        if self.cosine_threshold is not None:
            if isinstance(self.cosine_threshold, bool) or not isinstance(
                self.cosine_threshold, (int, float)
            ):
                raise ValueError("cosine_threshold must be numeric")
            if not 0.0 <= float(self.cosine_threshold) <= 1.0:
                raise ValueError("cosine_threshold must be between 0 and 1")
            if self.score_space is None:
                raise ValueError(
                    "cosine_threshold requires score_space "
                    "(a threshold without its numeric space is meaningless)"
                )
        if self.vector_only_cosine_threshold is not None:
            if isinstance(self.vector_only_cosine_threshold, bool) or not isinstance(
                self.vector_only_cosine_threshold, (int, float)
            ):
                raise ValueError("vector_only_cosine_threshold must be numeric")
            if not 0.0 <= float(self.vector_only_cosine_threshold) <= 1.0:
                raise ValueError("vector_only_cosine_threshold must be between 0 and 1")
            if self.cosine_threshold is None:
                raise ValueError(
                    "vector_only_cosine_threshold requires cosine_threshold"
                )
        if self.score_space is not None:
            if self.score_space not in _SCORE_SPACES:
                raise ValueError(f"score_space must be one of {sorted(_SCORE_SPACES)}")
            if self.cosine_threshold is None:
                raise ValueError("score_space requires cosine_threshold")
        for name in ("public_index_version", "private_index_version"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string when provided")
        if (self.public_index_version is None) != (self.private_index_version is None):
            raise ValueError(
                "public_index_version and private_index_version must be provided together"
            )

    @property
    def uses_cosine_gate(self) -> bool:
        """当该工件启用候选级余弦门禁时为 True。"""

        return self.cosine_threshold is not None

    def expected_index_version_for(self, visibility: str) -> str | None:
        """``visibility`` 的记录必须匹配的索引版本。

        遗留工件（无按索引版本）回落到唯一的全局
        ``index_version``，因此其行为不变。未知的 visibility 取值返回 ``None``，
        使调用方 fail closed，而不是静默跳过溯源检查。
        """

        if self.public_index_version is None and self.private_index_version is None:
            return self.index_version
        if visibility == _VISIBILITY_PUBLIC:
            return self.public_index_version
        if visibility == _VISIBILITY_PRIVATE:
            return self.private_index_version
        return None


@dataclass(frozen=True, slots=True)
class RolloutDecision:
    """:class:`RolloutGate` 返回的有界、无副作用决策。"""

    decision: str
    reasons: tuple[str, ...] = ()
    baseline_metrics: Mapping[str, int | float] = field(default_factory=dict)
    candidate_metrics: Mapping[str, int | float] = field(default_factory=dict)
    artifact_match: bool = False

    @property
    def action(self) -> str:
        """为把决策称为动作的调用方提供的兼容别名。"""

        return self.decision

    @property
    def status(self) -> str:
        return self.decision

    @property
    def promote(self) -> bool:
        return self.decision == "promote"

    @property
    def should_promote(self) -> bool:
        return self.promote

    @property
    def reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None

    @property
    def metrics(self) -> Mapping[str, int | float]:
        return self.candidate_metrics


class RolloutGate:
    """把已评测的候选与校准基线做比较。

    输入可以是 ``{"artifact": artifact, "metrics": metrics}`` 映射、
    暴露这两个属性的对象，或 ``(artifact, metrics)`` 元组。门禁不返回别名/控制器
    操作；在本决策经复核后，晋级是一次显式的运维操作。
    """

    _SECURITY_KEYS = (
        "permission_violations",
        "deletion_residuals",
        "review_required_leakage",
    )
    _QUALITY_KEYS = ("hit_rate", "recall_at_k", "mrr")

    @staticmethod
    def compare(baseline: Any, candidate: Any) -> RolloutDecision:
        gate = RolloutGate()
        baseline_artifact, baseline_metrics = gate._coerce_snapshot(baseline)
        candidate_artifact, candidate_metrics = gate._coerce_snapshot(candidate)
        reasons: list[str] = []
        artifact_match = (
            baseline_artifact is not None
            and candidate_artifact is not None
            and baseline_artifact == candidate_artifact
        )
        if not artifact_match:
            reasons.append("calibration_artifact_mismatch")

        candidate_security = gate._security_failures(candidate_metrics)
        if candidate_security:
            reasons.extend(candidate_security)

        missing_quality = [key for key in gate._QUALITY_KEYS if key not in candidate_metrics]
        if missing_quality:
            reasons.append("candidate_quality_metrics_missing")
        else:
            for key in gate._QUALITY_KEYS:
                baseline_value = baseline_metrics.get(key)
                candidate_value = candidate_metrics.get(key)
                if not isinstance(baseline_value, (int, float)) or not isinstance(
                    candidate_value, (int, float)
                ):
                    reasons.append(f"{key}_metric_invalid")
                elif float(candidate_value) < float(baseline_value):
                    reasons.append(f"{key}_regression")

        if reasons and candidate_security:
            decision = "rollback"
        elif reasons:
            decision = "hold"
        else:
            decision = "promote"
        return RolloutDecision(
            decision=decision,
            reasons=tuple(dict.fromkeys(reasons))[:8],
            baseline_metrics=gate._bounded_metrics(baseline_metrics),
            candidate_metrics=gate._bounded_metrics(candidate_metrics),
            artifact_match=artifact_match,
        )

    @staticmethod
    def _coerce_snapshot(value: Any) -> tuple[CalibrationArtifact | None, Mapping[str, Any]]:
        artifact: Any = None
        metrics: Any = {}
        if isinstance(value, CalibrationArtifact):
            artifact = value
        elif isinstance(value, Mapping):
            artifact = value.get("artifact", value.get("calibration_artifact"))
            metrics = value.get("metrics", value.get("retrieval_metrics", {}))
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            artifact, metrics = value
        else:
            artifact = getattr(value, "artifact", getattr(value, "calibration_artifact", None))
            metrics = getattr(value, "metrics", getattr(value, "retrieval_metrics", {}))
        if isinstance(metrics, RetrievalMetrics):
            metrics = {
                key: getattr(metrics, key)
                for key in (
                    "hit_rate",
                    "recall_at_k",
                    "mrr",
                    "ndcg_at_k",
                    "permission_violations",
                    "deletion_residuals",
                    "review_required_leakage",
                    "k",
                )
            }
        elif isinstance(metrics, Iterable) and not isinstance(metrics, (str, bytes, Mapping)):
            values = [item for item in metrics if isinstance(item, RetrievalMetrics)]
            if values:
                metrics = {
                    "hit_rate": sum(item.hit_rate for item in values) / len(values),
                    "recall_at_k": sum(item.recall_at_k for item in values) / len(values),
                    "mrr": sum(item.mrr for item in values) / len(values),
                    "ndcg_at_k": sum(item.ndcg_at_k for item in values) / len(values),
                    "permission_violations": sum(item.permission_violations for item in values),
                    "deletion_residuals": sum(item.deletion_residuals for item in values),
                    "review_required_leakage": sum(
                        item.review_required_leakage for item in values
                    ),
                }
        if not isinstance(metrics, Mapping):
            metrics = {}
        if isinstance(artifact, Mapping):
            try:
                artifact = CalibrationArtifact(**dict(artifact))
            except (TypeError, ValueError):
                artifact = None
        if not isinstance(artifact, CalibrationArtifact):
            artifact = None
        return artifact, metrics

    @classmethod
    def _security_failures(cls, metrics: Mapping[str, Any]) -> list[str]:
        failures: list[str] = []
        aliases = {"review_leakage": "review_required_leakage"}
        for key in cls._SECURITY_KEYS:
            value = metrics.get(key, metrics.get(aliases.get(key, ""), 0))
            if isinstance(value, (int, float)) and value > 0:
                failures.append(f"{key}_nonzero")
            elif value not in (0, 0.0, None) and not isinstance(value, (int, float)):
                failures.append(f"{key}_invalid")
        return failures

    @staticmethod
    def _bounded_metrics(metrics: Mapping[str, Any]) -> dict[str, int | float]:
        return {
            str(key): value
            for key, value in metrics.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }


@dataclass(slots=True)
class MetricsRecorder:
    """适合测试与请求作用域的内存、隐私安全记录器。"""

    retrievals: list[dict[str, object]] = field(default_factory=list)
    reconciliations: list[dict[str, object]] = field(default_factory=list)

    def record_reconciliation(self, **metadata: object) -> None:
        """只记录有界的对账计数器/标识符。"""

        allowed = {
            "scope", "scanned", "residuals", "repaired", "failed", "reason",
            "item_id", "outcome", "action",
        }
        self.reconciliations.append(
            {str(key): value for key, value in metadata.items() if str(key) in allowed}
        )

    def reconciliation_snapshot(self) -> tuple[Mapping[str, object], ...]:
        return tuple(MappingProxyType(dict(item)) for item in self.reconciliations)

    def record_retrieval(self, result: Any) -> None:
        """只记录标识符、分数和白名单内的元数据。"""
        items = getattr(result, "items", ())
        reason = getattr(result, "reason", None)
        self.retrievals.append(
            {
                "evidence_ids": tuple(str(item.id) for item in items),
                "scores": tuple(float(item.score) for item in items),
                "reason": _safe_reason(reason),
                "diagnostics": _safe_diagnostics(getattr(result, "diagnostics", {})),
            }
        )

    def snapshot(self) -> tuple[Mapping[str, object], ...]:
        snapshots = []
        for entry in self.retrievals:
            frozen = dict(entry)
            if isinstance(frozen.get("diagnostics"), dict):
                frozen["diagnostics"] = MappingProxyType(dict(frozen["diagnostics"]))
            snapshots.append(MappingProxyType(frozen))
        return tuple(snapshots)


# 可以逐字记录的原因字符串。其他一律做哈希，因此
# 缺一个条目会把可诊断的失败（"retrieval_unavailable"）变成监控里
# 不透明的摘要。tests/unit/test_chat_route_and_metric_reasons.py
# 会在 retrieval.service 声明了此处缺失的原因时失败。
_SAFE_REASONS = {
    None,
    "insufficient_evidence",
    "unauthorized_scope",
    "retrieval_unavailable",
    "embedding_unavailable",
    "calibration_artifact_mismatch",
}
_SAFE_STATUSES = {"ok", "degraded", "error"}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _hash(value: object) -> str:
    return "sha256:" + sha256(str(value).encode("utf-8")).hexdigest()


def _safe_reason(reason: object) -> str | None:
    if reason in _SAFE_REASONS:
        return reason  # type: ignore[return-value]
    return None if reason is None else _hash(reason)


def _safe_identifier(value: object) -> str:
    text = str(value)
    return text if _SAFE_IDENTIFIER.fullmatch(text) else _hash(value)


def _safe_diagnostics(diagnostics: Mapping[str, object] | None) -> dict[str, object]:
    """只保留已知元数据；绝不保留任意诊断文本。"""
    safe: dict[str, object] = {}
    for key, value in (diagnostics or {}).items():
        key = str(key)
        if isinstance(value, (int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, str):
            if key == "reason":
                safe[key] = _safe_reason(value)
            elif key == "status":
                safe[key] = value if value in _SAFE_STATUSES else _hash(value)
            elif key in {"index_version", "embedding_model"}:
                safe[key] = _safe_identifier(value)
            else:
                safe[key] = _hash(value)
        elif isinstance(value, (list, tuple)):
            if key.endswith("_ids") or key == "ids":
                safe[key] = tuple(
                    _safe_identifier(item) if isinstance(item, str) else item
                    for item in value
                    if isinstance(item, (str, int, float))
                )
            elif all(isinstance(item, (int, float, bool)) for item in value):
                safe[key] = tuple(value)
            else:
                safe[key] = _hash(value)
    return safe


# 原始可观测方案使用的兼容名称。
Metrics = MetricsRecorder
