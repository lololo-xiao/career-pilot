FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM node:22-slim AS codex

ARG CODEX_VERSION=0.139.0
RUN npm install --global "@openai/codex@${CODEX_VERSION}"

FROM python:3.12-slim AS runtime

ARG TARGETARCH
ARG TECTONIC_VERSION=0.16.9
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/opt/hermes/bin:/app/.venv/bin:$PATH" \
    CAREER_COMPANION_HOME=/app/.data/companion \
    CAREER_COMPANION_DISTRIBUTION_PATH=/app/agent-profile \
    AUTH_DATABASE_PATH=/app/.data/careerpilot.db

COPY --from=uv /uv /uvx /bin/
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/bin/codex /usr/local/bin/codex
COPY --from=codex /usr/local/lib/node_modules/@openai/codex /usr/local/lib/node_modules/@openai/codex

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY installers/tectonic-0.16.9.sha256 /tmp/tectonic.sha256
RUN case "$TARGETARCH" in \
      amd64) asset="tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-gnu.tar.gz" ;; \
      arm64) asset="tectonic-${TECTONIC_VERSION}-aarch64-unknown-linux-musl.tar.gz" ;; \
      *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && expected="$(awk -v asset="$asset" '$2 == asset {print $1}' /tmp/tectonic.sha256)" \
    && curl --proto '=https' --tlsv1.2 --fail --location \
      --output "/tmp/$asset" \
      "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/$asset" \
    && echo "$expected  /tmp/$asset" | sha256sum --check --strict \
    && tar -xzf "/tmp/$asset" -C /usr/local/bin \
    && chmod 0755 /usr/local/bin/tectonic \
    && rm -f "/tmp/$asset" /tmp/tectonic.sha256

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY agent-profile/requirements-hermes.txt /tmp/requirements-hermes.txt
RUN uv sync --frozen --no-dev --extra companion --no-install-project \
    && python -m venv /opt/hermes \
    && /opt/hermes/bin/pip install --no-cache-dir -r /tmp/requirements-hermes.txt

COPY app ./app
COPY career_companion ./career_companion
COPY job_pipeline ./job_pipeline
COPY migrations ./migrations
COPY agent-profile ./agent-profile
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --extra companion \
    && python -m playwright install --with-deps chromium

RUN useradd --create-home --uid 10001 careerpilot \
    && mkdir -p /app/.data /app/.cache \
    && chown -R careerpilot:careerpilot /app /home/careerpilot /ms-playwright
USER careerpilot

# Bake the local retrieval model into a user-readable cache for stable first use.
RUN python -c "from app.retrieval import _get_local_embedder; _get_local_embedder()(['CareerPilot warmup'])"

EXPOSE 8000

CMD ["fastapi", "run", "app/main.py", "--host", "0.0.0.0", "--port", "8000"]
