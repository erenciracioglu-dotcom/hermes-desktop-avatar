@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
set "PYTHONUNBUFFERED=1"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo venv missing: %PY%
  pause
  exit /b 1
)
echo Starting Hermes Desktop Avatar...
"%PY%" -u -m avatar
echo Exit code: %errorlevel%
pause
