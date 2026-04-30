@echo off
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,13) else 1)" 2>nul
if errorlevel 1 (
  echo Expected Python 3.13 ^(see repo pyproject.toml^).
  "%PY%" --version
  echo Tip: create a venv in this folder: py -3.13 -m venv .venv
  exit /b 1
)

echo.
echo === AML web UI ===
echo Use **http** (not https). Try in order:
echo   http://127.0.0.1:8000/health   ^(should show {"ok":true}^)
echo   http://127.0.0.1:8000/         ^(redirects to detections^)
echo.
echo If the browser cannot connect:
echo   - Stop other apps using port 8000, or change the port in this file.
echo   - Windows: allow Python when the firewall prompts; binding 127.0.0.1 avoids "public network" blocks.
echo   - Cursor remote/SSH: use Ports panel and "Forward a Port" for 8000, then open the forwarded URL.
echo.
"%PY%" -m uvicorn app.main:app --reload --reload-exclude ".venv" --host 127.0.0.1 --port 8000
