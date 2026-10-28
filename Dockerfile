# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Optional combined CA bundle for managed environments with an HTTPS proxy.
# The certificate is mounted for the build only and is never copied into a layer.
RUN --mount=type=secret,id=proxy_ca \
    set -eu; \
    if [ -f /run/secrets/proxy_ca ]; then \
      apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update; \
      apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends ca-certificates ffmpeg; \
    else \
      apt-get update && apt-get install -y --no-install-recommends ca-certificates ffmpeg; \
    fi \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 publisher \
    && useradd --uid 10001 --gid publisher --no-create-home publisher \
    && mkdir -p /data/uploads \
    && chown -R publisher:publisher /data

COPY pyproject.toml requirements.lock README.md LICENSE ./
COPY src ./src

RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -c requirements.lock .; \
    else \
      pip install --no-cache-dir -c requirements.lock .; \
    fi

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).close()"

CMD ["uvicorn", "instagram_publisher.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
