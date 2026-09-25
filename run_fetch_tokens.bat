@echo off
chcp 65001 >nul
cd /d "%~dp0third_party\xiaomi-ad1204-python"
set "PY=C:\Users\zhiya\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] venv python not found:
  echo   %PY%
  echo.
  pause
  exit /b 1
)
echo ============================================================
echo   Xiaomi cloud token fetch  (region: cn)
echo ============================================================
echo  1) It will ask for your Xiaomi account email/phone + password.
echo  2) If a CAPTCHA appears, it opens http://127.0.0.1:31415
echo     in your browser -- read the code there, type it back here.
echo  3) If 2FA is on, check your mail for a code and type it here.
echo.
echo  At the end, look for the line marked:
echo     njcuk.fitting.ad1204   CUKTECH 10 GaN Charger Ultra
echo  Copy its  address:  and  token:  values.
echo ============================================================
echo.
"%PY%" -X utf8 fetch_tokens.py --region cn
echo.
echo ============================================================
echo  Done. Copy address + token of njcuk.fitting.ad1204.
echo  Do NOT commit the token anywhere.
echo ============================================================
pause
