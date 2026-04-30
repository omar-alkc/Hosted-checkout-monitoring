# Run from PowerShell at repository root:  .\run_setup.ps1
# (Do not use CMD's "cd /d" syntax in PowerShell.)

Set-Location $PSScriptRoot

Write-Host "[1/3] Python dependencies..."
python -m pip install -r requirements.txt -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/3] Starting Postgres only (Compose service 'db', host port 15433)..."
Write-Host "Full stack in Docker: docker compose up --build -d"
$env:COMPOSE_HTTP_TIMEOUT = "300"
docker compose pull db
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Docker image pull failed (registry timeout, offline, or proxy). Retry: docker compose pull db"
    Write-Host "Or set DATABASE_URL in .env to your Postgres, then: python -m alembic upgrade head"
    exit 1
}
docker compose up -d --wait db
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Docker Compose failed. If --wait is unsupported, run: docker compose up -d db then wait ~15s and run alembic again."
    exit 1
}

Write-Host "Postgres is healthy (compose --wait)."

Write-Host "[3/3] Alembic migrations..."
python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "If password authentication failed, reset the volume and retry:"
    Write-Host "  docker compose down -v"
    Write-Host "  .\run_setup.ps1"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Done. Start the app:"
Write-Host "  .\start_app.cmd"
Write-Host "  (or: python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000)"
