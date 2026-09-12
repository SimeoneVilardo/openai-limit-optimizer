# syntax=docker/dockerfile:1
# Pinned Codex CLI 0.154.0 on linux/amd64 via the official release
# binary (no Node toolchain needed at runtime or build time).
FROM --platform=linux/amd64 python:3.14-slim-bookworm

ENV CODEX_VERSION=0.154.0 \
    CODEX_TARBALL=codex-x86_64-unknown-linux-musl.tar.gz \
    CODEX_SHA256=d7e18b2597ae8f242f5f31ee9e90deef48dbc9edd634d9868fb6435d08c07f02 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Official digest from the GitHub release API (openai/codex release
# id 385887902, asset id 553706458, rust-v0.154.0): verified below
# with sha256sum before extraction. Fails the build on mismatch.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL -o /tmp/codex.tar.gz "https://github.com/openai/codex/releases/download/rust-v0.154.0/${CODEX_TARBALL}" \
    && echo "${CODEX_SHA256}  /tmp/codex.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/codex.tar.gz -C /tmp \
    && mv /tmp/codex-x86_64-unknown-linux-musl /usr/local/bin/codex \
    && chmod 0755 /usr/local/bin/codex \
    && rm -f /tmp/codex.tar.gz \
    && codex --version \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 65532 appuser \
    && useradd -u 65532 -g 65532 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app /data/codex \
    && chown -R 65532:65532 /app /data \
    && chmod 0755 /app && chmod 0700 /data/codex

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN python -c "import app.config, app.protocol, app.policy, app.state, app.send, app.main; print('imports ok')" \
    && chown -R 65532:65532 /app

# Verify the pinned CLI AFTER cleanup, as the runtime user.
RUN runuser -u appuser -- codex --version | grep -q "0.154.0"

USER 65532:65532
ENV CODEX_HOME=/data/codex

ENTRYPOINT ["python", "-m", "app.main"]
CMD ["daemon"]

HEALTHCHECK --interval=5m --timeout=30s --start-period=2m --retries=2 \
    CMD ["python", "-m", "app.main", "healthcheck"]
