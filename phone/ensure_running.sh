#!/data/data/com.termux/files/usr/bin/sh
# 自愈看门狗：被 JobScheduler 定期调用，也会被 ~/.bashrc 钩子调用。
# 三件事：① 自更新 ② 同步守护代码（/sdcard -> ~/charge_guard，有更新就重启）③ 判活并按需拉起
termux-wake-lock
LOG=/sdcard/charge_guard/watchdog.log
SRC=/sdcard/charge_guard
DST=$HOME/charge_guard
CSV=$SRC/phone_guard_log.csv
FRESH=180          # 秒：正常每 60s 写一次，超过这个时间没更新才算不在
mkdir -p "$DST"

# ① 自更新（adb shell 写不进 Termux 私有目录，只能推 /sdcard，用这招自动升级）
if [ -f "$SRC/ensure_running.sh" ] && [ "$SRC/ensure_running.sh" -nt "$0" ]; then
  cp "$SRC/ensure_running.sh" "$0" 2>/dev/null && exec sh "$0"
fi

# ② 同步守护代码：只要 /sdcard 上的比本地新，就替换并重启（这样"部署"不再需要 adb 敲命令）
CHANGED=0
for f in charge_guard_phone.py charge_guard_phone.json; do
  if [ -f "$SRC/$f" ] && [ "$SRC/$f" -nt "$DST/$f" ]; then
    cp "$SRC/$f" "$DST/$f" && CHANGED=1 && echo "$(date '+%F %T') 同步了 $f" >> "$LOG"
  fi
done

if [ "$CHANGED" = "1" ]; then
  echo "$(date '+%F %T') 代码有更新 → 重启守护" >> "$LOG"
  pkill -f charge_guard_phone.py
  sleep 2
  cd "$DST" || exit 1
  nohup python charge_guard_phone.py >> "$SRC/guard.out" 2>&1 &
  exit 0
fi

# ③ 判活：只认「采样文件新鲜度」
NOW=$(date +%s)
LAST=$(stat -c %Y "$CSV" 2>/dev/null || echo 0)
AGE=$((NOW - LAST))
if [ "$AGE" -lt "$FRESH" ]; then
  echo "$(date '+%F %T') alive（CSV ${AGE}s 前写过，无需动作）" >> "$LOG"
  exit 0
fi
echo "$(date '+%F %T') 守护不在（CSV ${AGE}s 未更新），重新拉起" >> "$LOG"
cd "$DST" || exit 1
nohup python charge_guard_phone.py >> "$SRC/guard.out" 2>&1 &
sleep 4
if [ "$(stat -c %Y "$CSV" 2>/dev/null || echo 0)" -gt "$LAST" ]; then
  echo "$(date '+%F %T') 已拉起并开始采样" >> "$LOG"
else
  echo "$(date '+%F %T') 拉起后仍未采样，看 guard.out" >> "$LOG"
fi
