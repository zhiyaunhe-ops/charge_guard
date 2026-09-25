@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "VENV=C:\Users\zhiya\.workbuddy\binaries\python\envs\default"
if not exist "%VENV%\Scripts\miiocli.exe" (
  echo [ERROR] miiocli not found. Install python-miio into the project venv:
  echo   "%VENV%\Scripts\python.exe" -m pip install -U python-miio
  echo.
  pause
  exit /b 1
)
echo ============================================================
echo   Mi Cloud: list devices with IP + token  (miiocli cloud)
echo ============================================================
echo  1) It asks for your Xiaomi account email/phone + password.
echo  2) Xiaomi may ask for a verification code again (risk control).
echo  3) In the output, find the SMART PLUG entry and copy its
echo     "ip" and "token" values.
echo.
echo  Where to put them:
echo    ip    -^> charge_guard.json      "plug": { "ip": "..." }
echo    token -^> charge_guard.local.json "plug": { "token": "..." }   (never commit it)
echo.
echo  The token is a secret. Do NOT paste it into issues or commit it.
echo ============================================================
echo.
"%VENV%\Scripts\miiocli.exe" cloud
echo.
echo ============================================================
echo  Next step: double-click run_probe_plug.bat
echo ============================================================
pause
