"""检索评测台（2026-10-12 按取证重建）。

背景
----
原 ``scripts/retrieval_tuning_lab.py`` 在 2026-10-12 的 git 对象库损坏中丢失
（``scripts/`` 当时未被版本库追踪，原开发机亦不在本机）。本脚本依据
``docs/superpowers/plans/2026-10-08-rag-evaluation.md``、``eval/`` 用例与
``eval_out/`` 历史结果重建，并保持原 CLI 契约：

    python scripts/retrieval_tuning_lab.py --cases-file <path> --quick --rounds 5 --out <dir>

全局约束（沿用原评估计划）
--------------------------
* 所有检索指标**必须真实调用** ``RetrievalService.retrieve``；禁止把全部候选
  全量返回的模拟检索路径充当评估结果。
* public/private 用例使用真实访问上下文；跨患者、跨租户、删除与复核记录不混用身份。

本次新增
--------
* 门禁档 ``gate=cosine_absolute``：用于量化「无关查询从不拒答」这一 P0 缺陷在
  门禁开启后的变化（历史评测中该准确率恒为 0.00）。
* ``--selftest``：内置确定性夹具跑通全链路（不接触 OpenSearch），可在无容器环境
  下验证驱动层、身份映射、指标聚合与 JSON 输出。

指标口径说明
------------
本重建版按每条用例的**规范** ``gold_evidence_ids`` 计算指标（历史 grouped 口径
额外使用 ``gold_evidence_groups`` 的等价组语义）。门禁对比（拒答准确率）不受该
差异影响；若要复现 grouped 的 ``accepted_hit_rate``，请另行接入分组口径。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from medical_rag.auth.policy import AccessContext  # noqa: E402
from medical_rag.observability.metrics import (  # noqa: E402
    CalibrationArtifact,
    EvaluationCase,
    evaluate_retrieval,
)
from medical_rag.retrieval.contracts import (  # noqa: E402
    RetrievalFilters,
    SearchQuery,
    SearchRecord,
    Visibility,
)
from medical_rag.retrieval.ranking import HybridRanker  # noqa: E402
from medical_rag.retrieval.service import RetrievalService  # noqa: E402

# --------------------------------------------------------------------------- #
# 测试身份（取证：eval_out/private_audit_raw.json、eval_out/_db_probe.txt）
# --------------------------------------------------------------------------- #
TENANT = UUID("11111111-1111-1111-1111-111111111111")
# 合成 blood_report.pdf 的所属患者（私有 quality 用例的 gold 证据属于该患者）。
PATIENT_A = UUID("22222222-2222-2222-2222-222222222222")
# 安全用例中的查询方「患者 B」：其自身无记录，forbidden 证据属于患者 A。
PATIENT_B = UUID("be97b3b1-ed4e-45d5-bab3-92c433fcc9fe")

# --------------------------------------------------------------------------- #
# 配置矩阵
# --------------------------------------------------------------------------- #
Config = dict[str, Any]

QUICK_CONFIGS: list[tuple[str, Config]] = [
    ("A1-pure-lexical", {"mode": "lexical", "rrf_k": 60, "candidate_limit": 50}),
    ("A2-pure-vector", {"mode": "vector", "rrf_k": 60, "candidate_limit": 50}),
    ("A3-hybrid-rrf60", {"mode": "hybrid", "rrf_k": 60, "candidate_limit": 50}),
    ("G1-hybrid-rrf60-gate", {"mode": "hybrid", "rrf_k": 60, "candidate_limit": 50, "gate": True}),
]

FULL_CONFIGS: list[tuple[str, Config]] = QUICK_CONFIGS + [
    ("B3-rrf30", {"mode": "hybrid", "rrf_k": 30, "candidate_limit": 50}),
    ("D2-cand20", {"mode": "hybrid", "rrf_k": 60, "candidate_limit": 20}),
    ("G2-cand20-gate", {"mode": "hybrid", "rrf_k": 60, "candidate_limit": 20, "gate": True}),
]


@dataclass(frozen=True, slots=True)
class LoadedCase:
    """一条用例及其评测元数据（``category`` 不进入 ``EvaluationCase``）。"""

    case: EvaluationCase
    category: str
    bucket: str
    note: str

    def identity(self) -> AccessContext:
        """按用例语义选择访问身份（取证还原的映射规则）。"""

        if self.category == "safety":
            patient = PATIENT_B
        elif self.case.scope == "private":
            patient = PATIENT_A
        else:
            patient = PATIENT_A
        return AccessContext(tenant_id=TENANT, patient_id=patient, user_id=patient)

    def query(self, top_k: int) -> SearchQuery:
        private = self.case.scope == "private"
        return SearchQuery(
            text=self.case.question,
            patient_id=self.identity().patient_id,
            top_k=top_k,
            include_public=not private,
            include_private=private,
        )


def load_cases(path: Path) -> list[LoadedCase]:
    loaded: list[LoadedCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        loaded.append(
            LoadedCase(
                case=EvaluationCase.from_json(raw),
                category=str(raw.get("category", "quality")),
                bucket=str(raw.get("bucket", "standard")),
                note=str(raw.get("note", "")),
            )
        )
    return loaded


# --------------------------------------------------------------------------- #
# 离线自检夹具（不接触 OpenSearch）
# --------------------------------------------------------------------------- #
class _ModeMaskedIndex:
    """按评测模式遮蔽单路候选，使三种模式跑在同一服务代码路径上。

    * ``lexical``：遮蔽向量路（服务侧也未装配向量模型，二者一致）；
    * ``vector``：遮蔽词法路，得到纯向量召回；
    * ``hybrid``：两路透传。
    """

    def __init__(self, inner, mode: str) -> None:
        self._inner = inner
        self._mode = mode

    def search(self, query, *, tenant_id, patient_id, include_public, include_private, limit):
        return self._inner.search(
            query,
            tenant_id=tenant_id,
            patient_id=patient_id,
            include_public=include_public,
            include_private=include_private,
            limit=limit,
        )

    def search_lexical(self, text, filters, limit):
        if self._mode == "vector":
            return []
        return self._inner.search_lexical(text, filters, limit)

    def search_vector(self, embedding, filters, limit):
        if self._mode == "lexical":
            return []
        return self._inner.search_vector(embedding, filters, limit)

    def __getattr__(self, name):
        # ``search_by_parent`` / ``prime`` 等接缝一律透传内层索引。
        return getattr(self._inner, name)


class _FixtureIndex:
    """确定性索引：实现 ``search`` 与双路接缝，供 ``--selftest`` 使用。

    真实门禁要求索引暴露 ``search_lexical`` / ``search_vector``（否则 fail-closed）；
    本夹具据此实现，使离线自检能够覆盖门禁路径。
    """

    PUBLIC_VERSION = "public-v1"
    PRIVATE_VERSION = "private-v1"
    EMBEDDING_MODEL = "test-embed"

    def __init__(self) -> None:
        self.records = [
            SearchRecord(
                id="fx-pub-a",
                text="高血压 成人 定义 测量 诊断",
                visibility=Visibility.PUBLIC,
                citation=_fixture_citation("pub-a"),
                bm25_score=2.0,
                vector_score=0.90,
                vector_score_space="l2_inv",
                index_version=self.PUBLIC_VERSION,
                embedding_model=self.EMBEDDING_MODEL,
            ),
            SearchRecord(
                id="fx-pub-b",
                text="复查 安排 术后 建议",
                visibility=Visibility.PUBLIC,
                citation=_fixture_citation("pub-b"),
                bm25_score=1.5,
                vector_score=0.10,
                vector_score_space="l2_inv",
                index_version=self.PUBLIC_VERSION,
                embedding_model=self.EMBEDDING_MODEL,
            ),
            SearchRecord(
                id="fx-priv-a",
                text="红细胞计数 4.5 单位",
                visibility=Visibility.PRIVATE,
                citation=_fixture_citation("priv-a"),
                tenant_id=TENANT,
                patient_id=PATIENT_A,
                bm25_score=2.5,
                vector_score=0.88,
                vector_score_space="l2_inv",
                index_version=self.PRIVATE_VERSION,
                embedding_model=self.EMBEDDING_MODEL,
            ),
        ]
        # 向量路按查询文本给出固定的候选与分数（模拟真实 kNN 结果）。
        self._vector_by_query: dict[str, list[str]] = {
            "成人高血压如何定义": ["fx-pub-a"],
            "甲状腺癌术后复查应该如何安排": ["fx-pub-b"],
            "报告中的红细胞计数和单位是什么": ["fx-priv-a"],
            "患者 B 查询自己的血常规记录": [],
        }

    def _visible(self, record: SearchRecord, filters: RetrievalFilters) -> bool:
        if record.deleted or not record.source_active or not record.retrieval_eligible:
            return False
        if record.visibility == Visibility.PUBLIC:
            return filters.include_public
        if not filters.include_private:
            return False
        return record.tenant_id == filters.tenant_id and record.patient_id == filters.patient_id

    def search_lexical(
        self, text: str, filters: RetrievalFilters, limit: int
    ) -> list[SearchRecord]:
        terms = {term for term in text.split() if term}
        hits = [
            record
            for record in self.records
            if self._visible(record, filters)
            and any(term in record.text for term in terms)
        ]
        hits.sort(key=lambda item: (-item.bm25_score, item.id))
        return hits[:limit]

    def search_vector(
        self, embedding: tuple[float, ...] | None, filters: RetrievalFilters, limit: int
    ) -> list[SearchRecord]:
        if not embedding:
            return []
        wanted = set(self._vector_by_query.get(self._current_query, []))
        hits = [
            record
            for record in self.records
            if record.id in wanted and self._visible(record, filters)
        ]
        hits.sort(key=lambda item: (-item.vector_score, item.id))
        return hits[:limit]

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
        filters = RetrievalFilters(
            tenant_id=tenant_id,
            patient_id=patient_id,
            include_public=include_public,
            include_private=include_private,
        )
        hits = [record for record in self.records if self._visible(record, filters)]
        hits.sort(key=lambda item: (-item.score, item.id))
        return hits[:limit]

    # 夹具在检索前记录当前查询文本，供 ``search_vector`` 查表。
    _current_query: str = ""

    def prime(self, text: str) -> None:
        self._current_query = text


def _fixture_citation(source_id: str):
    from medical_rag.knowledge.contracts import Citation

    return Citation(
        source_id=source_id,
        title=f"fixture-{source_id}",
        version="v1",
        location="p1",
    )


class _FixtureEmbedding:
    """固定维度占位向量模型：仅让查询进入向量路，不做真实编码。"""

    def __init__(self, dimensions: int = 4) -> None:
        self._dimensions = dimensions

    def embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        return [tuple([0.0] * self._dimensions) for _ in texts]


_SELFTEST_CASES: list[dict[str, Any]] = [
    {
        "id": "fx-q-hypertension",
        "question": "成人高血压如何定义",
        "scope": "public",
        "gold_evidence_ids": ["fx-pub-a"],
        "expected_status": "answered",
        "category": "quality",
        "note": "可答：词法+向量双命中",
    },
    {
        "id": "fx-rej-thyroid",
        "question": "甲状腺癌术后复查应该如何安排",
        "scope": "public",
        "gold_evidence_ids": [],
        "expected_status": "insufficient_evidence",
        "category": "rejection",
        "note": "无关查询：基线被词法命中误导，门禁应拒答",
    },
    {
        "id": "fx-q-private",
        "question": "报告中的红细胞计数和单位是什么",
        "scope": "private",
        "gold_evidence_ids": ["fx-priv-a"],
        "expected_status": "answered",
        "category": "quality",
        "note": "私有用例：患者 A 身份",
    },
    {
        "id": "fx-sec-cross-patient",
        "question": "患者 B 查询自己的血常规记录",
        "scope": "private",
        "gold_evidence_ids": [],
        "forbidden_evidence_ids": ["fx-priv-a"],
        "expected_status": "insufficient_evidence",
        "category": "safety",
        "note": "跨患者：患者 B 身份不得读到 A 的记录",
    },
]


# --------------------------------------------------------------------------- #
# 服务构建
# --------------------------------------------------------------------------- #
def _build_real_index(settings, public_index: str, private_index: str):
    """构造带显式索引名的真实索引。

    ``api.composition.build_search_index`` 使用 ``OpenSearchStore`` 的默认名
    （``medical-rag-public`` / ``medical-rag-private``），而本机实际索引带
    ``-local`` 后缀（历史 ``retrieval_eval_results.json`` 的 meta 可证）。评测台
    因此显式传入索引名，避免查到空索引后把「语料为空」误报成「检索退化」。
    """

    from opensearchpy import OpenSearch

    from medical_rag.retrieval.opensearch_store import OpenSearchStore

    client = OpenSearch(settings.opensearch_url, timeout=10)
    return OpenSearchStore(client, public_index=public_index, private_index=private_index)


def _build_calibration(settings) -> CalibrationArtifact | None:
    from medical_rag.api.composition import load_calibration_artifact

    return load_calibration_artifact(settings)


def build_service(
    index,
    *,
    mode: str,
    rrf_k: int,
    candidate_limit: int,
    top_k: int,
    embedding_model,
    embedding_model_name: str,
    embedding_dimensions: int,
    calibration_artifact: CalibrationArtifact | None,
) -> RetrievalService:
    ranker = HybridRanker(rrf_k=rrf_k)
    masked = _ModeMaskedIndex(index, mode)
    if mode == "lexical":
        # 纯词法：不装配向量模型，避免白编码污染延迟。
        return RetrievalService(
            masked,
            ranker=ranker,
            embedding_model=None,
            embedding_model_name=embedding_model_name,
            embedding_dimensions=embedding_dimensions,
            candidate_limit=candidate_limit,
            calibration_artifact=calibration_artifact,
        )
    return RetrievalService(
        masked,
        ranker=ranker,
        embedding_model=embedding_model,
        embedding_model_name=embedding_model_name,
        embedding_dimensions=embedding_dimensions,
        candidate_limit=candidate_limit,
        calibration_artifact=calibration_artifact,
    )


def _top_k_for(config: Config) -> int:
    return int(config.get("top_k", 3))


# --------------------------------------------------------------------------- #
# 单配置评测
# --------------------------------------------------------------------------- #
def evaluate_config(
    name: str,
    config: Config,
    service: RetrievalService,
    cases: list[LoadedCase],
    *,
    rounds: int,
    top_k: int,
    describe: str = "",
) -> dict[str, Any]:
    per_case: list[dict[str, Any]] = []
    latencies: list[float] = []

    for loaded in cases:
        query = loaded.query(top_k)
        prime = getattr(service.index, "prime", None)
        if callable(prime):
            prime(query.text)
        started = perf_counter()
        result = service.retrieve(query, loaded.identity())
        elapsed_ms = (perf_counter() - started) * 1000.0
        latencies.append(elapsed_ms)
        returned = [item.id for item in result.items]
        metrics = evaluate_retrieval(loaded.case, returned, k=top_k)
        gold = set(loaded.case.gold_evidence_ids)
        top = returned[:top_k]
        precision = (len(set(top) & gold) / len(top)) if top else 0.0
        per_case.append(
            {
                "case_id": loaded.case.id,
                "category": loaded.category,
                "bucket": loaded.bucket,
                "returned_ids": returned,
                "hit_rate": metrics.hit_rate,
                "recall": metrics.recall_at_k,
                "precision": precision,
                "mrr": metrics.mrr,
                "ndcg": metrics.ndcg_at_k,
                "permission_violations": metrics.permission_violations,
                "deletion_residuals": metrics.deletion_residuals,
                "review_leakage": metrics.review_required_leakage,
                "reason": result.reason,
                "status": "ok" if returned else (result.reason or "empty"),
                "total_ms": elapsed_ms,
            }
        )

    return {
        "name": name,
        "config": {"name": name, **{k: v for k, v in config.items()}, "top_k": top_k},
        "description": describe,
        "quality": _aggregate_quality(per_case),
        "rejection": _aggregate_rejection(per_case),
        "safety": _aggregate_safety(per_case),
        "latency": _aggregate_latency(latencies, rounds=rounds),
        "cases": per_case,
    }


def _aggregate_quality(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in cases if row["category"] == "quality"]
    if not rows:
        return {"n": 0}
    return {
        "n": len(rows),
        "hit_rate": _mean(row["hit_rate"] for row in rows),
        "recall_at_k": _mean(row["recall"] for row in rows),
        "precision_at_k": _mean(row["precision"] for row in rows),
        "mrr": _mean(row["mrr"] for row in rows),
        "ndcg_at_k": _mean(row["ndcg"] for row in rows),
        "top1_accuracy": _mean(1.0 if row["mrr"] == 1.0 else 0.0 for row in rows),
    }


def _aggregate_rejection(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in cases if row["category"] == "rejection"]
    if not rows:
        return {"n": 0}
    correct_empty = sum(1 for row in rows if not row["returned_ids"])
    return {
        "n": len(rows),
        "correct_empty": correct_empty,
        "accuracy": correct_empty / len(rows),
    }


def _aggregate_safety(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in cases if row["category"] == "safety"]
    if not rows:
        return {"n": 0}
    violations = sum(row["permission_violations"] for row in rows)
    deletions = sum(row["deletion_residuals"] for row in rows)
    leakage = sum(row["review_leakage"] for row in rows)
    return {
        "n": len(rows),
        "permission_violations": violations,
        "deletion_residuals": deletions,
        "review_leakage": leakage,
        "all_zero": violations == 0 and deletions == 0 and leakage == 0,
    }


def _aggregate_latency(latencies: list[float], *, rounds: int) -> dict[str, Any]:
    if not latencies:
        return {"rounds": rounds}
    ordered = sorted(latencies)
    return {
        "rounds": rounds,
        "samples": len(ordered),
        "p50_ms": _percentile(ordered, 50),
        "p95_ms": _percentile(ordered, 95),
        "p99_ms": _percentile(ordered, 99),
        "mean_ms": _mean(ordered),
    }


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _percentile(ordered: list[float], pct: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[index]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    cases_path = Path(args.cases_file)
    if not cases_path.is_absolute():
        cases_path = ROOT / cases_path
    if not cases_path.is_file():
        print(f"[lab] 用例文件不存在: {cases_path}", file=sys.stderr)
        return 2

    cases = load_cases(cases_path)
    configs = QUICK_CONFIGS if args.quick else FULL_CONFIGS
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.selftest:
        configs_meta, results, corpus, used_cases = _run_selftest(cases, configs, args)
    else:
        configs_meta, results, corpus, used_cases = _run_real(cases, configs, args)

    if results is None:
        return 1

    payload = {
        "meta": configs_meta,
        "corpus_integrity": corpus,
        "configs": results,
        "cases": [
            {
                "id": loaded.case.id,
                "question": loaded.case.question,
                "scope": loaded.case.scope,
                "gold": list(loaded.case.gold_evidence_ids),
                "category": loaded.category,
                "forbidden": list(loaded.case.forbidden_evidence_ids),
                "note": loaded.note,
            }
            for loaded in used_cases
        ],
    }
    result_file = out_dir / "retrieval_eval_results.json"
    result_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    _print_table(results)
    print(f"\n[lab] 结果已写入: {result_file}")
    return 0


def _base_meta(args: argparse.Namespace, cases: list[LoadedCase], *, mode: str) -> dict[str, Any]:
    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "runner": "scripts/retrieval_tuning_lab.py (rebuilt 2026-10-12)",
        "mode": mode,
        "case_count": len(cases),
        "latency_rounds": args.rounds,
        "quick": bool(args.quick),
        "category_counts": dict(Counter(loaded.category for loaded in cases)),
    }


def _run_selftest(
    cases: list[LoadedCase], configs: list[tuple[str, Config]], args: argparse.Namespace
) -> tuple[dict[str, Any], list[dict[str, Any]] | None, dict[str, Any], list[LoadedCase]]:
    """离线自检：用内置夹具替代真实用例与索引。"""

    del cases
    selftest_cases = [
        LoadedCase(
            case=EvaluationCase.from_json(raw),
            category=str(raw["category"]),
            bucket="standard",
            note=str(raw.get("note", "")),
        )
        for raw in _SELFTEST_CASES
    ]
    index = _FixtureIndex()
    artifact = CalibrationArtifact(
        index_version="fixture-2026-10-12",
        embedding_model=_FixtureIndex.EMBEDDING_MODEL,
        rrf_k=60,
        threshold=0.42,
        eval_dataset_version="fixture",
        cosine_threshold=0.50,
        score_space="l2_inv",
        public_index_version=_FixtureIndex.PUBLIC_VERSION,
        private_index_version=_FixtureIndex.PRIVATE_VERSION,
    )
    meta = _base_meta(args, selftest_cases, mode="selftest")
    meta["note"] = "离线自检：夹具索引 + 夹具用例，不接触 OpenSearch"
    results = []
    for name, config in configs:
        gate = bool(config.get("gate"))
        service = build_service(
            index,
            mode=config["mode"],
            rrf_k=config["rrf_k"],
            candidate_limit=config["candidate_limit"],
            top_k=_top_k_for(config),
            embedding_model=_FixtureEmbedding(),
            embedding_model_name=_FixtureIndex.EMBEDDING_MODEL,
            embedding_dimensions=4,
            calibration_artifact=artifact if gate else None,
        )
        results.append(
            evaluate_config(
                name,
                config,
                service,
                selftest_cases,
                rounds=args.rounds,
                top_k=_top_k_for(config),
                describe="离线自检配置",
            )
        )
        service.close()
    corpus = {"note": "selftest fixture", "records": len(index.records)}
    return meta, results, corpus, selftest_cases


def _run_real(
    cases: list[LoadedCase], configs: list[tuple[str, Config]], args: argparse.Namespace
) -> tuple[dict[str, Any], list[dict[str, Any]] | None, dict[str, Any]]:
    """真实复跑：装配 OpenSearch 索引与本地向量模型。"""

    from medical_rag.core.config import get_settings
    from medical_rag.models.factory import resolve_embedding

    settings = get_settings()
    embedding = resolve_embedding(settings)
    if not embedding.enabled:
        print(
            "[lab] 向量后端不可用（EMBEDDING_BACKEND/凭据缺失），"
            "门禁与向量类指标将失真；请先修复评估运行时。",
            file=sys.stderr,
        )
    artifact = _build_calibration(settings)
    index = _build_real_index(settings, args.public_index, args.private_index)
    meta = _base_meta(args, cases, mode="real")
    meta.update(
        {
            "opensearch_url": settings.opensearch_url,
            "public_index": args.public_index,
            "private_index": args.private_index,
            "embedding_model": embedding.model_name,
            "embedding_dimensions": embedding.dimensions,
            "embedding_backend": embedding.backend,
            "calibration_artifact": (
                None if artifact is None else artifact.eval_dataset_version
            ),
        }
    )
    results = []
    for name, config in configs:
        gate = bool(config.get("gate"))
        service = build_service(
            index,
            mode=config["mode"],
            rrf_k=config["rrf_k"],
            candidate_limit=config["candidate_limit"],
            top_k=_top_k_for(config),
            embedding_model=embedding.model,
            embedding_model_name=embedding.model_name,
            embedding_dimensions=embedding.dimensions,
            calibration_artifact=artifact if gate else None,
        )
        results.append(
            evaluate_config(
                name,
                config,
                service,
                cases,
                rounds=args.rounds,
                top_k=_top_k_for(config),
                describe="真实链路配置",
            )
        )
        service.close()
    corpus = _corpus_integrity(settings, args.public_index, args.private_index)
    return meta, results, corpus, cases


def _corpus_integrity(settings, public_index: str, private_index: str) -> dict[str, Any]:
    """尽力统计两索引文档数；不可达时返回说明而非抛错。"""

    try:
        from opensearchpy import OpenSearch

        client = OpenSearch(settings.opensearch_url, timeout=5)
        info: dict[str, Any] = {}
        for label, name in (("public", public_index), ("private", private_index)):
            try:
                info[label] = client.count(index=name).get("count")
            except Exception as exc:  # noqa: BLE001 - 统计失败不阻断评测
                info[label] = f"unavailable: {type(exc).__name__}"
        return info
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _print_table(results: list[dict[str, Any]]) -> None:
    header = (
        f"{'config':24s} {'rec@k':>7s} {'mrr':>7s} {'ndcg':>7s} "
        f"{'top1':>7s} {'rej':>7s} {'p95ms':>8s}"
    )
    print("\n" + header)
    print("-" * len(header))
    for row in results:
        quality = row["quality"]
        rejection = row["rejection"]
        latency = row["latency"]
        print(
            f"{row['name']:24s} "
            f"{quality.get('recall_at_k', 0.0):7.4f} "
            f"{quality.get('mrr', 0.0):7.4f} "
            f"{quality.get('ndcg_at_k', 0.0):7.4f} "
            f"{quality.get('top1_accuracy', 0.0):7.4f} "
            f"{rejection.get('accuracy', 0.0):7.2f} "
            f"{latency.get('p95_ms', 0.0):8.1f}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检索评测台（重建版）")
    parser.add_argument(
        "--cases-file",
        default="eval/rag_cases.test.v2.1.jsonl",
        help="评测用例 JSONL 路径（默认 v2.1）",
    )
    parser.add_argument("--quick", action="store_true", help="只跑 4 个核心配置")
    parser.add_argument("--rounds", type=int, default=5, help="延迟采样轮数")
    parser.add_argument("--out", default="eval_out/lab_run", help="结果输出目录")
    parser.add_argument(
        "--public-index",
        default="medical-rag-public-local",
        help="公开索引名（默认本机实际名）",
    )
    parser.add_argument(
        "--private-index",
        default="medical-rag-private-local",
        help="私有索引名（默认本机实际名）",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="离线自检：使用内置夹具，不接触 OpenSearch",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
