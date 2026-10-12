# Backend API image for the Medical RAG service.
#
# There was previously no backend image at all, even though
# `frontend/nginx.conf` proxied `/v1/` to `http://api:8000`.
FROM python:3.12-slim AS runtime

# The settings default binds loopback; inside a container the process must
# accept connections from the proxy, so bind all interfaces explicitly.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONIOENCODING=utf-8 \
    API_HOST=0.0.0.0 \
    API_PORT=8000

WORKDIR /app

# Dependency layer first so source edits do not invalidate the wheel cache.
COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install --upgrade pip && pip install ".[live]"

# Run as an unprivileged user: the service handles patient data and has no need
# for root inside the container.
RUN useradd --create-home --uid 10001 medicalrag \
    && chown -R medicalrag:medicalrag /app
USER medicalrag

EXPOSE 8000

# `/health` is liveness.  Readiness (dependencies wired, index not degraded) is a
# separate probe so the orchestrator does not restart-loop on a downstream outage.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Migrations are run by the deploy pipeline (or an init container), not here, so
# that several replicas cannot race each other on `alembic upgrade head`.
CMD ["medical-rag-api"]
