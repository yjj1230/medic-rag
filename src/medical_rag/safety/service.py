from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from medical_rag.domain.enums import RiskClass
from medical_rag.safety.rules import DIAGNOSIS_PATTERNS, MEDICATION_CHANGE_PATTERNS, URGENT_PATTERNS
from medical_rag.safety.utterance import is_general_knowledge_enquiry


class SafetyAction(StrEnum):
    ALLOW_RETRIEVAL = "allow_retrieval"
    SAFE_TEMPLATE = "safe_template"
    URGENT_ESCALATION = "urgent_escalation"


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    risk_class: RiskClass
    action: SafetyAction
    safety_notice: str | None = None


class SafetyService:
    def classify(self, question: str) -> SafetyDecision:
        normalized = question.casefold()
        urgent_hit = any(
            pattern.casefold() in normalized for pattern in URGENT_PATTERNS
        )
        # 用户以第一人称描述自身症状才升级；若是「网上有人说胸痛怎么回事」这类
        # 一般知识问询，不应被急症词表短路。fail-safe 姿态下，仅当有明显一般知识
        # 信号且不含第一人称症状描述时才豁免。
        if urgent_hit and not is_general_knowledge_enquiry(question):
            return SafetyDecision(
                risk_class=RiskClass.URGENT,
                action=SafetyAction.URGENT_ESCALATION,
                safety_notice="你描述的情况可能需要紧急医疗处理，请立即拨打急救电话或前往最近的急诊。",
            )
        if any(pattern.casefold() in normalized for pattern in MEDICATION_CHANGE_PATTERNS):
            return SafetyDecision(
                risk_class=RiskClass.MEDICATION_CHANGE,
                action=SafetyAction.SAFE_TEMPLATE,
                safety_notice="我不能建议加减药量、停药或换药。请联系开药医生或药师确认。",
            )
        if any(pattern.casefold() in normalized for pattern in DIAGNOSIS_PATTERNS):
            return SafetyDecision(
                risk_class=RiskClass.DIAGNOSIS_REQUEST,
                action=SafetyAction.SAFE_TEMPLATE,
                safety_notice="我不能替代医生作出诊断。可以帮助你整理报告中的客观信息，并建议你带报告咨询医生。",
            )
        return SafetyDecision(risk_class=RiskClass.ORDINARY, action=SafetyAction.ALLOW_RETRIEVAL)
