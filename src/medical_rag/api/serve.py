"""API 进程的控制台入口。

之所以存在，是因为本项目此前根本没有启动后端的方式：没有 ``[project.scripts]``、
没有 Dockerfile，``docker-compose.yml`` 里也没有 ``api`` 服务，尽管 nginx 已经把
``/v1/`` 反向代理到 ``api:8000``。
"""
from __future__ import annotations

import logging

from medical_rag.core.config import Settings

logger = logging.getLogger(__name__)


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    # 在绑定端口之前就失败：不安全的部署绝不应接受
    # 哪怕一个请求，而且运维应当立刻看到原因。
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


if __name__ == "__main__":  # pragma: no cover - 手动调用
    main()
