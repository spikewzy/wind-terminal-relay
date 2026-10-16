@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Upgrade-Gateway.ps1"
if errorlevel 1 (
  echo Upgrade stopped. Keep the message above; do not delete task or recovery files.
  pause
  exit /b 1
)
pause
