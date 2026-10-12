from __future__ import annotations

import logging
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.auth.jwt import verify_access_token
from medical_rag.auth.policy import AccessContext
from medical_rag.core.config import Settings, get_settings
from medical_rag.core.errors import AuthenticationRequired
from medical_rag.db.session import get_session
from medical_rag.domain.models import Patient

logger = logging.getLogger(__name__)


def current_access_context(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_tenant_id: UUID | None = Header(default=None, alias="X-Tenant-ID"),
    x_patient_id: UUID | None = Header(default=None, alias="X-Patient-ID"),
    x_user_id: UUID | None = Header(default=None, alias="X-User-ID"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AccessContext:
    """Resolve the caller identity.

    Identity comes from a verified bearer token.  ``X-Tenant-ID`` /
    ``X-Patient-ID`` / ``X-User-ID`` are **never** trusted on their own: doing so
    let any caller act as an arbitrary patient (reading and deleting their
    records, and forging the audit actor).  They are honoured only when the
    operator has explicitly enabled ``allow_insecure_identity_headers`` in a
    development environment, and even then the values are validated against the
    database rather than taken at face value.
    """
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        try:
            claims = verify_access_token(token, settings=settings)
        except ValueError:
            # Deliberately not chained: the reason a token failed must not leak
            # to the caller, and an unauthenticated caller is not a bug report.
            raise AuthenticationRequired("token 无效或已过期") from None
        return _context_from_claims(claims, session)

    if settings.allow_insecure_identity_headers and settings.is_dev_environment:
        return _development_context(x_tenant_id, x_patient_id, x_user_id, session, settings)

    raise AuthenticationRequired("缺少完整的身份上下文")


def _context_from_claims(claims: dict[str, str], session: Session) -> AccessContext:
    try:
        patient_id = UUID(claims["patient_id"])
        tenant_id = UUID(claims["tenant_id"])
        user_id = UUID(claims["user_id"])
    except (KeyError, ValueError):
        raise AuthenticationRequired("token 身份声明无效") from None

    patient = session.execute(select(Patient).where(Patient.id == patient_id)).scalar_one_or_none()
    if patient is None:
        raise AuthenticationRequired("患者不存在")
    # The token must agree with the authoritative record.  A mismatch means a
    # stale or forged token, so fail closed instead of quietly preferring either.
    if patient.tenant_id != tenant_id:
        raise AuthenticationRequired("令牌与患者所属租户不一致")
    return AccessContext(tenant_id=patient.tenant_id, patient_id=patient.id, user_id=user_id)


def _development_context(
    tenant_id: UUID | None,
    patient_id: UUID | None,
    user_id: UUID | None,
    session: Session,
    settings: Settings,
) -> AccessContext:
    """Development-only identity headers, validated against the database.

    Raises if the flag is somehow enabled outside development, so a
    misconfiguration can never turn into an open door.
    """
    if not settings.is_dev_environment:
        raise AuthenticationRequired("非开发环境不接受请求头身份")

    if not (tenant_id and patient_id and user_id):
        raise AuthenticationRequired("缺少完整的身份上下文")

    patient = session.execute(select(Patient).where(Patient.id == patient_id)).scalar_one_or_none()
    if patient is None:
        raise AuthenticationRequired("患者不存在")
    if patient.tenant_id != tenant_id:
        raise AuthenticationRequired("身份头与患者所属租户不一致")
    logger.warning("使用不安全的请求头身份（开发模式）: patient_id=%s", patient.id)
    return AccessContext(tenant_id=patient.tenant_id, patient_id=patient.id, user_id=user_id)
