#!/data/data/com.termux/files/usr/bin/sh
# 自愈看门狗：被 JobScheduler 定期调用（也会被 ~/.bashrc 钩子调用）；
# 发现守护不在就拉起来。被 MIUI 强制停止后 JobScheduler 会失效，
# 此时只能靠外部 `am start com.termux` 触发 .bashrc 钩子来救（PC 侧脚本负责）。
termux-wake-lock
LOG=/sdcard/charge_guard/watchdog.log

# 严格判活。两个坑都踩过（2026-09-26）：
#   · `ps -A | grep charge_guard_phone.py` 假阴性 —— Android 的 ps 只显示进程名 python；
#   · 裸 `pgrep -f` 假阳性 —— 调用者自己的命令行里含这个字符串，会把自己也算上。
# 所以用 grep -a 直接核对 /proc/<pid>/cmdline（用 grep 而不是 tr，避免脚本里出现 NUL 字节）。
guard_running() {
  for p in $(pgrep -f charge_guard_phone.py 2>/dev/null); do
    if grep -qa '^python' "/proc/$p/cmdline" 2>/dev/null &&
       grep -qa 'charge_guard_phone.py' "/proc/$p/cmdline" 2>/dev/null; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

if guard_running >/dev/null; then
  echo "$(date '+%F %T') alive（无需动作）" >> "$LOG"
  exit 0
fi

echo "$(date '+%F %T') 守护不在，重新拉起" >> "$LOG"
cd "$HOME/charge_guard" || exit 1
nohup python charge_guard_phone.py >> /sdcard/charge_guard/guard.out 2>&1 &
sleep 4
if guard_running >/dev/null; then
  echo "$(date '+%F %T') 已拉起 PID=$(guard_running)" >> "$LOG"
else
  echo "$(date '+%F %T') 拉起失败（看 guard.out）" >> "$LOG"
fi
