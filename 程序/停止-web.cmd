@echo off
rem ============================================================
rem  MSYT Web UI - stop   (ASCII only: cmd.exe mis-parses non-ASCII)
rem ============================================================
title MSYT Web UI - stop
where pwsh >nul 2>&1
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0web.ps1" stop
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0web.ps1" stop
)
echo.
pause
