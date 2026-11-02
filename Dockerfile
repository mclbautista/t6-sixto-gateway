# Sixto gateway production image. Build on the Intel Mac mini: linux/amd64.
FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dedicated non-root user. UID 1000 so the host's ./data (chmod 0700) can be owned by the same
# id: `sudo chown 1000:1000 data`.
RUN groupadd --system --gid 1000 gateway \
    && useradd --system --uid 1000 --gid gateway --home-dir /app --shell /usr/sbin/nologin gateway

WORKDIR /app

COPY pyproject.toml README.md ./
COPY sixto_gateway ./sixto_gateway
RUN pip install . \
    && mkdir -p /app/data \
    && chown gateway:gateway /app/data \
    && chmod 0700 /app/data

USER gateway
EXPOSE 8780

# A /healthz health check is added with the rest of the module API (spec section 7).
ENTRYPOINT ["python", "-m", "sixto_gateway", "--config", "/app/config.yaml"]
