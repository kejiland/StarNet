@echo off
chcp 65001 >nul
title Product Info Grabber - Setup
cd /d "%~dp0"

set "SKIP_PAUSE=0"
if /I "%~1"=="from_launcher" set "SKIP_PAUSE=1"

echo.
echo ============================================
echo   Product Info Grabber - Environment Setup
echo ============================================
echo.

rem ===== [1/5] Detect a REAL Python =====
echo [1/5] Checking Python...
call "%~dp0_detect_python.cmd"
if errorlevel 1 goto install_python
echo   Python found: "%PYTHON%"
"%PYTHON%" --version
goto python_ready

:install_python
echo.
echo   Python not found. Trying to install it automatically (needs network)...
echo.
where winget >nul 2>nul
if errorlevel 1 goto direct_download
echo   [via winget] Installing Python 3.12 ...
winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
    echo   [via winget] failed, falling back to direct download...
    goto direct_download
)
call "%~dp0_detect_python.cmd"
if errorlevel 1 goto direct_download
echo   Python found: "%PYTHON%"
goto python_ready

:direct_download
echo   [via download] Downloading Python 3.12 from python.org ...
set "DLURL=https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
set "DLFILE=%TEMP%\python-3.12.8-amd64.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%DLURL%' -OutFile '%DLFILE%'"
if errorlevel 1 (
    echo.
    echo [ERROR] Download failed. Please install Python manually:
    echo         https://www.python.org/downloads/
    echo         Remember to check "Add Python to PATH".
    goto fail
)
echo   Installing Python silently...
start /wait "" "%DLFILE%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
if errorlevel 1 (
    echo [ERROR] Python installer failed.
    goto fail
)
call "%~dp0_detect_python.cmd"
if errorlevel 1 (
    echo.
    echo [ERROR] Python installed but not detected yet.
    echo         Close this window and reopen it, or restart the computer.
    goto fail
)
echo   Python found: "%PYTHON%"

:python_ready
echo.
echo [2/5] Checking current environment...
"%PYTHON%" env_setup.py --check-only
if %errorlevel%==0 goto all_ready

echo.
echo [3/5] Environment incomplete, installing missing components...
"%PYTHON%" env_setup.py
if errorlevel 1 (
    echo.
    echo [ERROR] Installation incomplete. Check network and retry.
    goto fail
)

echo.
echo [4/5] Verifying environment...
"%PYTHON%" -c "import requests, openpyxl, PIL, customtkinter, bs4, playwright; print('  All dependencies verified.')"
if errorlevel 1 (
    echo [ERROR] Verification failed. Re-run this script.
    goto fail
)

:all_ready
echo.
echo ============================================
echo   Environment is ready!
echo   Double-click the startup .bat to launch the tool.
echo ============================================
if "%SKIP_PAUSE%"=="0" pause
exit /b 0

:fail
if "%SKIP_PAUSE%"=="0" pause
exit /b 1