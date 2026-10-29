@echo off
rem ============================================================
rem  MSYT site watcher - stop   (ASCII only: cmd.exe mis-parses non-ASCII)
rem ============================================================
title MSYT site watcher - stop
where pwsh >nul 2>&1
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0site-watch.ps1" stop
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0site-watch.ps1" stop
)
echo.
pause
