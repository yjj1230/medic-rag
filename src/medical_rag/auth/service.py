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
    """解析调用方身份。

    身份来自经过验证的持有者令牌。``X-Tenant-ID`` / ``X-Patient-ID`` / ``X-User-ID``
    **绝不**单独信任：那样做会让任何调用方冒充任意患者（读取和删除其记录、伪造审计
    操作人）。只有当运维在开发环境中显式启用 ``allow_insecure_identity_headers`` 时
    才会采信它们，而且即便如此，这些值也会与数据库校验而非照单全收。
    """
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        try:
            claims = verify_access_token(token, settings=settings)
        except ValueError:
            # 有意不做异常链：令牌失败的原因不得泄漏给
            # 调用方，而且未鉴权的调用方不是一份缺陷报告。
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
    # 令牌必须与权威记录一致。不一致意味着
    # 令牌过期或被伪造，此时应 fail closed，而不是悄悄偏向任何一方。
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
    """仅开发环境的身份请求头，会与数据库校验。

    若该开关在非开发环境被意外启用则抛错，这样错误配置永远不会变成一扇敞开的门。
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
