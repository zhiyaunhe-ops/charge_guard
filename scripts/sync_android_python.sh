#!/bin/sh
# 把 phone/ 的共享 Python 源同步进 APK 工程的唯一入口。
# android/app/src/main/python/ 下的同名文件是副本，不要手改 —— CI 用 diff 钉着两边。
set -e
cd "$(dirname "$0")/.."
cp phone/charge_guard_phone.py android/app/src/main/python/charge_guard_phone.py
cp phone/charge_guard_phone.json android/app/src/main/python/charge_guard_phone.json
echo "synced:"
diff phone/charge_guard_phone.py android/app/src/main/python/charge_guard_phone.py && \
  diff phone/charge_guard_phone.json android/app/src/main/python/charge_guard_phone.json && \
  echo "  charge_guard_phone.py / .json  OK"
