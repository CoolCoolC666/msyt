@echo off
rem ============================================================
rem  Rebuild the report docx (md -> docx) and overwrite the
rem  locked one as soon as WPS/Word releases the file.
rem  ASCII only: cmd.exe mis-parses non-ASCII bytes.
rem  All Chinese paths are handled inside the Python script.
rem ============================================================
title Rebuild report docx
cd /d "%~dp0"
python _tools\rebuild_report_docx.py --wait 180
echo.
pause
