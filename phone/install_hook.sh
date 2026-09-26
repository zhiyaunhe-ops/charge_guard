#!/data/data/com.termux/files/usr/bin/sh
# 一次性：装 .bashrc 钩子 + 重启守护 + 重新注册 JobScheduler 任务。输出写 /sdcard 供外部核对。
OUT=/sdcard/charge_guard/hook_install.log
exec > "$OUT" 2>&1
echo "== $(date '+%F %T') 安装钩子 / 重启守护 / 重注册任务 =="

BRC="$HOME/.bashrc"
touch "$BRC"
if ! grep -q 'charge_guard: ensure_running' "$BRC" 2>/dev/null; then
  cat >> "$BRC" <<'EOF'

# charge_guard: ensure_running -- 每次打开 Termux 时确保守护在跑
# 为什么需要：MIUI 强制停止应用时会取消 JobScheduler 任务、也收不到开机广播；
# 此时唯一能救回来的动作是「有人打开 Termux」——PC 侧一个 `am start` 即可触发本钩子。
if [ -x "$HOME/charge_guard/ensure_running.sh" ]; then
  sh "$HOME/charge_guard/ensure_running.sh" >/dev/null 2>&1 &
fi
EOF
  echo "已写入 ~/.bashrc 钩子"
else
  echo "~/.bashrc 钩子已存在（跳过）"
fi

echo
echo "---- 重启守护 ----"
sh "$HOME/charge_guard/ensure_running.sh"
sleep 4
if pgrep -f charge_guard_phone.py >/dev/null 2>&1; then
  echo "守护在跑 PID=$(pgrep -f charge_guard_phone.py | head -1)"
else
  echo "守护没起来！看 /sdcard/charge_guard/guard.out"
fi

echo
echo "---- 重新注册 JobScheduler 任务 ----"
termux-job-scheduler --script "$HOME/charge_guard/ensure_running.sh" \
  --job-id 1 --period-ms 900000 --persisted true 2>&1 | tail -2
echo "当前登记的任务："
termux-job-scheduler --pending 2>&1 | head -3

echo
echo "---- 看门狗日志末尾 ----"
tail -3 /sdcard/charge_guard/watchdog.log 2>/dev/null
echo "== 完成 =="
