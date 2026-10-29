@echo off
rem ============================================================
rem  MSYT site watcher - start   (ASCII only: cmd.exe mis-parses non-ASCII)
rem  Watches whether the upstream forum is reachable and, when it
rem  comes back, settles the pending "short-posts" backlog.
rem  Manual start only -- never registered as an autostart service.
rem ============================================================
title MSYT site watcher - start
where pwsh >nul 2>&1
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0site-watch.ps1" start
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0site-watch.ps1" start
)
if errorlevel 1 (
  echo.
  echo Start failed. See the error above.
  pause
)
