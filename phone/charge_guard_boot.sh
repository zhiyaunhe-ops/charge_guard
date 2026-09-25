#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot autostart -- open the Termux:Boot app once so the system records it.
termux-wake-lock
cd "$HOME/charge_guard" || exit 1
nohup python charge_guard_phone.py >> /sdcard/charge_guard/guard.out 2>&1 &
