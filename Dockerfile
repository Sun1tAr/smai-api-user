# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.22 AS uv

FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN groupadd --gid 10001 user \
    && useradd --uid 10001 --gid user --create-home user
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY --chmod=755 docker/entrypoint.sh ./docker/entrypoint.sh
USER user
EXPOSE 8000
# В slim-образе нет curl, поэтому проверяем /health стандартной библиотекой.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "-m", "user"]

FROM runtime AS test
USER root
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never RUFF_CACHE_DIR=/tmp/ruff-cache
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY tests ./tests
COPY Dockerfile docker-compose.yml ./
RUN uv sync --locked --no-editable
USER user
HEALTHCHECK NONE
ENTRYPOINT []
CMD ["pytest", "-p", "no:cacheprovider", "-q"]

FROM runtime AS production
