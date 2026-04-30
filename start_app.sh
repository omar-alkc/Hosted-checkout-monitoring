#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi

if ! "$PY" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)" 2>/dev/null; then
  echo "Expected Python 3.13 (see pyproject.toml and README)."
  "$PY" --version 2>&1 || true
  echo "Tip: python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

echo ""
echo "=== AML web UI (Linux) ==="
echo "Use http (not https). Try:"
echo "  http://127.0.0.1:8000/health   (should show {\"ok\":true})"
echo "  http://127.0.0.1:8000/       (redirects after login)"
echo ""
echo "For production bind only locally and put Nginx/Caddy on :80/:443."

exec "$PY" -m uvicorn app.main:app --reload --reload-exclude ".venv" --host 127.0.0.1 --port 8000
