#!/bin/sh
set -e
cd /app
python -m alembic upgrade head
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1,172.16.0.0/12}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS"
