FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS frontend-builder
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime
ARG COMMIT_SHA
LABEL org.opencontainers.image.revision=$COMMIT_SHA
ENV REVIEW_SOURCE_COMMIT=$COMMIT_SHA
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/opt/review/venv REVIEW_STATIC_DIR=/opt/review/static \
    PATH=/opt/review/venv/bin:$PATH APP_ENV=production QUEUE_DB_PATH=/data/review.db \
    HOME=/tmp/review-home XDG_CACHE_HOME=/tmp/cache TEAM_MEMORY_CACHE_DIR=/data/team-memory \
    REVIEW_ENABLE_REMEDIATION=false
WORKDIR /opt/review
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv==0.11.32 \
    && groupadd --gid 10001 review && useradd --uid 10001 --gid review --no-create-home review \
    && mkdir /data && chown review:review /data
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY .code-review.yaml ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable
COPY --from=frontend-builder /build/dist /opt/review/static
USER 10001:10001
WORKDIR /tmp
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready', timeout=4)"]
CMD ["uvicorn", "code_review_agent.webhook_server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
