@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found on PATH.
    echo.
    pause
    exit /b
)

python "G:\docker\tools\data_safety_check.py"

echo.
echo ---------------------------------------------
echo  Press any key to close this window.
echo ---------------------------------------------
pause >nul
