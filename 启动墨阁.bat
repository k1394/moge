@echo off
REM ---------------------------------------------------------------
REM  Moge launcher.  KEEP THIS FILE PURE ASCII -- no Chinese here.
REM
REM  Why: cmd parses a .bat file's bytes with the system ANSI codepage
REM  (GBK on Chinese Windows), but this file is UTF-8. Chinese in an
REM  "echo" gets mangled, and some byte pairs break a line in two, so
REM  the user sees red lines like
REM      'xxx' is not recognized as an internal or external command
REM  chcp 65001 does NOT fix this: it only affects OUTPUT display, not
REM  how cmd decodes the file. (Confirmed by measurement 2026-09-30 --
REM  this file had been printing that red line on every launch.)
REM
REM  So: bat stays ASCII, all Chinese text is printed by
REM  backend/main.py at startup instead.
REM ---------------------------------------------------------------
chcp 65001 >nul
REM  Keep Python's stdout/stderr in UTF-8 so it matches the console
REM  codepage above. On a real console Python goes through the Windows
REM  console API and Chinese is fine anyway, but if the output is ever
REM  redirected to a file (or the float window launcher captures it),
REM  this is what keeps the two ends speaking the same encoding.
set PYTHONIOENCODING=utf-8
REM  Unbuffered. On a real console it does not matter, but the moment the
REM  output goes to a file (the float window launcher captures it into
REM  data\float_backend.log, and any "> log.txt" diagnosis does the same)
REM  Python switches to block buffering -- the banner and every print()
REM  then sit in the buffer and the log looks EMPTY until the buffer
REM  fills up. That is exactly the situation where she needs to see how
REM  far the startup got.
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
pause
