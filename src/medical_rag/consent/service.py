from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.audit.service import AuditService
from medical_rag.auth.policy import AccessContext
from medical_rag.core.errors import AccessDenied, ConsentRequired
from medical_rag.domain.enums import AuditAction, ConsentPurpose
from medical_rag.domain.models import Consent, Patient


class ConsentService:
    def __init__(
        self,
        session: Session,
        *,
        policy_version: str = "v1",
        audit_service: AuditService | None = None,
    ) -> None:
        self.session = session
        self.policy_version = policy_version
        self.audit = audit_service or AuditService(session)

    def _require_patient(self, context: AccessContext) -> Patient:
        patient = self.session.execute(
            select(Patient).where(
                Patient.id == context.patient_id,
                Patient.tenant_id == context.tenant_id,
            )
        ).scalar_one_or_none()
        if patient is None:
            raise AccessDenied("当前身份不属于指定租户或患者")
        return patient

    def grant(
        self,
        context: AccessContext,
        purpose: ConsentPurpose,
        *,
        granted_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> Consent:
        self._require_patient(context)
        consent = Consent(
            tenant_id=context.tenant_id,
            patient_id=context.patient_id,
            purpose=purpose,
            policy_version=self.policy_version,
            granted_at=granted_at or datetime.now(UTC),
            expires_at=expires_at,
        )
        self.session.add(consent)
        self.session.flush()
        self.audit.record(
            context,
            AuditAction.CONSENT_GRANTED,
            resource_type="consent",
            resource_id=consent.id,
            payload_metadata={"purpose": purpose.value, "policy_version": self.policy_version},
        )
        return consent

    def revoke(
        self,
        context: AccessContext,
        purpose: ConsentPurpose,
        *,
        revoked_at: datetime | None = None,
    ) -> Consent:
        self._require_patient(context)
        consent = self.session.execute(
            select(Consent)
            .where(
                Consent.tenant_id == context.tenant_id,
                Consent.patient_id == context.patient_id,
                Consent.purpose == purpose,
                Consent.revoked_at.is_(None),
            )
            .order_by(Consent.granted_at.desc())
        ).scalars().first()
        if consent is None:
            raise ConsentRequired("没有可撤回的有效同意记录")
        consent.revoked_at = revoked_at or datetime.now(UTC)
        self.session.flush()
        self.audit.record(
            context,
            AuditAction.CONSENT_REVOKED,
            resource_type="consent",
            resource_id=consent.id,
            payload_metadata={"purpose": purpose.value},
        )
        return consent

    def require_active_consent(
        self,
        context: AccessContext,
        purpose: ConsentPurpose,
        *,
        now: datetime | None = None,
    ) -> Consent:
        self._require_patient(context)
        current_time = now or datetime.now(UTC)
        consent = self.session.execute(
            select(Consent)
            .where(
                Consent.tenant_id == context.tenant_id,
                Consent.patient_id == context.patient_id,
                Consent.purpose == purpose,
                Consent.policy_version == self.policy_version,
                Consent.granted_at <= current_time,
                Consent.revoked_at.is_(None),
                (Consent.expires_at.is_(None) | (Consent.expires_at > current_time)),
            )
            .order_by(Consent.granted_at.desc())
        ).scalars().first()
        if consent is None:
            raise ConsentRequired("当前用途缺少有效的知情同意")
        return consent

