@echo off
setlocal
cd /d "%~dp0"
set "MONITOR_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%MONITOR_PY%" set "MONITOR_PY=%~dp0..\..\Python\python.exe"
if not exist "%MONITOR_PY%" (
  echo Python not found. Run this file from the existing app\monitor directory.
  pause
  exit /b 1
)
set "MONITOR_REPORT=%USERPROFILE%\Downloads\wind-relay-diagnostics-%RANDOM%-%RANDOM%.zip"
"%MONITOR_PY%" "%~dp0monitor.py" --export-diagnostics --output "%MONITOR_REPORT%"
if errorlevel 1 (
  echo Diagnostic export failed. Keep this message and check file permissions.
  pause
  exit /b 1
)
explorer.exe "%USERPROFILE%\Downloads"
echo Send the diagnostic ZIP together with the time and action when the issue occurred.
pause
