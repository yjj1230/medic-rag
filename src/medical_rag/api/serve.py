"""Console entrypoint for the API process.

Exists because the project previously had no way to start the backend at all:
no ``[project.scripts]``, no Dockerfile, and no ``api`` service in
``docker-compose.yml`` even though nginx proxied ``/v1/`` to ``api:8000``.
"""
from __future__ import annotations

import logging

from medical_rag.core.config import Settings

logger = logging.getLogger(__name__)


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    # Fail before binding a port: an unsafe deployment should never accept a
    # single request, and the operator should see why immediately.
    settings.validate_runtime()
    for warning in settings.plaintext_phi_egress_warnings():
        logger.warning("%s", warning)

    uvicorn.run(
        "medical_rag.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.api_log_level,
    )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    main()
