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

当天 APK 出过三种「悄悄死」：

1. Python 循环异常逃逸 —— `CsvLog.row` 写失败抛异常，循环 except 分支里再写一行同样抛，
   异常逃出 while：服务活着、循环死了，status 停在 12:50:26（代码路径实锤）；
2. 循环线程**卡死**（12:50:26 / 13:24:52 / 14:02 三次取证）：进程在、FGS 在、cgroup
   frozen=0、无 Log.e 无 stop 行 ⇒ 阻塞在无超时调用里，头号嫌疑 /sdcard FUSE 写；
3. 上滑清理杀进程 —— logcat `am_kill due to SwipeUpClean`，前台服务连同进程一起被杀，
   START_STICKY 未被 HyperOS 执行。

对应加固：

- `GuardService`：**监督线程** —— start_guard 无论正常返回还是抛异常，30s 后重拉
  （1s 步进退避，onDestroy 最多等 1s）；
- **/sdcard 移出循环线程**（v2.1）：CSV 固定私有 `files/guard_log.csv`（ext4，无 FUSE）；
  `/sdcard/charge_guard/apk_status.json` 由独立镜像线程尽力搬运（它卡死也不影响守护）；
- **卡死看门狗**：心跳停 >150s ⇒ faulthandler 把全部线程 Python 栈**追加落盘**
  `files/stall_dump.txt`（logcat 会轮转、14:30 那次证据就是这么丢的）并镜像到
  `python.stderr`，然后 **SIGKILL 自杀**。三种死法都实测过：os.abort() 弹闪退窗
  （14:10）、System.exit(0) 在 JVM 关闭钩子上挂死 4.7h（14:30→19:11）、SIGKILL 立死无弹窗；
- **复活闹钟链**（v2.2）：服务每次启动预约 **5 分钟**后的一次性闹钟，`GuardAlarmReceiver`
  拉起服务并续约 —— 进程死透后闹钟照常触发（除非 force-stop），是 START_STICKY 被
  HyperOS 拦截后唯一验证可行的自动补位通道；「停止」按钮置 `should_run=false` 让链空转；
- **wakelock 只在充电时持有**（v2.5）：恒持 PARTIAL_WAKE_LOCK 让 CPU 永远进不了
  suspend —— batterystats 实测 `chargeguard:loop` 连续持有 10h31m，占待机耗电大头
  （Termux 的 `termux-wake-lock` 同病，只是当时没归因）。插电=持锁保证 60s 节拍；
  不插电=放锁深睡，5 分钟唤醒闹钟驱动采样（掉电 ~1%/10min，粒度足够）。
  双看门狗阈值同步提到 600s 起步，避免误杀深睡节拍；
- **Java 侧看门狗**（v2.3）：20:48 的 stall 连 python 看门狗都一起冻住了（GIL 被卡死线程
  持有，stall_count 没来得及自增）——这条线程不碰 python，只盯 `status.json` 的 mtime，
  超时把全部线程的 Java 栈落盘 `files/stall_dump_java.txt`（能看到卡在哪个 Java 方法）
  再 `killProcess`。python 看门狗管 GIL 释放型卡死，Java 看门狗兜 GIL 冻结型卡死；
- `GuardService.onTaskRemoved`：闹钟预约 3s 后重拉前台服务（ROM 可能拦，尽力而为；
  最可靠的仍是用户侧三项设置）；
- `android_main.SafeLog`：写日志失败永不外抛（v2.1 起主路径=私有目录，兼自愈句柄）；
- `start_guard` 循环体整体兜底，异常秒回时补齐整间隔防风暴；
- MainActivity 只在未授权时请求通知权限（原来每次打开都弹一次）。

回归测试：`python -X utf8 scripts/test_android_main_offline.py`（CI 有同名步骤）。

## 观测链路

- **CSV：私有目录 `files/guard_log.csv`**（`run-as com.zhiyaunhe.chargeguard cat files/guard_log.csv`
  或 `tail`）。/sdcard 上的 `phone_guard_log.csv` 是 Termux/PC 看门狗时代的历史文件，**已冻结不再更新**。
- 状态快照：私有 `filesDir/status.json`（真源）+ 镜像 `/sdcard/charge_guard/apk_status.json`
  （独立线程尽力搬运），界面每 2s 刷新读私有那份。
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
