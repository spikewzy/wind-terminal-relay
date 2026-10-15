@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update.ps1"
if errorlevel 1 (
  echo Update failed. Keep the message above. Existing gateway credentials were not reset.
  pause
  exit /b 1
)
pause
