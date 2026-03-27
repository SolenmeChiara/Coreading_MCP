@echo off
cd /d "%~dp0"

echo.
echo  ========================================
echo    共读书房 Co-Reading Room
echo    Streamable HTTP 模式
echo  ========================================
echo.

REM 设置用户名（可自定义）
if not defined COREADING_USER set COREADING_USER=Sol

REM MCP 服务器 host/port（默认监听所有网卡，方便手机访问）
if not defined MCP_HOST set MCP_HOST=0.0.0.0
if not defined MCP_PORT set MCP_PORT=8766

REM 启动 Web 服务器（新窗口，监听所有网卡）
start "Co-Reading Web Server" cmd /k "cd /d "%~dp0" && set COREADING_USER=%COREADING_USER% && set HTTP_HOST=0.0.0.0 && python web.py"

REM 启动 MCP HTTP 服务器（新窗口）
start "Co-Reading MCP Server" cmd /k "cd /d "%~dp0" && set COREADING_USER=%COREADING_USER% && set MCP_HOST=%MCP_HOST% && set MCP_PORT=%MCP_PORT% && python server.py --http"

echo  Web 服务器启动中...

REM 健康检查（等 Web 就绪）
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
echo  本机浏览器:  http://127.0.0.1:8765
echo  手机浏览器:  http://100.112.25.69:8765
echo  MCP 端点:    http://100.112.25.69:%MCP_PORT%/mcp
echo.
echo  手机 Claude App 配置:
echo    claude.ai 添加 Custom Connector
echo    URL: http://100.112.25.69:%MCP_PORT%/mcp
echo    传输: streamable-http
echo.

REM 打开浏览器
start http://127.0.0.1:8765

echo  按任意键关闭此窗口...
pause >nul
