# syntax=docker/dockerfile:1
# Garmin MCP, official OAuth mode (GARMIN_AUTH_MODE=oauth). One image, one data volume.
# Python dependencies come from uv.lock (--locked fails the build if it is stale), so a rebuild
# never pulls a new major behind our back. Base images are tags; pin by digest for byte-identical builds.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# Dependencies first, so code changes reuse this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="garmin-mcp" \
      org.opencontainers.image.source="https://github.com/Sinfjell/garmin-mcp" \
      org.opencontainers.image.version="${VERSION}"
RUN useradd --system --uid 10001 --home-dir /data --no-create-home app \
    && mkdir /data && chown app:app /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    GARMIN_AUTH_MODE=oauth \
    GARMIN_OAUTH_TOKEN_ROOT=/data \
    GARMIN_OAUTH_PATH_PREFIX=/garmin-oauth
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; p = '/' + os.environ['GARMIN_OAUTH_PATH_PREFIX'].strip().strip('/'); urllib.request.urlopen(f'http://127.0.0.1:8000{p}/healthz', timeout=4)"]
ENTRYPOINT ["garmin-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
