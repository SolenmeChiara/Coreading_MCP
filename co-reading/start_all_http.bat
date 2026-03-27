@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set MYDIR=%~dp0

echo.
echo  ========================================
echo    Co-Reading Room - Full HTTP Mode
echo    Web + MCP + Playwright
echo  ========================================
echo.

if not defined COREADING_USER set COREADING_USER=Sol
if not defined MCP_HOST set MCP_HOST=0.0.0.0
if not defined MCP_PORT set MCP_PORT=8766

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765.*LISTENING" 2^>nul') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8766.*LISTENING" 2^>nul') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8767.*LISTENING" 2^>nul') do taskkill /F /PID %%a >nul 2>&1
timeout /t 1 /nobreak >nul

start "WebServer" cmd /k "cd /d %MYDIR% && set HTTP_HOST=0.0.0.0 && set COREADING_USER=%COREADING_USER% && python web.py"

start "MCPServer" cmd /k "cd /d %MYDIR% && set MCP_HOST=%MCP_HOST% && set MCP_PORT=%MCP_PORT% && set COREADING_USER=%COREADING_USER% && python server.py --http"

start "Playwright" cmd /k "npx @playwright/mcp@latest --port 8767 --host 0.0.0.0 --headless"

echo  Starting 3 servers...

set /a attempts=0
:healthcheck
set /a attempts+=1
if %attempts% gtr 20 (
    echo  Timeout - check server windows for errors
    pause
    exit /b 1
)
timeout /t 1 /nobreak >nul
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8765/api/books | findstr "200" >nul
if errorlevel 1 goto healthcheck

echo  Ready!
echo.
echo  === Local ===
echo  Browser:     http://127.0.0.1:8765
echo.
echo  === Remote ===
echo  Browser:     http://100.112.25.69:8765
echo  CoReading:   http://100.112.25.69:8766/mcp
echo  Playwright:  http://100.112.25.69:8767/mcp
echo.

start http://127.0.0.1:8765

echo  Press any key to close...
pause >nul
