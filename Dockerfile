# syntax=docker/dockerfile:1.7
# Titanic MCP Server - container image (spec section 11, CON-01 to CON-08).
# Stage 1 installs Python and the packages, stage 2 downloads and verifies the data, stage 3 is the runtime.

ARG DB_COMMIT=867f5bae5c0b53b0035bacb671ae1d3b07bc18ce
ARG DB_SHA256=aa9cf7683387fefae3759a6d51fd03c917836fdc9c61299ffea15de5bf5f327d

# ---- stage 1: build ---------------------------------------------------------------------------------
FROM debian:trixie-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed
# pip and ensurepip are not needed at runtime (uv installs all packages at build time).
# Removing them also removes the old packages that pip vendors (Trivy finding CVE-2025-47273).
RUN uv python install 3.12 \
 && rm -rf /python/cpython-*/lib/python3.12/site-packages/pip \
           /python/cpython-*/lib/python3.12/site-packages/pip-*.dist-info \
           /python/cpython-*/lib/python3.12/ensurepip \
           /python/cpython-*/bin/pip*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

# ---- stage 2: data (DS-01: pinned commit and checksum) ----------------------------------------------
FROM debian:trixie-slim AS data
ARG DB_COMMIT
ARG DB_SHA256
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL --retry 3 -o /titanic.db \
      "https://raw.githubusercontent.com/davidjamesknight/SQLite_databases_for_learning_data_science/${DB_COMMIT}/titanic.db" \
 && echo "${DB_SHA256}  /titanic.db" | sha256sum -c - \
 && chmod 0444 /titanic.db

# ---- stage 3: runtime (CON-02: no shell, no package manager) ----------------------------------------
FROM gcr.io/distroless/cc-debian13:nonroot AS runtime
ARG DB_SHA256
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
ARG VERSION=1.0.0
LABEL org.opencontainers.image.title="titanic-mcp" \
      org.opencontainers.image.description="MCP server with governed access to the Titanic database" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}"
COPY --from=build /python /python
COPY --from=build /app/.venv /app/.venv
COPY --from=data /titanic.db /app/data/titanic.db
COPY policy/policy.yaml /etc/tmcp/policy.yaml
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMCP_DB_PATH=/app/data/titanic.db \
    TMCP_DB_SHA256=${DB_SHA256} \
    TMCP_POLICY_PATH=/etc/tmcp/policy.yaml \
    GIT_COMMIT=${GIT_COMMIT} \
    BUILD_DATE=${BUILD_DATE}
# CON-03: non-root user. All files are owned by root and read-only for this user.
USER 10001:10001
EXPOSE 8000 9090
HEALTHCHECK --interval=30s --timeout=3s CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz/live', timeout=2)"]
ENTRYPOINT ["/app/.venv/bin/titanic-mcp"]
