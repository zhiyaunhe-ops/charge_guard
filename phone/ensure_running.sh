#!/data/data/com.termux/files/usr/bin/sh
# 自愈看门狗：被 JobScheduler 定期调用；发现守护不在就拉起来。
# 关键点：守护被 MIUI 杀过也没关系 —— JobScheduler 会重新启动 Termux 进程来跑本脚本。
termux-wake-lock
LOG=/sdcard/charge_guard/watchdog.log
if pgrep -f charge_guard_phone.py >/dev/null 2>&1; then
  echo "$(date '+%F %T') alive（无需动作）" >> "$LOG"
  exit 0
fi
echo "$(date '+%F %T') 守护不在，重新拉起" >> "$LOG"
cd "$HOME/charge_guard" || exit 1
nohup python charge_guard_phone.py >> /sdcard/charge_guard/guard.out 2>&1 &
sleep 3
pgrep -f charge_guard_phone.py >/dev/null 2>&1 \
  && echo "$(date '+%F %T') 已拉起 PID=$(pgrep -f charge_guard_phone.py | head -1)" >> "$LOG" \
  || echo "$(date '+%F %T') 拉起失败（看 guard.out）" >> "$LOG"
