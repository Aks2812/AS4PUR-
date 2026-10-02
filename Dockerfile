# AS4PUR application image. Started by docker-compose.yml behind the nginx
# container, which terminates TLS; this container is never published directly.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # One process only (README, "Run"): pinned here so nothing inherited can raise it.
    WEB_CONCURRENCY=1 \
    AS4PUR_DATABASE_PATH=/app/data/as4pur.db \
    AS4PUR_UPLOAD_TEMP_DIR=/app/data/uploads_tmp

WORKDIR /app

# Dependencies first, so a code-only change reuses this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Non-root service user owning only the state directory.
RUN useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin as4pur \
    && mkdir -p /app/data \
    && chown as4pur:as4pur /app/data \
    && chmod +x deploy/docker/entrypoint.sh
USER as4pur

VOLUME ["/app/data"]
EXPOSE 8000

# No /health endpoint exists; /login answering 200 is the documented liveness check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=4).status == 200 else 1)"

ENTRYPOINT ["deploy/docker/entrypoint.sh"]
