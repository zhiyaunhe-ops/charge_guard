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
echo  2) If a CAPTCHA appears, open http://127.0.0.1:31415 in a browser and
echo     TYPE the code here (into this window), then press Enter.
echo  3) If a verification code is asked (SMS for phone-registered accounts,
echo     mail for email-registered ones), TYPE it here as well.
echo     The script now SENDS the code itself -- look for the lines
echo     "trigger verify..." and "send...Ticket" to see what the server said.
echo     Only the NEWEST code counts -- older ones from earlier runs are dead.
echo     Note: that is Xiaomi's risk check, NOT your account's 2FA switch --
echo     it can trigger even when 2FA is off.
echo     Driving it from a script instead of a human? The same prompt also
echo     accepts the value written into plugin_out\*.txt (path is printed).
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
