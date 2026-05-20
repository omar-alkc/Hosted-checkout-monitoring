# Web UI + API (FastAPI). Uses scenario helpers at repo root (io_utils, scenarios, wallet_enrichment).
#
# Runtime config is injected by Compose / orchestrator (not copied from `.env` — see `.dockerignore`).
# Required: DATABASE_URL, and either SESSION_SECRET (32+ chars) or ALLOW_INSECURE_DEV=true for dev only.
# Entrypoint: `docker-entrypoint.sh` runs migrations then uvicorn with `--proxy-headers` and
# `--forwarded-allow-ips` (override with env `FORWARDED_ALLOW_IPS`).
FROM python:3.13-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic/ ./alembic/
COPY app/ ./app/
COPY io_utils.py scenarios.py wallet_enrichment.py ./

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

ENTRYPOINT ["/docker-entrypoint.sh"]
