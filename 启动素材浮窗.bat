@echo off
REM ---------------------------------------------------------------
REM  Moge material float window (desktop always-on-top window).
REM  KEEP THIS FILE PURE ASCII -- no Chinese in here. See the long
REM  explanation in the sibling launcher / backend/main.py: cmd decodes
REM  .bat
REM  bytes with the system ANSI codepage, so UTF-8 Chinese would be
REM  turned into garbage and split into bogus commands.
REM
REM  Chinese text lives in floatwin/readme.txt (UTF-8) and is printed
REM  with `type` -- that copies the file's raw bytes out, and since we
REM  switched the console to 65001 it displays correctly. The file
REM  name in this bat MUST stay ASCII (a Chinese path here would hit
REM  exactly the same decoding problem).
REM ---------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"

REM  Normal path: start it detached and leave. The window itself shows
REM  a "starting up..." splash, so there is nothing worth reading in
REM  this console anyway (it closes instantly).
if exist ".venv\Scripts\pythonw.exe" goto run

REM  Only reached when the venv is missing -- keep the window open and
REM  explain what to do.
echo.
type "floatwin\readme.txt"
echo.
pause
exit /b 1

:run
start "" ".venv\Scripts\pythonw.exe" "floatwin\host.py"
exit /b 0
