@echo off
title MSYT Web UI - status
where pwsh >nul 2>&1
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0web.ps1" status
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0web.ps1" status
)
echo.
pause
