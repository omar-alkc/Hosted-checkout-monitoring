@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM  run_setup_local.cmd  --  no Docker required
REM
REM  Prerequisites (do these once before running this script):
REM
REM  1. Install PostgreSQL 16 for Windows:
REM       winget install PostgreSQL.PostgreSQL.16
REM     (or download from https://www.postgresql.org/download/windows/)
REM     Remember the password you set for the 'postgres' user.
REM
REM  2. Create the application database (run once in a terminal):
REM       psql -U postgres -c "CREATE DATABASE aml_web;"
REM
REM  3. Copy .env.example to .env and set DATABASE_URL to:
REM       DATABASE_URL=postgresql://postgres:<your-password>@127.0.0.1:5432/aml_web
REM     Also set SESSION_SECRET to a long random string.
REM
REM  4. Install Python 3.13 if not already present:
REM       winget install Python.Python.3.13
REM     Then create a venv in this folder (once):
REM       py -3.13 -m venv .venv
REM ============================================================

REM -- Resolve Python (prefer .venv) ---------------------------
if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

REM -- Check Python version ------------------------------------
"%PY%" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,13) else 1)" 2>nul
if errorlevel 1 (
  echo.
  echo ERROR: Python 3.13 is required.
  "%PY%" --version 2>nul
  echo.
  echo Fix options:
  echo   a) winget install Python.Python.3.13
  echo   b) py -3.13 -m venv .venv   (if 3.13 is installed but not the default)
  exit /b 1
)

REM -- Check .env exists ----------------------------------------
if not exist "%~dp0.env" (
  echo.
  echo ERROR: .env file not found.
  echo Copy .env.example to .env and update DATABASE_URL:
  echo   DATABASE_URL=postgresql://postgres:^<password^>@127.0.0.1:5432/aml_web
  echo Also set SESSION_SECRET to a long random string.
  exit /b 1
)

REM -- Install Python dependencies ------------------------------
echo [1/2] Installing Python dependencies...
"%PY%" -m pip install -r requirements.txt -q
if errorlevel 1 (
  echo.
  echo pip install failed. Check your internet connection or proxy settings.
  exit /b 1
)
echo       Done.

REM -- Check PostgreSQL connectivity (via psycopg2 / DATABASE_URL) --
echo [2/2] Applying Alembic database migrations...
"%PY%" -m alembic upgrade head
if errorlevel 1 (
  echo.
  echo Alembic migration failed. Common causes:
  echo   - PostgreSQL is not running.  Start it:
  echo       net start postgresql-x64-16
  echo     (service name may differ -- check Services or use pg_ctl)
  echo   - Wrong credentials or database name in .env DATABASE_URL.
  echo   - Database 'aml_web' does not exist.  Create it:
  echo       psql -U postgres -c "CREATE DATABASE aml_web;"
  exit /b 1
)
echo       Migrations applied.

echo.
echo ============================================================
echo  Setup complete!
echo.
echo  Next steps:
echo.
echo  1. Create the first admin user (run once):
echo       "%PY%" -m app.scripts.create_admin adminuser "YourP@ssw0rd" "Admin"
echo.
echo  2. Start the app:
echo       start_app.cmd
echo     or:
echo       "%PY%" -m uvicorn app.main:app --reload --reload-exclude ".venv" --host 127.0.0.1 --port 8000
echo.
echo  3. Open in browser:
echo       http://127.0.0.1:8000/
echo ============================================================
endlocal
