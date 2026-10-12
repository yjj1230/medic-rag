from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from medical_rag.core.config import Settings

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"

# When no secret is configured in a development environment we mint a random
# per-process secret instead of falling back to a guessable value such as the
# database URL.  Tokens then simply do not survive a restart, which is the
# correct trade-off for local work: there is no shared secret to steal.
_EPHEMERAL_DEV_SECRET = secrets.token_urlsafe(48)
_warned_about_ephemeral_secret = False


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else Settings.from_env()


def _secret(settings: Settings) -> str:
    global _warned_about_ephemeral_secret
    if settings.jwt_secret is not None:
        return settings.jwt_secret.get_secret_value()
    if settings.is_dev_environment:
        if not _warned_about_ephemeral_secret:
            logger.warning(
                "JWT_SECRET 未配置，开发环境使用进程内随机密钥；"
                "重启后既有令牌全部失效。非开发环境将拒绝启动。"
            )
            _warned_about_ephemeral_secret = True
        return _EPHEMERAL_DEV_SECRET
    # Belt and braces: startup validation should already have refused to boot.
    raise RuntimeError("JWT_SECRET 未配置，拒绝签发或校验令牌")


def sign_access_token(
    tenant_id: UUID,
    patient_id: UUID,
    user_id: UUID,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> str:
    resolved = _settings(settings)
    issued_at = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "patient_id": str(patient_id),
        "iss": resolved.jwt_issuer,
        "aud": resolved.jwt_audience,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=resolved.access_token_ttl_minutes),
    }
    return jwt.encode(payload, _secret(resolved), algorithm=_ALGORITHM)


def verify_access_token(token: str, *, settings: Settings | None = None) -> dict[str, str]:
    resolved = _settings(settings)
    try:
        payload = jwt.decode(
            token,
            _secret(resolved),
            algorithms=[_ALGORITHM],
            # Pinning issuer/audience stops a token minted for another service
            # (or another deployment) from being replayed here.
            audience=resolved.jwt_audience,
            issuer=resolved.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        raise ValueError("token expired") from None
    except jwt.InvalidTokenError:
        raise ValueError("invalid token") from None

    try:
        tenant_id = str(payload["tenant_id"])
        patient_id = str(payload["patient_id"])
        user_id = str(payload["sub"])
    except KeyError as exc:
        raise ValueError("token missing required claims") from exc
    return {"tenant_id": tenant_id, "patient_id": patient_id, "user_id": user_id}
