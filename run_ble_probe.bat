@echo off
cd /d "%~dp0"
chcp 65001 >nul
set "VENV=C:\Users\zhiya\.workbuddy\binaries\python\envs\default"
if not exist "%VENV%\Scripts\python.exe" (
  echo [ERROR] venv python not found: %VENV%\Scripts\python.exe
  echo.
  pause
  exit /b 1
)
echo ==== BLE probe: about 40 seconds, please wait ====
echo.
"%VENV%\Scripts\python.exe" -u -X utf8 ble_probe.py
echo.
echo ==== report ====
type ble_probe_report.txt
echo.
pause
