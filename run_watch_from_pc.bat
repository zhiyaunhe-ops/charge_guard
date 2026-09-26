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
echo   PC-side watchdog for the phone guard
echo ============================================================
echo  Why: when MIUI force-stops Termux, Android cancels its
echo  JobScheduler jobs and the app cannot restart itself.
echo  Only an external "am start" can revive it -- this script
echo  does that over ADB, and Termux's ~/.bashrc hook then
echo  brings the guard back up.
echo.
echo  Fallback: if the guard cannot be revived and we can still
echo  read the battery, it will flip the plug by level
echo  (^<= resume_at -^> power ON, ^>= stop_at -^> power OFF).
echo.
echo  Needs the phone's WIRELESS DEBUGGING to be on.
echo  Keep this window open (or run it under a scheduler).
echo  Log: phone\watch_from_pc.log       Stop: Ctrl-C
echo ============================================================
echo.
"%VENV%\Scripts\python.exe" -X utf8 phone\watch_from_pc.py --interval 300
echo.
pause
