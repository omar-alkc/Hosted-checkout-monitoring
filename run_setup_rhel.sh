#!/usr/bin/env bash
# RHEL / Linux setup: Podman Compose (db-only for host Python, or full stack).
set -euo pipefail

cd "$(dirname "$0")"

FULL_STACK=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)
      FULL_STACK=true
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--full]"
      echo "  default   Start Postgres only (host port 15433); run alembic if .venv exists"
      echo "  --full    Build and start web + db (production stack on RHEL 9)"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if ! command -v podman >/dev/null 2>&1; then
  echo "ERROR: podman not found. Install: sudo dnf install -y podman podman-compose"
  exit 1
fi

if ! podman compose version >/dev/null 2>&1; then
  echo "ERROR: 'podman compose' not available. Install: sudo dnf install -y podman-compose"
  exit 1
fi

export COMPOSE_HTTP_TIMEOUT="${COMPOSE_HTTP_TIMEOUT:-300}"

if [[ "$FULL_STACK" == true ]]; then
  echo "[1/1] Starting full stack (web + db)..."
  podman compose up --build -d
  podman compose ps
  echo ""
  echo "Open http://127.0.0.1:8000/health"
  echo "Create admin: podman exec -it card_cashin_web python -m app.scripts.create_admin myuser"
  exit 0
fi

if [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi

echo "[1/3] Python dependencies..."
if ! "$PY" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)" 2>/dev/null; then
  echo "Expected Python 3.13 for host-run app (see pyproject.toml)."
  "$PY" --version 2>&1 || true
  echo "Tip: python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  echo "Or use full Podman stack: $0 --full"
  exit 1
fi

"$PY" -m pip install -r requirements.txt -q

echo "[2/3] Starting Postgres only (Podman Compose service db, host port 15433)..."
echo "Full stack on RHEL: $0 --full   or   podman compose up --build -d"
podman compose pull db
if ! podman compose up -d --wait db; then
  echo ""
  echo "Podman Compose failed. Ensure podman socket is running and you are logged in."
  echo "If a container name conflicts: podman compose down"
  echo "If --wait is unsupported: podman compose up -d db && sleep 15"
  exit 1
fi

echo "Postgres is healthy."

echo "[3/3] Alembic migrations..."
if ! "$PY" -m alembic upgrade head; then
  echo ""
  echo "If password authentication failed, reset volume and retry:"
  echo "  podman compose down -v"
  echo "  $0"
  exit 1
fi

echo ""
echo "Done. Start the app:"
echo "  ./start_app.sh"
