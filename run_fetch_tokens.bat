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
echo  2) If a CAPTCHA appears, open http://127.0.0.1:31415 and write the code
echo     into plugin_out\captcha_code.txt (the script prints the exact path).
echo  3) If 2FA is on, a code is sent to you -- read it and write it into
echo     plugin_out\2fa_code.txt (the script prints the exact path and waits).
echo     Phone-registered accounts get an SMS; email-registered ones get mail.
echo     Only the NEWEST code counts -- older ones from earlier runs are dead.
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
