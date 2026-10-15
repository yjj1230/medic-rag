from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.api.dependencies import get_settings
from medical_rag.auth.jwt import sign_access_token
from medical_rag.auth.policy import AccessContext
from medical_rag.auth.service import current_access_context
from medical_rag.auth.throttle import LoginThrottle, throttle_key_pair
from medical_rag.consent.service import ConsentService
from medical_rag.core.config import Settings
from medical_rag.db.session import get_session
from medical_rag.domain.enums import ConsentPurpose, RecordStatus
from medical_rag.domain.models import Consent, Patient


router = APIRouter(prefix="/v1/consents", tags=["consent"])

# 每进程一个限流器，被每一次登录尝试共享。
_login_throttle = LoginThrottle()

# 失败响应。所有与凭据相关的拒绝 —— 账号不存在、
# 账号无密码、密码错误、账号被停用 —— 都返回这
# 同一条消息。区分它们会让攻击者在花掉哪怕一次猜测之前
# 就枚举出已注册的手机号。调用方只能得知这次
# 尝试失败了。
_INVALID_CREDENTIALS = "手机号或密码错误"

# 密码长度上下界属于校验，而非策略：``max_length`` 同时限制了
# 交给 bcrypt 的数据量。
_PASSWORD_MIN_LENGTH = 1
_PASSWORD_MAX_LENGTH = 200

# 一个固定值的 bcrypt 哈希，用来在账号不存在时也消耗与真实账号
# 相同的 CPU。没有它，"账号不存在"会在微秒级返回，
# 而"密码错误"要花约 100 ms，这就成了可轻松测量
# 的账号存在性预言机。该值是对一个随机字符串的已知答案哈希，
# 对任何账号都永远不是有效密码。
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"medical-rag-dummy-password", bcrypt.gensalt())


class LoginRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=_PASSWORD_MIN_LENGTH, max_length=_PASSWORD_MAX_LENGTH)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth 令牌*类型*，不是凭据
    expires_at: datetime


_login_router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _verify_password(password: str, password_hash: str | None) -> bool:
    """恒定开销的密码校验。

    哈希缺失时仍会对一个哑哈希执行 bcrypt 比较，使三种失败模式（无账号 / 无密码 /
    密码错误）的耗时无法区分。
    """
    encoded = password.encode("utf-8")
    candidate = password_hash.encode("utf-8") if password_hash else _DUMMY_PASSWORD_HASH
    matched = bcrypt.checkpw(encoded, candidate)
    return bool(password_hash) and matched


@_login_router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    throttle_keys = throttle_key_pair(
        payload.phone, request.client.host if request.client else None
    )
    retry_after = _login_throttle.retry_after(*throttle_keys)
    if retry_after > 0:
        # 在碰数据库之前就拒绝：被限流的尝试不得产生查询开销，
        # 且等待时间对调用方必须明确无歧义。
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试过于频繁，请稍后再试",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    patient = session.execute(
        select(Patient).where(Patient.external_user_id == payload.phone)
    ).scalar_one_or_none()
    password_ok = _verify_password(payload.password, patient.password_hash if patient else None)
    account_active = patient is not None and patient.status == RecordStatus.ACTIVE
    if not (password_ok and account_active):
        _login_throttle.record_failure(*throttle_keys)
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED, detail=_INVALID_CREDENTIALS
        )

    _login_throttle.record_success(*throttle_keys)
    now = datetime.now(UTC)
    token = sign_access_token(
        tenant_id=patient.tenant_id,
        patient_id=patient.id,
        user_id=patient.id,
        settings=settings,
        now=now,
    )
    return LoginResponse(
        access_token=token,
        token_type="bearer",  # noqa: S106 - OAuth token *type*, not a credential
        expires_at=now + timedelta(minutes=settings.access_token_ttl_minutes),
    )


class ConsentResponse(BaseModel):
    id: str
    purpose: ConsentPurpose
    policy_version: str
    granted_at: datetime
    revoked_at: datetime | None


@router.post("/{purpose}", response_model=ConsentResponse, status_code=status.HTTP_201_CREATED)
def grant_consent(
    purpose: ConsentPurpose,
    context: AccessContext = Depends(current_access_context),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ConsentResponse:
    consent = ConsentService(session, policy_version=settings.consent_policy_version).grant(
        context, purpose
    )
    session.commit()
    return ConsentResponse(
        id=str(consent.id),
        purpose=consent.purpose,
        policy_version=consent.policy_version,
        granted_at=consent.granted_at,
        revoked_at=consent.revoked_at,
    )


@router.delete("/{purpose}", response_model=ConsentResponse)
def revoke_consent(
    purpose: ConsentPurpose,
    context: AccessContext = Depends(current_access_context),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ConsentResponse:
    consent = ConsentService(session, policy_version=settings.consent_policy_version).revoke(
        context, purpose
    )
    session.commit()
    return ConsentResponse(
        id=str(consent.id),
        purpose=consent.purpose,
        policy_version=consent.policy_version,
        granted_at=consent.granted_at,
        revoked_at=consent.revoked_at,
    )


@router.get("", response_model=list[ConsentResponse])
def list_consents(
    context: AccessContext = Depends(current_access_context),
    session: Session = Depends(get_session),
) -> list[ConsentResponse]:
    consents = session.scalars(
        select(Consent)
        .where(Consent.tenant_id == context.tenant_id, Consent.patient_id == context.patient_id)
        .order_by(Consent.granted_at.desc())
    ).all()
    return [
        ConsentResponse(
            id=str(c.id),
            purpose=c.purpose,
            policy_version=c.policy_version,
            granted_at=c.granted_at,
            revoked_at=c.revoked_at,
        )
        for c in consents
    ]

