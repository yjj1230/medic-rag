from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from time import perf_counter
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from medical_rag.audit.service import AuditService
from medical_rag.auth.policy import AccessContext
from medical_rag.chat.citations import CitationValidator, UnsupportedClaim
from medical_rag.chat.contracts import AnswerResponse, AnswerSection, DraftAnswer
from medical_rag.chat.prompts import build_chat_request
from medical_rag.core.errors import AccessDenied
from medical_rag.domain.enums import AuditAction, DocumentStatus
from medical_rag.domain.models import ChatMessage, ChatSession, Document
from medical_rag.models.contracts import ChatModel, ModelError, ModelTimeout
from medical_rag.query.contracts import ProcessedQuery, SearchStrategy
from medical_rag.query.pipeline import QueryPipeline, needs_public_backfill
from medical_rag.retrieval.contracts import Evidence, RetrievalResult, SearchQuery
from medical_rag.retrieval.expansion import ScopedSiblingLookup, expand_with_parents
from medical_rag.retrieval.service import REASON_INSUFFICIENT_EVIDENCE, REASON_RETRIEVAL_UNAVAILABLE
from medical_rag.safety.service import SafetyAction, SafetyDecision, SafetyService

logger = logging.getLogger(__name__)

# 进入模型调用前必须保留的最小时间预算；低于该值直接降级，避免半截请求。
_MIN_MODEL_BUDGET_SECONDS = 10.0

# 允许透出到响应/trace 的检索诊断键白名单。诊断是排查「门禁砍空、集群故障、
# embedding 降级」的唯一线索，但它同时会进入对外响应，因此只放行已知的
# 计数器/版本号/耗时，绝不透传候选文档 id 之类的内部标识。
_ANSWER_DIAGNOSTIC_KEYS = (
    "status",
    "gate_policy",
    "gate_threshold",
    "gate_score_space",
    "gate_skipped_reason",
    "vector_gated_candidates",
    "vector_candidates_gated_out",
    "lexical_gated_out",
    "rank_gate",
    "gate_passed_candidates",
    "candidate_limit",
    "lexical_candidates",
    "vector_candidates",
    "vector_score_space",
    "fused_candidates",
    "reranked_candidates",
    "lexical_error",
    "vector_error",
    "rerank_error",
    "rerank_before_gate",
    "score_semantics",
    "embedding_degraded",
    "embedding_model",
    "index_version",
    "calibration_artifact",
    "calibration_artifact_mismatch",
    "calibration_mismatch_count",
    "calibration_mismatch_records",
    "stage_elapsed_ms",
    "public_fallback",
    "public_fallback_items",
    "public_fallback_reason",
)


def _json_value(column, key: str):
    """Portable ``json_extract(column, '$.key')`` expression for a JSON column.

    SQLAlchemy's generic ``JSON`` type exposes dialect-specific accessors
    (``.astext`` is PostgreSQL-only), so the path is built from the dialect's own
    JSON extractor.  That lets the same predicate run on PostgreSQL in
    production and SQLite in tests.
    """
    return column[key].as_string() if hasattr(column[key], "as_string") else column[key].astext


def _answer_diagnostics(diagnostics: dict[str, object] | None) -> dict[str, object]:
    """Whitelist retrieval diagnostics for the response/trace surface."""

    return {
        key: value
        for key, value in (diagnostics or {}).items()
        if key in _ANSWER_DIAGNOSTIC_KEYS
    }


class ChatOrchestrator:
    """聊天链路编排器。

    长生命周期单例；数据库会话通过 ``session_factory`` 按请求获取，
    避免跨请求共享 Session 带来的并发与事务污染问题。
    """

    def __init__(
        self,
        retriever,
        model_gateway: ChatModel,
        *,
        safety_service: SafetyService | None = None,
        citation_validator: CitationValidator | None = None,
        session_factory: Callable[[], Session] | None = None,
        audit_service: AuditService | None = None,
        policy_version: str = "v1",
        query_pipeline: QueryPipeline | None = None,
        history_size: int = 4,
        top_k: int = 8,
        total_timeout_seconds: float = 60.0,
        # 注入外发模型名：请求载荷与合规白名单都必须与配置的 MODEL_NAME 一致。
        model_name: str = "gpt-5.5",
        # Task 9: parent/child context expansion.  ``expansion_index`` is the
        # sibling-lookup seam (the same search index holding child-blocks).
        # When None, evidence passes through unchanged (legacy behavior).
        expansion_index: object | None = None,
        context_token_budget: int | None = None,
    ) -> None:
        self.retriever = retriever
        self.model_gateway = model_gateway
        self.safety = safety_service or SafetyService()
        self.citations = citation_validator or CitationValidator()
        self.session_factory = session_factory
        self.audit = audit_service
        self.policy_version = policy_version
        self.query_pipeline = query_pipeline or QueryPipeline()
        self.history_size = history_size
        self.top_k = top_k
        self.total_timeout_seconds = total_timeout_seconds
        self.model_name = model_name
        self.expansion_index = expansion_index
        self.context_token_budget = context_token_budget

    @contextmanager
    def _session_scope(self) -> Iterator[Session | None]:
        if self.session_factory is None:
            yield None
            return
        session = self.session_factory()
        try:
            yield session
        finally:
            session.close()

    def _record_audit(self, db: Session | None, context: AccessContext, action: AuditAction, **kwargs) -> None:
        audit = self.audit or (AuditService(db) if db is not None else None)
        if audit is not None:
            audit.record(context, action, **kwargs)

    def _safe_response(
        self,
        context: AccessContext,
        decision: SafetyDecision,
        *,
        db: Session | None = None,
        trace_id: str | None = None,
    ) -> AnswerResponse:
        self._record_audit(
            db,
            context,
            AuditAction.SAFETY_BLOCK,
            resource_type="chat",
            payload_metadata={"risk_class": decision.risk_class.value},
        )
        if db is not None and self.audit is None:
            db.commit()
        return AnswerResponse(
            status="safe_template",
            sections=[AnswerSection(title="安全提示", text=decision.safety_notice or "请咨询医生。")],
            citations=[],
            safety_notice=decision.safety_notice,
            trace_id=trace_id,
        )

    def _resolve_session(
        self, db: Session | None, context: AccessContext, session_id: UUID | None
    ) -> ChatSession | None:
        """加载会话并强制校验归属：越权会话视为安全事件，直接拒绝。"""
        if db is None or session_id is None:
            return None
        chat_session = db.get(ChatSession, session_id)
        if chat_session is None:
            return None
        if chat_session.tenant_id != context.tenant_id or chat_session.patient_id != context.patient_id:
            raise AccessDenied("无权访问该会话")
        return chat_session

    def _load_history(self, db: Session | None, chat_session: ChatSession | None) -> tuple[ChatMessage, ...]:
        """加载最近 N 条历史（按时间正序返回）。"""
        if db is None or chat_session is None:
            return ()
        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id == chat_session.id)
            .order_by(ChatMessage.created_at.desc())
            .limit(self.history_size)
            .all()
        )
        return tuple(reversed(rows))

    @staticmethod
    def _has_private_documents(db: Session, context: AccessContext) -> bool:
        return (
            db.execute(
                select(Document.id)
                .where(
                    Document.tenant_id == context.tenant_id,
                    Document.patient_id == context.patient_id,
                    Document.status == DocumentStatus.READY,
                    Document.deleted_at.is_(None),
                )
                .limit(1)
            ).first()
            is not None
        )

    def _persist_answer(
        self,
        db: Session,
        context: AccessContext,
        chat_session: ChatSession | None,
        question: str,
        validated,
        *,
        client_request_id: UUID | None,
        trace_id: str | None,
        intent: str,
    ) -> UUID:
        if chat_session is None:
            chat_session = ChatSession(tenant_id=context.tenant_id, patient_id=context.patient_id)
            db.add(chat_session)
            db.flush()
        if client_request_id is not None:
            # ``-`` / ``.astext`` are PostgreSQL-only operators that the generic
            # ``JSON`` type does not provide, so the lookup builds the JSON path
            # expression explicitly.  This keeps the predicate index-compatible
            # with ``ix_chat_messages_client_request_id`` on PostgreSQL while
            # still working on SQLite, where ``.astext`` raised AttributeError
            # and silently disabled idempotency.
            already_persisted = db.execute(
                select(ChatMessage.id)
                .where(
                    ChatMessage.session_id == chat_session.id,
                    _json_value(ChatMessage.metadata_json, "client_request_id")
                    == str(client_request_id),
                )
                .limit(1)
            ).first()
            if already_persisted is not None:
                logger.info(
                    "duplicate client_request_id %s ignored for session %s",
                    client_request_id,
                    chat_session.id,
                )
                return chat_session.id
        db.add_all(
            [
                ChatMessage(
                    session_id=chat_session.id,
                    role="user",
                    content=question,
                    metadata_json=(
                        {"client_request_id": str(client_request_id)} if client_request_id else None
                    ),
                ),
                ChatMessage(
                    session_id=chat_session.id,
                    role="assistant",
                    content=validated.text,
                    metadata_json={
                        "trace_id": trace_id,
                        "intent": intent,
                        "cited_evidence": list(validated.cited_evidence),
                    },
                ),
            ]
        )
        self._record_audit(
            db,
            context,
            AuditAction.ANSWER_DELIVERED,
            resource_type="chat_session",
            resource_id=chat_session.id,
        )
        try:
            db.commit()
        except IntegrityError:
            # P1-1（评审 2026-10-12）：先 SELECT 后 INSERT 存在 TOCTOU 窗口，
            # 并发同 ``client_request_id`` 的请求会双双通过探查。数据库层的
            # 唯一索引（ux_chat_messages_session_client_request_id，见迁移
            # 0009）是最终防线：败者回滚后回读既有记录，按重复请求处理，
            # 而不是让整个回答因竞态而 500。
            db.rollback()
            if client_request_id is not None:
                already_persisted = db.execute(
                    select(ChatMessage.id)
                    .where(
                        ChatMessage.session_id == chat_session.id,
                        _json_value(ChatMessage.metadata_json, "client_request_id")
                        == str(client_request_id),
                    )
                    .limit(1)
                ).first()
            else:
                already_persisted = None
            if already_persisted is not None:
                logger.info(
                    "duplicate client_request_id %s lost idempotency race for session %s",
                    client_request_id,
                    chat_session.id,
                )
                return chat_session.id
            # 与幂等键无关的完整性冲突（外键缺失等）不是竞态，向上抛出，
            # 由调用方按「持久化失败不阻断投递」路径降级。
            raise
        return chat_session.id

    def _degraded(
        self,
        notice: str,
        *,
        trace_id: str | None = None,
        title: str = "服务暂不可用",
        diagnostics: dict[str, object] | None = None,
    ) -> AnswerResponse:
        return AnswerResponse(
            status="degraded",
            sections=[AnswerSection(title=title, text=notice)],
            citations=[],
            safety_notice=notice,
            trace_id=trace_id,
            diagnostics=_answer_diagnostics(diagnostics),
        )

    def _backfill_public_evidence(
        self,
        query: SearchQuery,
        context: AccessContext,
        private_items: list[Evidence],
        strategy: SearchStrategy,
        diagnostics: dict[str, object],
    ) -> tuple[list[Evidence], str | None]:
        """私有优先策略下，私有证据不足时补一次公共检索。

        报告解释类问题只查私有资料时，「参考范围/正常值」这类关键依据恰恰
        常在公共指南里；私有证据不足就直接报「证据不足」等于主动放弃已有
        证据。补检索只放宽到**公共**库，同时保持私有证据在前（分层注入，
        每条证据自带 visibility 标注来源）。
        """
        if not needs_public_backfill(strategy, len(private_items)):
            return private_items, None
        public_query = replace(query, include_public=True, include_private=False)
        try:
            public_result = self.retriever.retrieve(public_query, context)
        except Exception:
            logger.exception(
                "public evidence backfill failed for patient %s", context.patient_id
            )
            diagnostics["public_fallback"] = True
            diagnostics["public_fallback_items"] = 0
            diagnostics["public_fallback_reason"] = REASON_RETRIEVAL_UNAVAILABLE
            return private_items, REASON_RETRIEVAL_UNAVAILABLE
        diagnostics["public_fallback"] = True
        diagnostics["public_fallback_items"] = len(public_result.items)
        diagnostics["public_fallback_reason"] = public_result.reason
        merged = list(private_items)
        seen = {item.id for item in merged}
        for item in public_result.items:
            if item.id not in seen:
                seen.add(item.id)
                merged.append(item)
        return merged, public_result.reason

    def _expand_evidence(
        self,
        evidence: list,
        context: AccessContext,
        *,
        include_public: bool,
        include_private: bool,
    ) -> list:
        """Apply Task 9 parent/child context expansion.

        Returns the evidence set to feed the model and the citation validator.
        Sibling child-blocks are pulled through the shared index's
        ``search_by_parent`` seam, scoped to this request's tenant/patient so
        sibling context never crosses patients.  When the orchestrator has no
        ``expansion_index`` configured, evidence passes through unchanged so
        legacy behavior is preserved.
        """
        if self.expansion_index is None:
            return evidence
        lookup = ScopedSiblingLookup(
            self.expansion_index,
            tenant_id=context.tenant_id,
            patient_id=context.patient_id,
            include_public=include_public,
            include_private=include_private,
        )
        return expand_with_parents(
            evidence,
            index=lookup,
            context_token_budget=self.context_token_budget,
        )

    def answer(
        self,
        context: AccessContext,
        question: str,
        *,
        document_ids: list[UUID] | None = None,
        session_id: UUID | None = None,
        client_request_id: UUID | None = None,
    ) -> AnswerResponse:
        question = question.strip()
        if not question:
            raise ValueError("问题不能为空")
        started = perf_counter()

        with self._session_scope() as db:
            decision = self.safety.classify(question)
            if decision.action != SafetyAction.ALLOW_RETRIEVAL:
                return self._safe_response(context, decision, db=db)

            chat_session = self._resolve_session(db, context, session_id)
            history = self._load_history(db, chat_session)
            # 无数据库时保持旧的宽松默认（仅测试/开发场景），生产以真实数据为准。
            has_private = (
                self._has_private_documents(db, context) if db is not None else True
            )
            processed: ProcessedQuery = self.query_pipeline.process(
                question,
                history=history,
                has_private=has_private,
                access_context=context,
            )
            # 归一化/上下文补全可能引入新的风险表述，对处理后的文本二次分类。
            if processed.text != question:
                recheck = self.safety.classify(processed.text)
                if recheck.action != SafetyAction.ALLOW_RETRIEVAL:
                    return self._safe_response(context, recheck, db=db)

            query = SearchQuery(
                text=processed.text,
                patient_id=context.patient_id,
                top_k=self.top_k,
                include_public=processed.search_strategy.include_public,
                include_private=processed.search_strategy.include_private,
                document_ids=tuple(document_ids or ()),
            )
            try:
                retrieval: RetrievalResult = self.retriever.retrieve(query, context)
            except Exception:
                logger.exception("retrieval failed for patient %s", context.patient_id)
                return self._degraded("检索服务暂时不可用，未生成回答。请稍后重试或咨询医生。")
            diagnostics = dict(retrieval.diagnostics)
            evidence: list[Evidence] = list(retrieval.items)
            reason = retrieval.reason
            # 报告解释类问题的策略是「私有优先」：私有证据不足时补一次公共
            # 检索，而不是直接对患者宣布「没有医学证据」。
            backfilled, public_reason = self._backfill_public_evidence(
                query, context, evidence, processed.search_strategy, diagnostics
            )
            if diagnostics.get("public_fallback"):
                evidence = backfilled
                if not evidence:
                    # 任一环节报「检索不可用」就必须按故障处理，不得被降级
                    # 呈现为「没有足够的医学证据」。
                    if REASON_RETRIEVAL_UNAVAILABLE in (reason, public_reason):
                        reason = REASON_RETRIEVAL_UNAVAILABLE
                    else:
                        reason = public_reason or reason
            if not evidence:
                if reason in (None, REASON_INSUFFICIENT_EVIDENCE):
                    notice = "当前没有足够且可定位的医学证据，暂时无法可靠回答。请携带相关资料咨询医生。"
                    return AnswerResponse(
                        status="insufficient_evidence",
                        sections=[AnswerSection(title="暂无法回答", text=notice)],
                        citations=[],
                        safety_notice=notice,
                        diagnostics=_answer_diagnostics(diagnostics),
                    )
                # 检索链路本身不可用（集群宕机、embedding 全失败、校准不匹配等）：
                # 必须与「没有医学证据」区分，否则患者被误导、监控无从告警。
                logger.warning(
                    "retrieval unavailable for patient %s: reason=%s diagnostics=%s",
                    context.patient_id,
                    reason,
                    _answer_diagnostics(diagnostics),
                )
                return self._degraded(
                    "检索服务暂时不可用，未生成回答。请稍后重试或咨询医生。",
                    diagnostics=diagnostics,
                )

            remaining = self.total_timeout_seconds - (perf_counter() - started)
            if remaining <= _MIN_MODEL_BUDGET_SECONDS:
                return self._degraded("请求处理超时，未生成回答。请稍后重试。")
            # Task 9: expand child-block hits with sibling context so the model
            # sees the surrounding parent reading window.  Both the request and
            # the citation validator operate on the same expanded set.
            # 补检索注入公共证据后，兄弟块查找也必须放开公共库，否则公共证据
            # 的上下文扩展会被静默关闭。
            evidence_for_model = self._expand_evidence(
                evidence,
                context,
                include_public=processed.search_strategy.include_public
                or bool(diagnostics.get("public_fallback")),
                include_private=processed.search_strategy.include_private,
            )
            request = build_chat_request(
                question,
                evidence_for_model,
                model=self.model_name,
                policy_version=self.policy_version,
            )
            request = replace(request, timeout_seconds=min(request.timeout_seconds, remaining))
            try:
                model_response = self.model_gateway.chat(context, request)
            except (ModelTimeout, TimeoutError, ModelError):
                notice = "模型服务暂时不可用，未生成未经验证的医学回答。请稍后重试或咨询医生。"
                return self._degraded(notice, trace_id=request.trace_id)

            cited_ids = self.citations.extract_ids(model_response.text)
            try:
                validated = self.citations.validate(
                    DraftAnswer(text=model_response.text, cited_evidence=cited_ids),
                    evidence_for_model,
                )
            except UnsupportedClaim:
                notice = "回答未通过医学证据和引用校验，因此未向患者展示。请改写问题或咨询医生。"
                return self._degraded(
                    notice, trace_id=model_response.trace_id, title="无法安全生成回答"
                )

            persisted_session_id: UUID | None = chat_session.id if chat_session else session_id
            if db is not None:
                try:
                    persisted_session_id = self._persist_answer(
                        db,
                        context,
                        chat_session,
                        question,
                        validated,
                        client_request_id=client_request_id,
                        trace_id=model_response.trace_id,
                        intent=processed.intent.value,
                    )
                except Exception:
                    # 持久化失败不应阻断已校验的答案投递；降级为不记录会话。
                    logger.exception("failed to persist chat answer")
                    db.rollback()
            return AnswerResponse(
                status="answered",
                sections=[AnswerSection(title="基于资料的说明", text=validated.text)],
                citations=validated.citations,
                safety_notice=None,
                trace_id=model_response.trace_id,
                session_id=persisted_session_id,
                diagnostics=_answer_diagnostics(diagnostics),
            )
