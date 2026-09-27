# charge_guard APK（Android 壳）

把 Termux 方案升级成正经 App：前台服务常驻 + 开机自启 + BatteryManager 读电量，
判定逻辑 / miIO 协议 / AES **原样复用** `phone/charge_guard_phone.py`（同一份文件，CI diff 钉着）。

## 构建

本机不装 Android SDK，构建全在 GitHub Actions（`.github/workflows/android-build.yml`）：

```bash
# 推送后自动构建；手动触发：
gh workflow run android-build
gh run watch
gh run download --name chargeguard-debug-apk -D android/dist/
```

技术栈：AGP 8.7.3 / Gradle 8.10.2 / Chaquopy 17.0.0（MIT）/ compileSdk 35 / targetSdk 33 / minSdk 24 /
仅 arm64-v8a（K70 Pro）。无任何第三方 Android 依赖（没有 androidx）。

## 与 Termux 版的关系（重要）

**同一时间只允许一个大脑在控插座。** 换 APK 接管前的交接清单：

1. APK 安装、配置、读/写测试通过；
2. 杀掉 Termux 守护：Termux 里 `pkill -f charge_guard_phone.py`；
3. 拆掉 Termux 的自愈（否则 PC 看门狗 am start 会把它拉回来，变成双大脑）：
   - 删 `~/.bashrc` 里的 ensure_running 钩子行；
   - 取消 JobScheduler 任务（`termux-job-scheduler` 里那个 15 分钟的 job）；
4. 观察 APK 走完一整个滞回周期（掉到 50 通电 / 充到 65 断电）。

## 观测链路（保持与 Termux 版一致）

- CSV：优先写 `/sdcard/charge_guard/phone_guard_log.csv`（与 Termux 版同一路径，PC 看门狗无感切换）；
  没有「所有文件访问」权限时退回 app 私有目录（`run-as com.zhiyaunhe.chargeguard cat files/guard_log.csv`）。
- 状态快照：`filesDir/status.json` + 镜像 `/sdcard/charge_guard/apk_status.json`，界面每 2s 刷新。
- PC 侧 `watch_from_pc.py` 不需要任何改动。

## 需要用户手点的权限（HyperOS）

- 「所有文件访问」（设置 → 应用 → 充电守护）；启动时会自动跳转。
- MIUI 自启动 + 省电策略无限制 + 最近任务锁定（与 Termux 同一批）。

## 文件

| 文件 | 说明 |
|---|---|
| `app/src/main/java/.../GuardService.java` | 前台服务：PARTIAL_WAKE_LOCK + START_STICKY + 单实例 Python 循环 |
| `app/src/main/java/.../MainActivity.java` | 单页程序化 UI：IP/token/阈值、启动停止、读/写测试、状态 |
| `app/src/main/java/.../BootReceiver.java` | 开机自启 |
| `app/src/main/python/android_main.py` | Android 适配层：BatteryManager 电量注入、CSV/状态镜像、测试入口 |
| `app/src/main/python/charge_guard_phone.py` | **副本**，同步用 `scripts/sync_android_python.sh` |
