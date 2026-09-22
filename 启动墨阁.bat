@echo off
chcp 65001 >nul
cd /d G:\docker\moge
echo ================================
echo   墨阁 · 启动中
echo ================================
echo.
echo   启动后请在浏览器打开：
echo   http://localhost:8000
echo.
echo   关闭这个窗口 = 停止服务
echo ================================
echo.
.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000 --reload
pause
