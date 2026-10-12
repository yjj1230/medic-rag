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

# One throttle per process, shared by every login attempt.
_login_throttle = LoginThrottle()

# Failure response.  Every credential-related rejection — unknown account,
# account without a password, wrong password, suspended account — returns this
# exact message.  Distinguishing them let an attacker enumerate registered phone
# numbers before spending a single guess.  The caller learns only that the
# attempt failed.
_INVALID_CREDENTIALS = "手机号或密码错误"

# Password length bounds are validation, not policy: ``max_length`` also caps
# the amount of data bcrypt is handed.
_PASSWORD_MIN_LENGTH = 1
_PASSWORD_MAX_LENGTH = 200

# A bcrypt hash of a fixed value, used to spend the same CPU on an unknown
# account as on a real one.  Without it, "unknown account" returns in
# microseconds while "wrong password" takes ~100 ms, which is a trivially
# measurable account-existence oracle.  The value is a known-answer hash of a
# random string and is never a valid password for any account.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"medical-rag-dummy-password", bcrypt.gensalt())


class LoginRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=_PASSWORD_MIN_LENGTH, max_length=_PASSWORD_MAX_LENGTH)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token *type*, not a credential
    expires_at: datetime


_login_router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-cost password check.

    A missing hash still runs a bcrypt comparison against a dummy hash so the
    timing of the three failure modes (no account / no password / wrong
    password) is indistinguishable.
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
        # Refuse before touching the database: a throttled attempt must not cost
        # a query, and the wait must be unambiguous to the caller.
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

