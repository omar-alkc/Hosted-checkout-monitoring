@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,13) else 1)" 2>nul
if errorlevel 1 (
  echo Expected Python 3.13 ^(see repo pyproject.toml and README^).
  "%PY%" --version
  echo Install 3.13 or create a venv in this folder: py -3.13 -m venv .venv
  exit /b 1
)

echo [1/3] Python dependencies...
"%PY%" -m pip install -r requirements.txt -q
if errorlevel 1 exit /b 1

echo [2/3] Starting Postgres only ^(Docker Compose service `db`, host port 15433^)...
echo To run the **full stack** in Docker ^(web + db^): docker compose up --build -d
set COMPOSE_HTTP_TIMEOUT=300
docker compose pull db
if errorlevel 1 (
  echo.
  echo Docker image pull failed. Common causes: Docker Hub slow or blocked, VPN/proxy, or offline.
  echo Try again later, run: docker compose pull db
  echo Or skip Docker: set DATABASE_URL to your Postgres in .env then run: "%PY%" -m alembic upgrade head
  exit /b 1
)
docker compose up -d --wait db
if errorlevel 1 (
  echo.
  echo Docker Compose failed. Ensure Docker Desktop is running.
  echo If a container name conflicts, remove the old container or run: docker compose down
  echo If your Compose version is old, upgrade Docker Desktop or run: docker compose up -d db
  echo Then wait until Postgres is ready and run: "%PY%" -m alembic upgrade head
  exit /b 1
)

echo Postgres is healthy ^(compose --wait^).

echo [3/3] Alembic migrations...
"%PY%" -m alembic upgrade head
if errorlevel 1 (
  echo.
  echo If you see password authentication failed: remove stale data and retry:
  echo   docker compose down -v
  echo   .\run_setup.cmd
  exit /b 1
)

echo.
echo Done. Start the app:
echo   start_app.cmd
echo   ^(or: "%PY%" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000^)
endlocal
