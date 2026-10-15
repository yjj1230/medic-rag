from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from medical_rag.core.config import Settings

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"

# 开发环境未配置密钥时，我们生成一个随机的
# 每进程密钥，而不是回落到某个可猜的值（例如
# 数据库 URL）。令牌因此扛不过重启，这正是
# 本地工作的正确取舍：没有可供窃取的共享密钥。
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
    # 双重保险：启动校验本应已经拒绝启动。
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
            # 固定签发方/受众，可阻止为其他服务（或其他部署）签发的令牌
            # 在这里被重放。
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
