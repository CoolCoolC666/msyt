@echo off
rem ============================================================
rem  Start the msyt public tunnel (Cloudflare quick tunnel).
rem  ASCII only: cmd.exe mis-parses non-ASCII bytes.
rem  It keeps 2 tunnels alive and refreshes the member agent
rem  address file. Press Ctrl+C in this window to stop.
rem ============================================================
title msyt public tunnel
cd /d "%~dp0"
python _tools\tunnel_ctl.py start
echo.
echo tunnel stopped.
pause
