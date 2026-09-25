#!/data/data/com.termux/files/usr/bin/sh
# 在 Termux 内一次性安装「自愈看门狗」并注册 JobScheduler 任务。
# 全部输出写到 /sdcard/charge_guard/watchdog_install.log，方便外部核对。
OUT=/sdcard/charge_guard/watchdog_install.log
exec > "$OUT" 2>&1
echo "== $(date '+%F %T') 安装自愈看门狗 =="

mkdir -p "$HOME/charge_guard"
cp /sdcard/charge_guard/ensure_running.sh "$HOME/charge_guard/ensure_running.sh" \
  && chmod +x "$HOME/charge_guard/ensure_running.sh" \
  && echo "已就位: ~/charge_guard/ensure_running.sh"

echo
echo "---- termux-job-scheduler 可用性 ----"
command -v termux-job-scheduler || echo "缺 termux-job-scheduler（要 pkg install termux-api）"
termux-job-scheduler --help 2>&1 | head -18

echo
echo "---- 注册任务：每 15 分钟检查一次，持久化（重启后仍在）----"
termux-job-scheduler \
  --script "$HOME/charge_guard/ensure_running.sh" \
  --job-id 1 \
  --period-ms 900000 \
  --persisted true 2>&1

echo
echo "---- 已登记的任务 ----"
termux-job-scheduler --pending 2>&1 | head -12

echo
echo "---- 立刻手动执行一次看门狗 ----"
sh "$HOME/charge_guard/ensure_running.sh"
sleep 4
echo "守护进程："
ps -A -o PID,ETIME,NAME | grep -E 'python' | head -3
echo
echo "看门狗日志："
tail -3 /sdcard/charge_guard/watchdog.log 2>/dev/null || echo "(还没有日志)"
echo
echo "== 完成 =="
