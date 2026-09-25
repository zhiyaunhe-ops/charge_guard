@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "VENV=C:\Users\zhiya\.workbuddy\binaries\python\envs\default"
if not exist "%VENV%\Scripts\python.exe" (
  echo [ERROR] venv python not found: %VENV%\Scripts\python.exe
  echo.
  pause
  exit /b 1
)
echo ============================================================
echo   Charger BLE check  (read-only -- no port is switched)
echo ============================================================
echo  Do these three things first, or it will fail:
echo    1) Wake the charger's screen -- it STOPS broadcasting when
echo       the display sleeps (upstream README says so explicitly).
echo    2) Put the charger within 1 m of this PC, phone still plugged
echo       into C1 (a load helps it keep advertising).
echo    3) Close the Mi Home app on the phone -- the charger accepts
echo       only ONE Bluetooth connection at a time.
echo ============================================================
echo.
echo [1/2] Scanning 20s: is it advertising at all?
"%VENV%\Scripts\python.exe" -u -X utf8 ble_probe.py
echo.
echo [2/2] Directed connect using the address from Mi Cloud + dump all props
"%VENV%\Scripts\python.exe" -X utf8 charge_guard.py --probe-charger
echo.
echo ------------------------------------------------------------
echo  Reports are saved next to this file:
echo    ble_probe_report.txt       (scan: was it advertising?)
echo    charger_probe_report.txt   (connect: what did it answer?)
echo.
echo  How to read the scan result:
echo   * saw nothing at all            - radio issue, or it is asleep
echo   * saw other devices only        - asleep / Mi Home holds it / too far
echo   * saw 3C:CD:73:37:B7:EE         - address is right; step 2 should work
echo   * charger-like name but a DIFFERENT address
echo                                   - cloud address is stale or rotating
echo ------------------------------------------------------------
pause
