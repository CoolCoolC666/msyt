@echo off
rem ============================================================
rem  MSYT public tunnel - start  (ASCII only: cmd.exe mis-parses
rem  non-ASCII bytes). Keeps 2 Cloudflare quick tunnels alive and
rem  refreshes the member agent address file.
rem  Leave this window open; press Ctrl+C to stop.
rem ============================================================
title MSYT public tunnel - start
cd /d "%~dp0"
where pwsh >nul 2>&1
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0tunnel.ps1" start -Count 2 -Port 8765
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tunnel.ps1" start -Count 2 -Port 8765
)
echo.
echo tunnel stopped.
pause
