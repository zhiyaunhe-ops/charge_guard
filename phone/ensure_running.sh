#!/data/data/com.termux/files/usr/bin/sh
# 自愈看门狗：被 JobScheduler 定期调用（也会被 ~/.bashrc 钩子调用）；
# 发现守护不在就拉起来。被 MIUI 强制停止后 JobScheduler 会失效，
# 此时只能靠外部 `am start com.termux` 触发 .bashrc 钩子来救（PC 侧脚本负责）。
# 自更新：/sdcard 上有更新版就替换自己再跑一遍。
# 为什么需要：adb shell 用户写不进 Termux 私有目录，只能推到 /sdcard；
# 这样下次钩子/JobScheduler 触发时就能自动升级，不用打断用户手动拷贝。
SRC=/sdcard/charge_guard/ensure_running.sh
if [ -f "$SRC" ] && [ "$SRC" -nt "$0" ]; then
  cp "$SRC" "$0" 2>/dev/null && exec sh "$0"
fi

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

# 主判据：守护每 interval_sec 秒写一次 CSV，所以「CSV 文件新鲜度」才是最可靠的判活方式。
# 为什么不用 pgrep/ps：Android 上跨 UID 读 /proc/<pid>/cmdline 会失败（假阴性），
# 而裸 pgrep -f 又会自匹配（假阳性）—— 2026-09-26 两种误判都踩过，实测游戏期间就是这样误报的。
CSV=/sdcard/charge_guard/phone_guard_log.csv
FRESH=180   # 秒：超过这个时间没有新采样就认为它不在了（正常是 60s 一写）
csv_age() {
  now=$(date +%s)
  last=$(stat -c %Y "$CSV" 2>/dev/null || echo 0)
  echo $((now - last))
}
AGE=$(csv_age)
if [ "$AGE" -lt "$FRESH" ]; then
  echo "$(date '+%F %T') alive（CSV ${AGE}s 前刚写过，无需动作）" >> "$LOG"
  exit 0
fi
if guard_running >/dev/null; then
  echo "$(date '+%F %T') 进程在但 CSV ${AGE}s 没更新（可能卡住），仍拉起一次试试" >> "$LOG"
fi

echo "$(date '+%F %T') 守护不在（CSV ${AGE}s 未更新），重新拉起" >> "$LOG"
cd "$HOME/charge_guard" || exit 1
nohup python charge_guard_phone.py >> /sdcard/charge_guard/guard.out 2>&1 &
sleep 4
if guard_running >/dev/null; then
  echo "$(date '+%F %T') 已拉起 PID=$(guard_running)" >> "$LOG"
else
  echo "$(date '+%F %T') 拉起失败（看 guard.out）" >> "$LOG"
fi
