@echo off
rem ============================================================
rem  MSYT Web UI - start (local only, NO public tunnel)
rem  ASCII only: cmd.exe mis-parses non-ASCII bytes.
rem  Use this when you do NOT want the site exposed to the internet.
rem ============================================================
title MSYT Web UI - start (local only)
where pwsh >nul 2>&1
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0web.ps1" start -NoTunnel
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0web.ps1" start -NoTunnel
)
if errorlevel 1 (
  echo.
  echo Start failed. See the error above.
  pause
)