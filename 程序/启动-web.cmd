@echo off
rem ============================================================
rem  MSYT Web UI - start   (ASCII only: cmd.exe mis-parses non-ASCII)
rem  Starts the local service AND the public tunnel.
rem  The public domain is read from data\tunnel-domain.txt,
rem  so members can reach this site from the internet.
rem  Want local only? Use the other .cmd in this folder.
rem ============================================================
title MSYT Web UI - start
where pwsh >nul 2>&1
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0web.ps1" start
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0web.ps1" start
)
if errorlevel 1 (
  echo.
  echo Start failed. See the error above.
  pause
)