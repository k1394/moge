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
echo   只监听本机（127.0.0.1），同一个局域网里别的机器访问不到。
echo   关闭这个窗口 = 停止服务
echo ================================
echo.
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
pause
