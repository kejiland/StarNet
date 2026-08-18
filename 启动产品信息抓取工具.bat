@echo off
rem ===== 0) Hidden re-launch: run this script with a hidden console window =====
if "%1"=="--hidden" goto :run
if exist "%~dp0_hide_launch.vbs" (
    start "" /b wscript "%~dp0_hide_launch.vbs" "%~f0" --hidden
    exit /b 0
)
:run
chcp 65001 >nul
title Product Info Grabber
cd /d "%~dp0"

rem ===== 1) Fast path: real Python + all deps already present? =====
call "%~dp0_detect_python.cmd"
if not errorlevel 1 (
    "%PYTHON%" -c "import requests, openpyxl, PIL, customtkinter, bs4" >nul 2>nul
    if not errorlevel 1 goto launch
)

rem ===== 2) Otherwise run the setup script =====
echo [!] Environment is not ready yet, setting it up...
call "%~dp0setup_env.bat" from_launcher
if errorlevel 1 (
    echo.
    echo [ERROR] Environment setup failed. Please check network and retry.
    pause
    exit /b 1
)

rem ===== 3) Re-locate Python and final check =====
call "%~dp0_detect_python.cmd"
if errorlevel 1 (
    echo [ERROR] Python not found after setup.
    pause
    exit /b 1
)
"%PYTHON%" -c "import requests, openpyxl, PIL, customtkinter, bs4" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Dependencies are still missing. Run setup_env.bat manually.
    pause
    exit /b 1
)

:launch
echo Starting Product Info Grabber...
for %%i in ("%PYTHON%") do set "PYDIR=%%~dpi"
if exist "%PYDIR%pythonw.exe" (
    start "" /b "%PYDIR%pythonw.exe" main.py
) else (
    start "" "%PYTHON%" main.py
)
exit /b 0