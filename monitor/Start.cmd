@echo off
setlocal
cd /d "%~dp0"
set "MONITOR_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%MONITOR_PY%" set "MONITOR_PY=%~dp0..\..\Python\python.exe"
if not exist "%MONITOR_PY%" (
  echo Python not found. Extract monitor into C:\WindRelay\app\monitor
  pause
  exit /b 1
)
"%MONITOR_PY%" monitor.py
pause
