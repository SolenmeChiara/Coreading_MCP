@echo off
cd /d "%~dp0"

echo.
echo  ========================================
echo    共读书房 Co-Reading Room
echo  ========================================
echo.

REM 设置用户名（可自定义）
if not defined COREADING_USER set COREADING_USER=Sol

REM 启动 Web 服务器（新窗口）
start "Co-Reading Web Server" cmd /k "cd /d "%~dp0" && set COREADING_USER=%COREADING_USER% && python web.py"

echo  Web 服务器启动中...

REM 健康检查轮询（最多等 15 秒）
set /a attempts=0
:healthcheck
set /a attempts+=1
if %attempts% gtr 15 (
    echo  启动超时，请检查 web.py 是否有错误
    pause
    exit /b 1
)
timeout /t 1 /nobreak >nul
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8765/api/books | findstr "200" >nul
if errorlevel 1 goto healthcheck

echo  Web 服务器已就绪！
echo.
echo  浏览器阅读: http://127.0.0.1:8765
echo  MCP 服务器:  由 Claude Desktop 自动启动 (stdio)
echo.

REM 打开浏览器
start http://127.0.0.1:8765

echo  按任意键关闭此窗口...
pause >nul
