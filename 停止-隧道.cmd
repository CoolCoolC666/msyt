@echo off
rem ============================================================
rem  Stop the msyt public tunnel (Cloudflare quick tunnel).
rem  ASCII only: cmd.exe mis-parses non-ASCII bytes.
rem ============================================================
title msyt public tunnel - stop
cd /d "%~dp0"
python _tools\tunnel_ctl.py stop
echo.
pause
