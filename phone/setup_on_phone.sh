#!/data/data/com.termux/files/usr/bin/sh
# 手机端一次性安装 + 自检脚本（Termux 内运行：sh /sdcard/charge_guard/setup_on_phone.sh）
# 做三件事：把文件拷进 Termux 家目录 → 跑 4 项检查 → 打印下一步该敲什么。
set -u

SRC=/sdcard/charge_guard
DST="$HOME/charge_guard"

echo "== 1/4 拷贝文件到 $DST =="
mkdir -p "$DST" || { echo "无法创建 $DST"; exit 1; }
cp "$SRC/charge_guard_phone.py" "$SRC/charge_guard_phone.json" "$SRC/charge_guard_phone.local.json" "$DST/" 2>/dev/null
ls -l "$DST"
cd "$DST" || exit 1

echo
echo "== 2/3 依赖检查（缺就装：python / termux-api）=="
if ! command -v python >/dev/null 2>&1; then
  echo "-- 没找到 python，开始 pkg 安装（几分钟，别关 Termux）--"
  pkg update -y || echo "(pkg update 有报错，继续试)"
  pkg install -y python || { echo "python 安装失败，退出"; exit 1; }
fi
command -v python && python --version
if ! command -v termux-battery-status >/dev/null 2>&1; then
  echo "-- 没找到 termux-battery-status，装 termux-api --"
  pkg install -y termux-api || echo "(termux-api 安装失败，电量会读不到)"
fi
command -v termux-battery-status || echo "⚠️ termux-battery-status 仍不可用"

echo
echo "== 3/3 四项检查 =="
run() {
  echo
  echo "---- $* ----"
  python "$@" 2>&1 | tail -12
}

run charge_guard_phone.py --self-test
run charge_guard_phone.py --probe
run charge_guard_phone.py --show-battery
run charge_guard_phone.py --dry-run --once   # 一轮即退出；--ticks N 是每轮间隔 5 分钟，会等很久

echo
echo "== 4/4 下一步 =="
cat <<'EOF'
如果上面 --self-test 是「21 通过 / 0 失败」、--probe 能看到「开关 = True」，
就可以常驻运行了：

  termux-wake-lock
  cd ~/charge_guard && nohup python charge_guard_phone.py >> guard.out 2>&1 &
  tail -f guard.out          # 看实时输出（Ctrl-C 只是停止看，不会停脚本）

日志文件：~/charge_guard/phone_guard_log.csv
想停掉：pkill -f charge_guard_phone.py
EOF
