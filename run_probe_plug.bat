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
echo   Smart plug: enumerate MIoT properties  (READ-ONLY)
echo ============================================================
echo  Nothing is switched -- this only reads the property table,
echo  so it is safe to run while the plug powers the charger.
echo.
echo  Needs charge_guard.json "plug.ip" + a token in
echo  charge_guard.local.json (get both with run_miiocli_cloud.bat).
echo.
echo  Expected for Mijia Smart Plug 3 (cuco.plug.v3):
echo    siid 2  piid 1   switch          -^> plug.on_siid / on_piid
echo    siid 11 piid 2   live watts      -^> plug.power_siid / power_piid
echo    siid 11 piid 1   energy 0.01 kWh
echo  Confirm on YOUR device -- model numbers differ.
echo ============================================================
echo.
"%VENV%\Scripts\python.exe" -X utf8 charge_guard.py --probe-plug
echo.
echo ============================================================
echo  After filling on_siid/on_piid/power_siid/power_piid:
echo    python -X utf8 charge_guard.py --dry-run --once     (read only)
echo    python -X utf8 charge_guard.py                      (live)
echo ============================================================
pause
