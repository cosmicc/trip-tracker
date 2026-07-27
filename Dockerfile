FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        gosu \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY trip_tracker ./trip_tracker
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint

RUN pip install . \
    && chmod +x /usr/local/bin/docker-entrypoint \
    && useradd --system --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data/backups \
    && chown -R app:app /app /data

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint"]
CMD ["uvicorn", "trip_tracker.app:app", "--host", "0.0.0.0", "--port", "8000"]
