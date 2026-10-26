@echo off
rem ============================================================
rem  MSYT public tunnel - stop  (ASCII only: cmd.exe mis-parses
rem  non-ASCII bytes).
rem ============================================================
title MSYT public tunnel - stop
cd /d "%~dp0"
where pwsh >nul 2>&1
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0tunnel.ps1" stop
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tunnel.ps1" stop
)
echo.
pause
