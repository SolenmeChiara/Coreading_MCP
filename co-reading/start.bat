@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo  ========================================
echo    Co-Reading Room
echo  ========================================
echo.

if not defined COREADING_USER set COREADING_USER=Sol

REM start web server
start "WebServer" cmd /c "cd /d %~dp0 && set COREADING_USER=%COREADING_USER% && python web.py"

echo  Starting...

set /a attempts=0
:healthcheck
set /a attempts+=1
if %attempts% gtr 15 (
    echo  Timeout - check web.py for errors
    pause
    exit /b 1
)
timeout /t 1 /nobreak >nul
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8765/api/books | findstr "200" >nul
if errorlevel 1 goto healthcheck

echo  Ready!
echo.
echo  Browser: http://127.0.0.1:8765
echo  MCP:     Claude Desktop auto-starts (stdio)
echo.

start http://127.0.0.1:8765

echo  Press any key to close...
pause >nul
