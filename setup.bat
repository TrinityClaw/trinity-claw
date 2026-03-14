@echo off
setlocal EnableDelayedExpansion

echo.
echo    +======================================================+
echo    ^|       TrinityClaw AI Agent - Windows Setup           ^|
echo    +======================================================+
echo.

:: ── [1/3] Check Docker ────────────────────────────────────
echo    [1/3] Checking Docker Desktop...

docker --version >nul 2>&1
if errorlevel 1 goto no_docker

docker info >nul 2>&1
if errorlevel 1 goto start_docker

goto docker_ready

:no_docker
echo.
echo    Docker Desktop is not installed.
echo.
echo    +----------------------------------------------------+
echo    ^|   INSTALL DOCKER DESKTOP (it's free^)              ^|
echo    ^|                                                    ^|
echo    ^|   1. A browser window is opening now              ^|
echo    ^|   2. Click "Download for Windows"                 ^|
echo    ^|   3. Run the installer                            ^|
echo    ^|   4. Open Docker Desktop from the Start menu      ^|
echo    ^|   5. Wait for the whale icon in your taskbar      ^|
echo    ^|   6. Double-click setup.bat again                 ^|
echo    +----------------------------------------------------+
echo.
start https://www.docker.com/products/docker-desktop/
pause
exit /b 1

:start_docker
echo    Docker is installed but not running.
echo    Starting Docker Desktop - please wait...
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
echo    Waiting for Docker to start (up to 90 seconds)...
set /a TC_WAIT=0

:wait_loop
set /a TC_WAIT+=1
if !TC_WAIT! gtr 45 goto docker_timeout
timeout /t 2 /nobreak >nul
docker info >nul 2>&1
if errorlevel 1 goto wait_loop
goto docker_ready

:docker_timeout
echo.
echo    Docker did not start in time.
echo    Open Docker Desktop manually, wait for the whale icon
echo    in your taskbar, then double-click setup.bat again.
pause
exit /b 1

:docker_ready
echo    [OK] Docker is ready.
echo.

:: ── [2/3] Download files if not already present ──────────
echo    [2/3] Checking files...

if exist "docker-compose.yml" (
    echo    [OK] Files already present.
    goto run_installer
)

set "INSTALL_DIR=%USERPROFILE%\trinity-claw"
echo    Downloading TrinityClaw to %INSTALL_DIR%...

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

curl -fsSL -o "%TEMP%\trinity-claw.zip" "https://github.com/TrinityClaw/trinity-claw/archive/refs/heads/main.zip"
if errorlevel 1 (
    echo    [FAIL] Download failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo    Extracting...
powershell -NoProfile -Command "Expand-Archive -Path '%TEMP%\trinity-claw.zip' -DestinationPath '%TEMP%\tc-extract' -Force"
if errorlevel 1 (
    echo    [FAIL] Extraction failed.
    del "%TEMP%\trinity-claw.zip" 2>nul
    pause
    exit /b 1
)

xcopy /E /Y /Q "%TEMP%\tc-extract\trinity-claw-main\*" "%INSTALL_DIR%\" >nul
rmdir /S /Q "%TEMP%\tc-extract"
del "%TEMP%\trinity-claw.zip"

echo    [OK] Files ready at %INSTALL_DIR%
cd /d "%INSTALL_DIR%"

:: ── [3/3] Run installer ────────────────────────────────────
:run_installer
echo.
echo    [3/3] Running TrinityClaw installer...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "install.ps1"

endlocal
