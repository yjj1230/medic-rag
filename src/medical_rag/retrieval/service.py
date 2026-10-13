from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from time import perf_counter

from medical_rag.auth.policy import AccessContext
from medical_rag.consent.service import ConsentService
from medical_rag.core.errors import ConsentRequired
from medical_rag.domain.enums import ConsentPurpose
from medical_rag.knowledge.contracts import Citation
from medical_rag.models.contracts import EmbeddingModel, EmbeddingPipeline
from medical_rag.observability.metrics import CalibrationArtifact
from medical_rag.retrieval.contracts import (
    Evidence,
    GatePolicy,
    Reranker,
    RetrievalFilters,
    RetrievalResult,
    SearchIndex,
    SearchQuery,
    SearchRecord,
    Visibility,
    coerce_rerank_order,
)
from medical_rag.retrieval.ranking import HybridRanker

# 检索故障与「医学证据不足」必须区分：集群/服务不可用时患者看到的是
# 「服务暂不可用」，而不是被误导为「没有足够医学证据」。
REASON_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
REASON_RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"
REASON_EMBEDDING_UNAVAILABLE = "embedding_unavailable"
REASON_CALIBRATION_MISMATCH = "calibration_artifact_mismatch"
REASON_UNAUTHORIZED_SCOPE = "unauthorized_scope"

# ``vector_score`` 可能所处的分数空间。与 ``CalibrationArtifact`` 的
# ``score_space`` 取值域一致（09-24 spec §2.4）：缺省的 OpenSearch
# ``knn_vector`` 是 ``l2_inv``（``1/(1+L2²)``），显式 ``cosinesimil`` 才是真余弦。
_SCORE_SPACES = frozenset({"l2_inv", "cosinesimil"})
_MAPPING_UNAVAILABLE = "mapping_unavailable"


class RetrievalService:
    def __init__(
        self,
        index: SearchIndex,
        *,
        reranker: Reranker | None = None,
        consent_service: ConsentService | None = None,
        min_score: float = 0.35,
        embedding_model: EmbeddingModel | EmbeddingPipeline | None = None,
        embedding_model_name: str = "text-embedding-3-small",
        embedding_dimensions: int | None = None,
        ranker: HybridRanker | None = None,
        candidate_limit: int | None = None,
        rerank_limit: int | None = None,
        fused_score_threshold: float | None = None,
        rank_gate: int | None = None,
        cosine_threshold: float | None = None,
        score_space: str | None = None,
        calibration_artifact: CalibrationArtifact | None = None,
    ) -> None:
        self.index = index
        self.reranker = reranker
        self.consent_service = consent_service
        # 已弃用的兼容参数；分数阈值属于
        # 校准后的混合路径，有意不在此处施加。
        self.min_score = min_score
        self.embedding_model = embedding_model
        self.embedding_model_name = embedding_model_name
        self.embedding_dimensions = embedding_dimensions
        self.ranker = ranker or HybridRanker()
        if calibration_artifact is not None:
            if self.ranker.rrf_k != calibration_artifact.rrf_k:
                raise ValueError("calibration artifact RRF parameter mismatch")
            if embedding_model_name != calibration_artifact.embedding_model:
                raise ValueError("calibration artifact embedding model mismatch")
            if calibration_artifact.uses_cosine_gate:
                # 余弦门禁是候选级（融合前）门禁，与融合后门禁（RRF 阈值 /
                # 名次）作用于不同阶段。同时显式配置意味着调用方不清楚哪个
                # 生效 —— fail-fast（09-24 spec §5.3 互斥规则）。
                if rank_gate is not None:
                    raise ValueError("cosine gate artifact must not be combined with rank_gate")
                if fused_score_threshold is not None:
                    raise ValueError(
                        "cosine gate artifact must not be combined with fused_score_threshold"
                    )
            else:
                if rank_gate is not None:
                    raise ValueError("calibration artifact requires a fused-score threshold")
                if fused_score_threshold is not None and float(fused_score_threshold) != float(
                    calibration_artifact.threshold
                ):
                    raise ValueError("calibration artifact threshold mismatch")
        self.calibration_artifact = calibration_artifact
        if candidate_limit is not None and candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        if rerank_limit is not None and rerank_limit < 1:
            raise ValueError("rerank_limit must be positive")
        if fused_score_threshold is not None and not 0.0 <= fused_score_threshold <= 1.0:
            raise ValueError("fused_score_threshold must be between 0 and 1")
        if rank_gate is not None and rank_gate < 1:
            raise ValueError("rank_gate must be positive")
        # 余弦阈值与其分数空间必须一同声明：0.5 在 l2_inv 与 cosinesimil 两个
        # 空间恰好数值相同，其他取值则不相等（余弦 0.53 ↔ l2_inv 0.5155），
        # 单给阈值会产生无法察觉的静默偏移（09-24 spec §2.4 / §5.2）。
        if cosine_threshold is not None:
            if isinstance(cosine_threshold, bool) or not isinstance(cosine_threshold, (int, float)):
                raise ValueError("cosine_threshold must be numeric")
            if not 0.0 <= float(cosine_threshold) <= 1.0:
                raise ValueError("cosine_threshold must be between 0 and 1")
            if score_space is None:
                raise ValueError("cosine_threshold requires score_space")
            if fused_score_threshold is not None or rank_gate is not None:
                raise ValueError(
                    "cosine_threshold must not be combined with "
                    "fused_score_threshold or rank_gate"
                )
        if score_space is not None:
            if score_space not in _SCORE_SPACES:
                raise ValueError(f"score_space must be one of {sorted(_SCORE_SPACES)}")
            if cosine_threshold is None and not (
                calibration_artifact is not None and calibration_artifact.uses_cosine_gate
            ):
                raise ValueError("score_space requires cosine_threshold")
        self.candidate_limit = candidate_limit
        self.rerank_limit = rerank_limit
        # 门禁语义必须显式（``GatePolicy``），不再依赖 0.5 这个魔数默认值：
        # 未配置 calibration_artifact 且没有显式门禁参数时**不存在硬门禁**，
        # 入选范围完全由 ``top_k`` 决定。旧默认值把单路 rank>=2 的候选全部
        # 砍掉（归一化上界恰为 0.5），使 top_k=8 只回 1-2 条证据。
        #
        # 策略解析优先级（09-24 spec §5.3）：
        #   artifact.cosine_threshold → COSINE_ABSOLUTE（阈值取 artifact）
        #   artifact                  → CALIBRATED
        #   cosine_threshold          → COSINE_ABSOLUTE（显式参数）
        #   fused_score_threshold     → EXPLICIT_THRESHOLD
        #   rank_gate                 → RANK_GATE
        #   否则                       → TOP_K
        self.cosine_threshold: float | None = None
        self.score_space: str | None = None
        if calibration_artifact is not None and calibration_artifact.uses_cosine_gate:
            self.gate_policy = GatePolicy.COSINE_ABSOLUTE
            self.fused_score_threshold: float | None = None
            self.cosine_threshold = float(calibration_artifact.cosine_threshold)  # type: ignore[arg-type]
            self.score_space = calibration_artifact.score_space
        elif calibration_artifact is not None:
            self.gate_policy = GatePolicy.CALIBRATED
            self.fused_score_threshold = float(calibration_artifact.threshold)
        elif cosine_threshold is not None:
            self.gate_policy = GatePolicy.COSINE_ABSOLUTE
            self.fused_score_threshold = None
            self.cosine_threshold = float(cosine_threshold)
            self.score_space = score_space
        elif fused_score_threshold is not None:
            self.gate_policy = GatePolicy.EXPLICIT_THRESHOLD
            self.fused_score_threshold = float(fused_score_threshold)
        elif rank_gate is not None:
            self.gate_policy = GatePolicy.RANK_GATE
            self.fused_score_threshold = None
        else:
            self.gate_policy = GatePolicy.TOP_K
            self.fused_score_threshold = None
        self.rank_gate = rank_gate
        # 词法/向量两路候选检索互相独立，共用线程池并行以压缩链路时延。
        # 线程池必须显式关闭（close()）并注册 atexit，否则工作线程会阻止
        # 解释器退出，长生命周期进程也会随实例重建而泄漏线程。
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="rag-retrieval"
        )
        atexit.register(self._executor.shutdown, False)

    def close(self) -> None:
        """显式关闭并行候选检索线程池（幂等）。

        长生命周期单例在应用 shutdown 事件里调用；未调用时由 ``atexit``
        兜底。关闭后本实例不再接受检索请求（线程池会拒绝新任务）。
        """
        self._executor.shutdown(wait=True)

    def _with_query_embedding(self, query: SearchQuery) -> tuple[SearchQuery, str | None]:
        """为查询生成向量。embedding 服务不可用时降级为纯词法检索而非整体失败。"""
        if self.embedding_model is None or query.embedding is not None:
            return query, None
        try:
            if isinstance(self.embedding_model, EmbeddingPipeline):
                vectors = self.embedding_model.embed_records([query.text], self.embedding_model_name)
            else:
                vectors = self.embedding_model.embed([query.text])
                if len(vectors) != 1:
                    raise ValueError("embedding vector count mismatch")
                vectors = [tuple(float(value) for value in vectors[0])]
            if len(vectors) != 1:
                raise ValueError("embedding vector count mismatch")
            vector = tuple(float(value) for value in vectors[0])
            if not vector:
                raise ValueError("embedding vector dimension mismatch")
            if self.embedding_dimensions is not None and len(vector) != self.embedding_dimensions:
                raise ValueError("embedding vector dimension mismatch")
            return replace(query, embedding=vector), None
        except ValueError:
            # 维度/数量不匹配属于配置错误，fail-fast 以免静默产生低质检索。
            raise
        except Exception:
            return query, "embedding_unavailable"

    @staticmethod
    def _is_authorized(record: SearchRecord, context: AccessContext) -> bool:
        if record.deleted or not record.source_active or not record.retrieval_eligible:
            return False
        if record.visibility == Visibility.PUBLIC:
            return record.patient_id is None
        return record.tenant_id == context.tenant_id and record.patient_id == context.patient_id

    @staticmethod
    def _has_citation(citation: Citation) -> bool:
        return bool(citation.source_id and citation.title and citation.version and citation.location)

    @staticmethod
    def _record_breaks_calibration(record: SearchRecord, artifact: CalibrationArtifact) -> bool:
        """记录是否破坏了它所在索引的标定身份（09-28 spec §5.3）。

        两个维度独立判定：

        * ``embedding_model`` → **全局**统一（向量空间身份不因索引而异，两个索引
          的向量必须来自同一模型才能互相比较、才能与查询向量比较）；
        * ``index_version`` → **按可见性分派**（各索引按设计可有不同版本，如
          ``public-v1`` / ``private-v1``）。

        期望版本无法解析（``None``，即可见性不在已知枚举内）即视为破坏——fail-closed，
        不新增"跳过校验"的宽松分支（I2）。
        """

        if record.embedding_model != artifact.embedding_model:
            return True
        expected = artifact.expected_index_version_for(str(record.visibility))
        return expected is None or record.index_version != expected

    @staticmethod
    def _rerank_indices(order: object) -> list[int]:
        """Normalize every reranker return shape to canonical candidate indices.

        形状归一集中在 :func:`medical_rag.retrieval.contracts.coerce_rerank_order`
        （协议与适配器共用同一个返回类型），这里只做 list 适配。
        """

        return list(coerce_rerank_order(order))

    def retrieve(self, query: SearchQuery, context: AccessContext) -> RetrievalResult:
        started = perf_counter()
        if self.calibration_artifact is not None and not (
            callable(getattr(self.index, "search_lexical", None))
            and callable(getattr(self.index, "search_vector", None))
        ):
            return RetrievalResult(
                items=[],
                reason=REASON_CALIBRATION_MISMATCH,
                diagnostics={
                    "status": "degraded",
                    "calibration_artifact_mismatch": True,
                    "calibration_artifact_reason": "hybrid_candidate_seams_required",
                },
            )
        if self.gate_policy is GatePolicy.COSINE_ABSOLUTE and not (
            callable(getattr(self.index, "search_lexical", None))
            and callable(getattr(self.index, "search_vector", None))
        ):
            # 显式余弦门禁需要暴露双路候选接缝的索引：单次 ``search`` 接口既
            # 给不出候选级 ``vector_score``，也无法执行「向量过滤 + 词法交集」。
            # 静默退回 top_k 会让「配了门禁却完全不生效」，故显式降级返回。
            return RetrievalResult(
                items=[],
                reason=REASON_CALIBRATION_MISMATCH,
                diagnostics={
                    "status": "degraded",
                    "gate_policy": self.gate_policy.value,
                    "gate_skipped_reason": "hybrid_candidate_seams_required",
                },
            )
        if query.patient_id not in {None, context.patient_id}:
            return RetrievalResult(items=[], reason=REASON_UNAUTHORIZED_SCOPE)

        private_allowed = query.include_private
        if private_allowed and self.consent_service is not None:
            try:
                self.consent_service.require_active_consent(context, ConsentPurpose.REPORT_PROCESSING)
            except ConsentRequired:
                private_allowed = False

        query, embedding_error = self._with_query_embedding(query)

        if callable(getattr(self.index, "search_lexical", None)) and callable(
            getattr(self.index, "search_vector", None)
        ):
            return self._retrieve_hybrid(query, context, private_allowed, started, embedding_error)

        # 只有当候选路径暴露索引/模型溯源
        # 以及校准后的融合门禁时，校准工件才有效。遗留
        # 的单次调用索引无法证明任一条件，因此 fail closed，
        # 而不是返回来自未知构建的记录。
        if self.calibration_artifact is not None:
            return RetrievalResult(
                items=[],
                reason=REASON_CALIBRATION_MISMATCH,
                diagnostics={
                    "status": "degraded",
                    "calibration_artifact_mismatch": True,
                },
            )

        records = self.index.search(
            query,
            tenant_id=context.tenant_id,
            patient_id=context.patient_id,
            include_public=query.include_public,
            include_private=private_allowed,
            limit=max(query.top_k * 3, query.top_k),
        )
        authorized = [
            record
            for record in records
            if self._is_authorized(record, context)
            and self._has_citation(record.citation)
        ]
        if not authorized:
            return RetrievalResult(
                items=[], reason=embedding_error or REASON_INSUFFICIENT_EVIDENCE
            )

        if self.reranker is not None:
            order = self.reranker.rank(query.text, [record.text for record in authorized])
            reordered: list[SearchRecord] = []
            for index in self._rerank_indices(order):
                if isinstance(index, int) and 0 <= index < len(authorized):
                    if authorized[index] not in reordered:
                        reordered.append(authorized[index])
            reordered.extend(record for record in authorized if record not in reordered)
            authorized = reordered

        legacy_rank_gate = self.rank_gate or query.top_k
        return RetrievalResult(
            items=[
                Evidence(
                    id=record.id,
                    text=record.text,
                    citation=record.citation,
                    score=record.score,
                    visibility=record.visibility,
                    tenant_id=record.tenant_id,
                    patient_id=record.patient_id,
                    document_id=record.document_id,
                    locator=record.locator,
                    # 父子块上下文扩展（Task 9）依赖该字段分组；丢失它会让
                    # expand_with_parents 把所有命中归入 None 桶，扩展变成 no-op。
                    parent_block_id=record.parent_block_id,
                )
                for record in authorized[: min(query.top_k, legacy_rank_gate)]
            ],
            reason=None,
        )

    def _search_candidates(
        self,
        kind: str,
        filters: RetrievalFilters,
        candidate_limit: int,
        query: SearchQuery,
    ) -> tuple[list[SearchRecord], float, bool]:
        """执行单路候选检索并计时；失败时降级为空候选集并标记错误。"""
        started = perf_counter()
        errored = False
        try:
            if kind == "lexical":
                records = self.index.search_lexical(query.text, filters, candidate_limit)  # type: ignore[attr-defined]
            else:
                records = (
                    self.index.search_vector(query.embedding, filters, candidate_limit)  # type: ignore[attr-defined]
                    if query.embedding
                    else []
                )
        except Exception:
            records = []
            errored = True
        return records, (perf_counter() - started) * 1000, errored

    def _retrieve_hybrid(
        self,
        query: SearchQuery,
        context: AccessContext,
        private_allowed: bool,
        started: float,
        embedding_error: str | None = None,
    ) -> RetrievalResult:
        candidate_limit = self.candidate_limit or max(query.top_k * 5, 50)
        filters = RetrievalFilters(
            tenant_id=context.tenant_id,
            patient_id=context.patient_id,
            include_public=query.include_public,
            include_private=private_allowed,
            document_ids=query.document_ids,
        )
        diagnostics: dict[str, object] = {
            "candidate_limit": candidate_limit,
            "embedding_model": self.embedding_model_name,
            "index_version": None,
            "stage_elapsed_ms": {},
            # 入选策略必须可观测：外部据此判断「无门禁（未校准）」而不是
            # 把 top_k 退化当成正常的低召回。余弦门禁下 gate_threshold 是
            # 该门禁的绝对阈值，gate_score_space 声明其数值空间（09-24 §5.4）。
            "gate_policy": self.gate_policy.value,
            "gate_threshold": (
                self.cosine_threshold
                if self.gate_policy is GatePolicy.COSINE_ABSOLUTE
                else self.fused_score_threshold
            ),
            "gate_score_space": self.score_space,
            "rank_gate": self.rank_gate,
        }
        if self.calibration_artifact is not None:
            diagnostics["calibration_artifact"] = {
                "index_version": self.calibration_artifact.index_version,
                "embedding_model": self.calibration_artifact.embedding_model,
                "rrf_k": self.calibration_artifact.rrf_k,
                "threshold": self.calibration_artifact.threshold,
                "eval_dataset_version": self.calibration_artifact.eval_dataset_version,
                "cosine_threshold": self.calibration_artifact.cosine_threshold,
                "score_space": self.calibration_artifact.score_space,
                "public_index_version": self.calibration_artifact.public_index_version,
                "private_index_version": self.calibration_artifact.private_index_version,
            }
        if embedding_error is not None:
            diagnostics["embedding_degraded"] = True

        # 词法/向量两路并行：互相独立，等待时间取两者最大值而非求和。
        lexical_future = self._executor.submit(
            self._search_candidates, "lexical", filters, candidate_limit, query
        )
        if query.embedding:
            vector_future = self._executor.submit(
                self._search_candidates, "vector", filters, candidate_limit, query
            )
        else:
            vector_future = None
        lexical, lexical_elapsed, lexical_errored = lexical_future.result()
        if vector_future is not None:
            vector, vector_elapsed, vector_errored = vector_future.result()
        else:
            vector, vector_elapsed, vector_errored = [], 0.0, False
        vector_attempted = vector_future is not None
        if lexical_errored:
            diagnostics["lexical_error"] = True
        if vector_errored:
            diagnostics["vector_error"] = True
        diagnostics["lexical_candidates_raw"] = len(lexical)
        diagnostics["vector_candidates_raw"] = len(vector)

        lexical = [
            record
            for record in lexical
            if self._is_authorized(record, context)
            and self._has_citation(record.citation)
        ]
        vector = [
            record
            for record in vector
            if self._is_authorized(record, context)
            and self._has_citation(record.citation)
        ]
        vector_spaces = {record.vector_score_space for record in vector if record.vector_score_space}
        if vector:
            diagnostics["vector_score_space"] = (
                sorted(vector_spaces)[0]
                if len(vector_spaces) == 1
                else (sorted(vector_spaces) if vector_spaces else "unknown")
            )
        mapping_score_space: str | None = None
        resolver = getattr(self.index, "resolve_vector_score_space", None)
        if vector_attempted and not vector_errored:
            if callable(resolver):
                try:
                    resolved = resolver()
                except Exception:
                    resolved = _MAPPING_UNAVAILABLE
                mapping_score_space = (
                    resolved if isinstance(resolved, str) else _MAPPING_UNAVAILABLE
                )
            elif len(vector_spaces) == 1:
                # Legacy test/development indexes have no physical mapping seam;
                # use their record annotation for compatibility while production
                # OpenSearchStore always exposes the resolver above.
                mapping_score_space = next(iter(vector_spaces))
        diagnostics["mapping_score_space"] = mapping_score_space
        score_space_match: bool | None = None
        if (
            self.gate_policy is GatePolicy.COSINE_ABSOLUTE
            and vector_attempted
            and not vector_errored
            and (callable(resolver) or bool(vector))
        ):
            expected = self.score_space
            mapping_ok = mapping_score_space in _SCORE_SPACES and mapping_score_space == expected
            records_ok = bool(vector) and all(
                record.vector_score_space == expected for record in vector
            )
            # Empty vector responses carry no record identity; the mapping identity
            # remains sufficient to validate the gate for this query.
            if not vector:
                records_ok = mapping_ok
            score_space_match = mapping_ok and records_ok
            diagnostics["score_space_match"] = score_space_match
            if not score_space_match:
                diagnostics["status"] = "degraded"
                stage_elapsed = diagnostics.get("stage_elapsed_ms")
                if isinstance(stage_elapsed, dict):
                    stage_elapsed["total"] = (perf_counter() - started) * 1000
                return RetrievalResult(
                    items=[],
                    reason=REASON_CALIBRATION_MISMATCH,
                    diagnostics=diagnostics,
                )
        diagnostics["score_space_match"] = score_space_match
        if self.calibration_artifact is not None:
            artifact = self.calibration_artifact
            breaking = [
                record
                for record in (*lexical, *vector)
                if self._record_breaks_calibration(record, artifact)
            ]
            # 同一条记录可能同时来自词法与向量两路，按 id 去重（保留首次出现）。
            # 不去重会让 mismatch_count 虚高约一倍，并用同一条记录占满只保留 5 条
            # 的 mismatch_records（09-28 spec §5.3）。
            unique: dict[str, SearchRecord] = {}
            for record in breaking:
                unique.setdefault(record.id, record)
            mismatched = list(unique.values())
            if mismatched:
                diagnostics["calibration_artifact_mismatch"] = True
                diagnostics["status"] = "degraded"
                # 可追溯：只报身份标识（id/可见性/版本/模型），**不含正文**
                # —— 诊断面绝不泄漏 PHI（09-28 spec §5.4 / I8）。
                diagnostics["calibration_mismatch_records"] = tuple(
                    {
                        "id": record.id,
                        "visibility": str(record.visibility),
                        "index_version": record.index_version,
                        "embedding_model": record.embedding_model,
                    }
                    for record in mismatched[:5]
                )
                diagnostics["calibration_mismatch_count"] = len(mismatched)
                diagnostics["stage_elapsed_ms"]["total"] = (perf_counter() - started) * 1000
                return RetrievalResult(
                    items=[],
                    reason=REASON_CALIBRATION_MISMATCH,
                    diagnostics=diagnostics,
                )
        # P1-5（评审 2026-10-12）：vector_score 语义随诊断透出。必须在门禁
        # **之前**取样：门禁会改写 vector 集合，语义标注应反映原始候选。
        # 任何消费者在对 vector_score 做阈值/比较前都必须先看这个字段；
        # ``unknown`` 表示语义未知（内存索引等非生产实现），不得用于门禁。
        # --- 余弦相关性门禁（09-24 spec §4.3 / §6.1）---------------------------
        # 落点在 ACL/引用过滤之后、RRF 融合之前：门禁是「相关性准入」，只做
        # 减法（I1）。融合只看到相关候选，RRF 排名不被不相关内容干扰。
        if self.gate_policy is GatePolicy.COSINE_ABSOLUTE:
            threshold = self.cosine_threshold
            if not vector_attempted or vector_errored:
                # 向量路径未实际运行 → 跳过门禁（有意降级，09-24 spec §7.2）。
                # 照常执行会把全部词法候选交集清空 → 每次查询都拒答，把一次
                # 可降级运行放大成完全不可用。代价（该状态下恢复「永不拒答」）
                # 显式接受，并由本诊断键标记，监控据此告警。
                diagnostics["gate_skipped_reason"] = "vector_path_unavailable"
            elif threshold is None:  # pragma: no cover - 策略解析保证非 None
                diagnostics["gate_skipped_reason"] = "cosine_threshold_missing"
            else:
                gated = [record for record in vector if record.vector_score >= threshold]
                gated_ids = {record.id for record in gated}
                lexical_before = len(lexical)
                # 词法交集：只保留同时通过余弦门禁的候选（严格于「只过滤向量」，
                # 防止纯关键词命中的不相关文档绕过门禁 —— 09-24 §4.2）。
                lexical = [record for record in lexical if record.id in gated_ids]
                diagnostics["vector_gated_candidates"] = len(gated)
                diagnostics["vector_candidates_gated_out"] = len(vector) - len(gated)
                diagnostics["lexical_gated_out"] = lexical_before - len(lexical)
                vector = gated

        fused_started = perf_counter()
        fused = self.ranker.fuse(lexical, vector, candidate_limit)
        fused_elapsed = (perf_counter() - fused_started) * 1000

        if fused:
            versions = {record.index_version for record in fused if record.index_version}
            if versions:
                diagnostics["index_version"] = sorted(versions)[0]
        diagnostics["lexical_candidates"] = len(lexical)
        diagnostics["vector_candidates"] = len(vector)
        diagnostics["lexical_filtered"] = diagnostics["lexical_candidates_raw"] - len(lexical)
        diagnostics["vector_filtered"] = diagnostics["vector_candidates_raw"] - len(vector)
        diagnostics["fused_candidates"] = len(fused)
        diagnostics["candidate_ids"] = [record.id for record in fused]
        lexical_ranks: dict[str, int] = {}
        for candidate in lexical:
            if candidate.id not in lexical_ranks:
                lexical_ranks[candidate.id] = len(lexical_ranks) + 1
        vector_ranks: dict[str, int] = {}
        for candidate in vector:
            if candidate.id not in vector_ranks:
                vector_ranks[candidate.id] = len(vector_ranks) + 1
        diagnostics["ranks"] = {
            record.id: {
                "rank": index,
                "fused_score": record.fused_score,
                "calibrated_score": record.calibrated_score,
                "lexical_rank": lexical_ranks.get(record.id),
                "vector_rank": vector_ranks.get(record.id),
                "bm25_score": record.bm25_score,
                "vector_score": record.vector_score,
            }
            for index, record in enumerate(fused, 1)
        }
        # 阶段耗时直接维护在局部字典里，不再从 diagnostics 里读回来再断言类型
        # （生产代码里的 assert 在 -O 下会消失，不能作为一致性保障）。
        stage_elapsed = diagnostics.get("stage_elapsed_ms")
        if not isinstance(stage_elapsed, dict):  # pragma: no cover - 内部构造保证
            stage_elapsed = {}
            diagnostics["stage_elapsed_ms"] = stage_elapsed
        stage_elapsed.update({"lexical": lexical_elapsed, "vector": vector_elapsed, "fusion": fused_elapsed})

        # P1-6（评审 2026-10-12）：rerank 必须发生在门禁**之前**。旧顺序
        # （先门禁后 rerank）意味着被阈值误杀的候选永远不会因 rerank 复活，
        # rerank 只能在一小池幸存者里洗牌，重排序价值大打折扣。新顺序：
        # 融合候选 → rerank（rerank_limit 头部重排）→ 门禁 → top_k。
        # rerank 失败时静默保留融合序（与旧行为一致），不放大故障。
        rerank_candidates = fused[: self.rerank_limit or len(fused)]
        rerank_started = perf_counter()
        if self.reranker is not None:
            try:
                order = self.reranker.rank(query.text, [record.text for record in rerank_candidates])
                reordered: list[SearchRecord] = []
                for index in self._rerank_indices(order):
                    if isinstance(index, int) and 0 <= index < len(rerank_candidates):
                        if rerank_candidates[index] not in reordered:
                            reordered.append(rerank_candidates[index])
                reordered.extend(record for record in rerank_candidates if record not in reordered)
                rerank_candidates = reordered
            except Exception:
                diagnostics["rerank_error"] = True
        fused = rerank_candidates + fused[len(rerank_candidates):]
        diagnostics["rerank_before_gate"] = True
        diagnostics["reranked_candidates"] = len(rerank_candidates)
        stage_elapsed["rerank"] = (perf_counter() - rerank_started) * 1000

        if self.fused_score_threshold is not None:
            fused = [
                record
                for record in fused
                if (record.calibrated_score or 0.0) >= self.fused_score_threshold
            ]
        elif self.rank_gate is not None:
            fused = fused[: max(0, self.rank_gate)]
        # 门禁通过条数（截断 top_k 之前）必须可观测：这是判断 top_k 是否真正
        # 生效、门禁是否把结果砍空的关键指标。
        diagnostics["gate_passed_candidates"] = len(fused)
        if not fused:
            diagnostics["status"] = "degraded"
            stage_elapsed["total"] = (perf_counter() - started) * 1000
            return RetrievalResult(
                items=[],
                reason=self._empty_reason(
                    lexical_errored=lexical_errored,
                    vector_attempted=vector_attempted,
                    vector_errored=vector_errored,
                    embedding_error=embedding_error,
                ),
                diagnostics=diagnostics,
            )

        stage_elapsed["total"] = (perf_counter() - started) * 1000
        # P1-6（评审 2026-10-12）：Evidence.score 的语义显式标注 —— 它是
        # 原始 RRF 融合分（bm25/kNN 分数不可互比，也不经 rerank 重算），
        # 只用于诊断展示，禁止当相关性阈值或置信度使用。
        diagnostics["score_semantics"] = "rrf_fused_raw"
        diagnostics["status"] = "ok"

        return RetrievalResult(
            items=[
                Evidence(
                    id=record.id,
                    text=record.text,
                    citation=record.citation,
                    score=record.score,
                    visibility=record.visibility,
                    tenant_id=record.tenant_id,
                    patient_id=record.patient_id,
                    document_id=record.document_id,
                    locator=record.locator,
                    # Task 9 父子块扩展按该字段分组并调用 search_by_parent；
                    # 不传就等价于关闭上下文扩展（静默 no-op）。
                    parent_block_id=record.parent_block_id,
                )
                for record in fused[: query.top_k]
            ],
            reason=None,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _empty_reason(
        *,
        lexical_errored: bool,
        vector_attempted: bool,
        vector_errored: bool,
        embedding_error: str | None,
    ) -> str:
        """区分「检索不可用」与「医学证据不足」。

        所有**实际发起**的候选路径都失败时返回 ``retrieval_unavailable``：
        集群宕机/服务不可用不能被呈现成「没有足够的医学证据」。降级模式下
        没有任何一路检索过（embedding 未配置）时，词法一路失败同样属于
        「不可用」，而不是「证据不足」。
        """
        all_attempted_failed = lexical_errored and (not vector_attempted or vector_errored)
        if all_attempted_failed:
            return REASON_RETRIEVAL_UNAVAILABLE
        return embedding_error or REASON_INSUFFICIENT_EVIDENCE

    def search(self, query: SearchQuery, context: AccessContext) -> list[Evidence]:
        return self.retrieve(query, context).items
