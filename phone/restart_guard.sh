#!/data/data/com.termux/files/usr/bin/sh
# 干净重启守护：更新文件 → 停旧实例 → 启动 → 写出校验结果
cd "$HOME/charge_guard" || exit 1
cp /sdcard/charge_guard/charge_guard_phone.py /sdcard/charge_guard/ensure_running.sh .
pkill -f charge_guard_phone.py
sleep 2
termux-wake-lock
nohup python charge_guard_phone.py >> /sdcard/charge_guard/guard.out 2>&1 &
sleep 7
{
  echo "== $(date '+%F %T') 干净重启 =="
  echo "匹配到的 PID: $(pgrep -f charge_guard_phone.py | tr '\n' ' ')"
  echo "真实 python 实例："
  for p in $(pgrep -f charge_guard_phone.py 2>/dev/null); do
    cl=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)
    case "$cl" in python*charge_guard_phone.py*) echo "  PID=$p  cmdline=$cl" ;; esac
  done
  echo "--- guard.out 末尾："
  tail -3 /sdcard/charge_guard/guard.out
  echo "--- 电量/充电："
  dumpsys battery 2>/dev/null | grep -E 'AC powered|level' | head -2
} > /sdcard/charge_guard/restart_check.log 2>&1
