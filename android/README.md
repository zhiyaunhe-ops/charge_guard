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

**2026-10-01 交接已完成**，同日 PC 侧 `watch_from_pc.py` 看门狗**停用**：APK 接管后它仍按
Termux 时代的 CSV 判活，误判「守护不在」→ 每 5 分钟 `am start` 把 Termux 拉到前台
（机主手机上反复弹 Termux 的原因，watch_from_pc.log 1072–1077 轮可证）。兜底职责改由
下面的加固机制承担，脚本与 bat 保留仅供考古 / 应急。

## 常驻加固（2026-10-01，versionCode 2）

当天 APK 出过两种「悄悄死」：

1. Python 循环异常逃逸 —— `CsvLog.row` 写失败抛异常，循环 except 分支里再写一行同样抛，
   异常逃出 while：服务活着、循环死了，status 停在 12:50:26，无任何日志（logcat 缓冲
   轮转后不可考，代码路径是实锤）；
2. 上滑清理杀进程 —— logcat `am_kill due to SwipeUpClean`，前台服务连同进程一起被杀，
   START_STICKY 未被 HyperOS 执行。

对应加固：

- `GuardService`：**监督线程** —— start_guard 无论正常返回还是抛异常，30s 后重拉
  （1s 步进退避，onDestroy 最多等 1s）；
- `GuardService.onTaskRemoved`：闹钟预约 3s 后重拉前台服务（ROM 可能拦，尽力而为；
  最可靠的仍是用户侧三项设置）；
- `android_main.SafeLog`：写日志失败永不外抛；主 CSV 连续 3 次失败自动降级私有目录
  （status.json 的 `csv_path` 可见实际在用哪个）；
- `start_guard` 循环体整体兜底，异常秒回时补齐整间隔防风暴；
- MainActivity 只在未授权时请求通知权限（原来每次打开都弹一次）。

回归测试：`python -X utf8 scripts/test_android_main_offline.py`（CI 有同名步骤）。

## 观测链路（保持与 Termux 版一致）

- CSV：优先写 `/sdcard/charge_guard/phone_guard_log.csv`（与 Termux 版同一路径）；
  「所有文件访问」缺失或运行期写失败时由 SafeLog 退回 app 私有目录
  （`run-as com.zhiyaunhe.chargeguard cat files/guard_log.csv`）。
- 状态快照：`filesDir/status.json` + 镜像 `/sdcard/charge_guard/apk_status.json`，界面每 2s 刷新。
- PC 看门狗已停用（见上）；应急仍可用 `am start-foreground-service -n
  com.zhiyaunhe.chargeguard/.GuardService` 从 adb 拉起守护。

## 需要用户手点的权限（HyperOS）

- 「所有文件访问」（设置 → 应用 → 充电守护）；启动时会自动跳转。
- MIUI 自启动 + 省电策略无限制 + 最近任务锁定（与 Termux 同一批）。

## 文件

| 文件 | 说明 |
|---|---|
| `app/src/main/java/.../GuardService.java` | 前台服务：PARTIAL_WAKE_LOCK + START_STICKY + 监督线程（Python 循环死了 30s 重拉）+ onTaskRemoved 闹钟自救 |
| `app/src/main/java/.../MainActivity.java` | 单页程序化 UI：IP/token/阈值、启动停止、读/写测试、状态 |
| `app/src/main/java/.../BootReceiver.java` | 开机自启 |
| `app/src/main/python/android_main.py` | Android 适配层：BatteryManager 电量注入、CSV/状态镜像、测试入口 |
| `app/src/main/python/charge_guard_phone.py` | **副本**，同步用 `scripts/sync_android_python.sh` |
