#!/usr/bin/env bash
# One-time RHEL 9 host bootstrap for rootless Podman deployment.
# Run from the repository root after cloning, or set REPO_DIR to the install path.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO_DIR"

echo "=== AML web — RHEL 9 Podman setup ==="
echo "Repository: $REPO_DIR"
echo ""

if ! command -v podman >/dev/null 2>&1; then
  echo "Installing podman and podman-compose (requires sudo)..."
  sudo dnf install -y podman podman-compose git
fi

if ! podman compose version >/dev/null 2>&1; then
  echo "ERROR: 'podman compose' is not available. Install podman-compose: sudo dnf install -y podman-compose"
  exit 1
fi

echo "Podman: $(podman --version)"
echo "Compose: $(podman compose version 2>/dev/null | head -1 || podman compose version)"
echo ""

if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
  echo "WARNING: XDG_RUNTIME_DIR is unset. Rootless Podman needs a logged-in user session."
fi

if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'yes'; then
  echo "Enabling systemd linger for $USER (services survive logout/reboot)..."
  loginctl enable-linger "$USER"
else
  echo "Systemd linger already enabled for $USER."
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit SESSION_SECRET, POSTGRES_PASSWORD, and ENV=production before going live."
else
  echo ".env already exists; not overwriting."
fi

if grep -qE '^ALLOW_INSECURE_DEV=true' .env 2>/dev/null && ! grep -qE '^ENV=production' .env 2>/dev/null; then
  echo ""
  echo "WARNING: ALLOW_INSECURE_DEV=true without ENV=production — acceptable for dev only."
fi

if ! grep -qE '^SESSION_SECRET=.{32,}' .env 2>/dev/null; then
  echo ""
  echo "WARNING: Set a 32+ character SESSION_SECRET in .env before production use."
  echo "  python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
fi

export COMPOSE_HTTP_TIMEOUT="${COMPOSE_HTTP_TIMEOUT:-300}"

echo ""
echo "Pulling Postgres image..."
podman compose pull db

echo ""
echo "Building and starting full stack (web + db)..."
podman compose up --build -d

echo ""
podman compose ps

echo ""
echo "Health check:"
if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "  http://127.0.0.1:8000/health OK"
else
  echo "  Waiting for web container (migrations may take a moment)..."
  sleep 5
  curl -sf http://127.0.0.1:8000/health && echo "  OK" || echo "  Not ready yet — run: podman compose logs -f web"
fi

echo ""
echo "Next steps:"
echo "  1. Create admin: podman exec -it card_cashin_web python -m app.scripts.create_admin myuser"
echo "  2. Optional systemd auto-start: see deploy/rhel9/README.md"
echo "  3. Optional HTTPS: deploy/rhel9/nginx-aml-web.conf.example"
