# Sixto gateway production image. Build on the Intel Mac mini: linux/amd64.
FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dedicated non-root user (uid 1000). /app/data below is owned by it, and the named volume
# mounted there takes that ownership and mode on first use.
RUN groupadd --system --gid 1000 gateway \
    && useradd --system --uid 1000 --gid gateway --home-dir /app --shell /usr/sbin/nologin gateway

WORKDIR /app

COPY pyproject.toml README.md ./
COPY sixto_gateway ./sixto_gateway
# git is needed only to install t6 from its git tag
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && pip install . \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/data \
    && chown gateway:gateway /app/data \
    && chmod 0700 /app/data

USER gateway
EXPOSE 8780

# Healthy = a Telegram poll completed in the last 5 minutes and the database is writable
# (503 otherwise, which makes urlopen raise). Uses the default port; change both if you move it.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8780/healthz', timeout=5)"]
ENTRYPOINT ["python", "-m", "sixto_gateway", "--config", "/app/config.yaml"]
