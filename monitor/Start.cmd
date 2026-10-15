@echo off
setlocal
cd /d "%~dp0"
set "CONSOLE_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%CONSOLE_PY%" set "CONSOLE_PY=%~dp0..\..\Python\python.exe"
if not exist "%CONSOLE_PY%" (
  echo Python not found. Run Update.cmd from the separately extracted console ZIP.
  pause
  exit /b 1
)
"%CONSOLE_PY%" "%~dp0monitor.py" --open-console
if errorlevel 1 pause
