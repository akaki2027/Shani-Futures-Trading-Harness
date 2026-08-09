# Shani — API and CLI.
#
# The portal is not built into this image; it is a separate Node service, and
# most people running Shani in a container want the API and webhook receiver
# rather than the UI. See docker-compose.yml for running both.
#
# Plane B (TradingView Desktop) cannot work from inside a container — it needs
# to reach a desktop application on your machine. Plane A and Plane C work fine.

FROM python:3.13-slim AS base

# uv, for the same lockfile-exact installs used in development.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, as their own layer, so a code change does not reinstall
# the world.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY shani/ ./shani/
RUN uv sync --frozen --no-dev

# Non-root. The journal is mounted from the host, so the container has no
# business running with more privilege than it needs.
RUN useradd --create-home --uid 10001 shani \
    && mkdir -p /data \
    && chown -R shani:shani /data /app
USER shani

# Persist the journal outside the container. Losing it on `docker rm` would mean
# losing the playbook, which is the entire point of the tool.
ENV SHANI_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8420/health')"

# 0.0.0.0 inside the container is correct — the container boundary is the
# isolation, and compose only publishes the port to loopback on the host.
CMD ["uv", "run", "shani", "serve", "--host", "0.0.0.0", "--port", "8420"]
