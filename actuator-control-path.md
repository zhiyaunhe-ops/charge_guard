# 执行端选型：怎么把「停 65%」这个动作落到硬件上

状态：**已定 —— 路线 1（手机当大脑 + ESP32 桥）**。手机侧已就绪（见 `phone/`），等 ESP32 硬件到货。
本文记录两条候选路径、已验证的证据、以及必须先验掉的失败模式。
日期：2026-09-25（末次更新：路线确定 + 手机侧落地）

---

## 一、先说结论

| 路径 | 能只切手机那一路？ | 现状 |
|---|---|---|
| **A. BLE 直控充电器的 C1 口** | ✅ 只切 C1，其他口不受影响 | 有开源实现，但**本机还没扫到设备**；且有一个可能致命的失败模式未验（见第四节） |
| **B. 智能插座断 220V** | ❌ 整个充电器一起断 | 技术路径最成熟可靠，但会连带 C1 之外的口，且每次通断是对充电器电源的一次上电冲击 |
| ~~C. python-miio 控充电器~~ | — | **不成立**：充电器是 BLE 设备，没有 Wi-Fi，miIO/miOT 是局域网协议 |

---

## 二、路径 A：BLE 直控 C1（开源实现已经存在）

### 证据（均为仓库 README 原文，非推测）

**1. `ohaiibuzzle/xiaomi-ad1204-python`**（最近提交 2026-09-15）
- 原文：*"Watch your CUKTECH 10 GaN Charger Ultra's live power stats and change its settings —
  **from your laptop, over Bluetooth, without the Mi Home app**."*
- 三个工具：`dashboard.py`（终端实时看各口电压/电流/功率/协议，按 `s` 进设置菜单）、
  `ad1204_ble.py`（脚本化用的 CLI）、`fetch_tokens.py`（登录小米账号，导出蓝牙地址 + token）
- 写设置的命令形态：`ad1204_ble.py --token <token> --set 2-5=1`（`siid-piid=value`）
- 明确支持改：场景模式 / 息屏时间 / 语言 / 亮度；安全提示里还提到 "port state"

**2. `kairui1108/cuktech-ble-ha`**（中文，v1.1.0，功能更全）
- README 原文：「**端口控制**：远程开关 C1/C2/C3/A 端口」 ← **正是我们要的能力**
- 另有：每口倒计时、协议开关（PD/PPS/UFCS/SCP）、场景模式、**充电完成事件**、
  MQTT / HTTP API（`POST /api/port` 控端口开关）、SQLite 历史
- 架构：BLE 网关（**Python BLE Server（Linux/Docker）** 或 **ESP32 BLE Bridge**）→ MQTT → Home Assistant
- ⚠️ 已知限制原文：「**平台支持（Python BLE Server）：开发与测试基于 Linux 环境**」

**3. 设备身份已确认**
`njcuk.fitting.ad1204` = CUKTECH 10 GaN Charger Ultra。这个型号串与上述仓库的支持目标完全一致
（2026-09-25 由 机主 在手机上确认）。

### 本机诊断结果（2026-09-25，He-PC）

| 项 | 结果 |
|---|---|
| 蓝牙适配器 | 存在，`USB\VID_8087&PID_0033`（**Intel 内置**），地址 `a4:f9:33:c2:a3:bb` |
| `is_low_energy_supported` | **True**（支持 BLE） |
| `is_central_role_supported` | **True**（能作为 central 主动连外设） |
| `is_extended_advertising_supported` | True |
| 蓝牙无线电状态 | On |
| **BLE 扫描（bleak 3.0.2，15s）** | **0 个设备** |
| **BLE 扫描（WinRT 原生广播监听，12s）** | **0 个广播**（绕开 bleak，排除库的问题） |
| 沙箱内外 | 结果一致（不是沙箱拦截） |

⇒ **适配器本身合格，PC 有能力当 BLE 主机。** 扫不到的最可能原因：
充电器当时没在广播 —— 仓库 README 明确写了两条：
> *"the charger only accepts **one Bluetooth connection at a time**, and if the app is connected,
> these tools can't get in"*
> *"its display goes to sleep and it **stops broadcasting over Bluetooth when idle**.
> Wake the display or plug something in, then try again."*

### ★ 追加结论：先把「环境问题」排除掉了（2026-09-25）

用 `ble_probe.py` 复查（`run_ble_probe.bat` 双击即可跑）：

| 检查 | 结果 |
|---|---|
| 适配器能力（WinRT，不走射频） | BLE 支持、central 角色支持、广播扩展支持 全部 True |
| 广播监听器 `start()` 后的状态 | **STARTED** |
| 是否有 `aborted` 事件 | **无** |
| 扫描 20s 收到广播数 | 0 |

监听器进入 STARTED 且不 abort，说明**射频请求被系统接受，本进程能正常做 BLE 扫描**
（过程中出现过两次静默段错误 exit 139，是偶发，不是通路被拦）。
⇒ **「环境拿不到无线设备」这个假设被排除。0 个广播的唯一解释是：当时附近没有设备在广播。**

于是收敛到两个具体原因，按可能性排序：
1. **充电器息屏停播**（README 明确写了；它只在被唤醒/有负载时广播）；
2. **充电器不在本机 BLE 距离内**（BLE 穿墙能力有限，若 PC 与充电器不在同一房间就可能扫不到）；
3. 次要：手机米家 App 正占着那条唯一的 BLE 连接。

**下一步（30 秒，交给 机主）**：把充电器拿到 PC 旁边 1 米内 → 点亮屏幕/插着手机让它有负载
→ 关掉手机上的米家 App → 双击 `run_ble_probe.bat`。
报告里若出现 `njcuk.fitting.ad1204`，路径 A 就成立了，可以继续去拿 token。

### ★★ 但扫描其实不该拦路：手机能连 ≠ 我需要能扫到

机主 问「为什么我 K70 Pro 都能轻易连上？」—— 关键在于**两侧用的不是同一个动作**：

| | 手机米家 App | 我的脚本（当前） |
|---|---|---|
| 知不知道充电器地址 | **知道**（配对时记下，存在小米云） | 不知道 |
| 动作 | `connect(已知地址)` —— **定向连接** | `discover()` —— **盲扫等广播** |
| 凭据 | 有 token / BLE key | 没有 |

所以「手机轻易连上」**并不能推出**「PC 一定能扫到」：前者是拿着地址去敲门，后者是在楼道里等它喊。
它也**不能**反过来说明设备或距离有问题。

⇒ **结论：不要卡在扫描上。** 开源实现里 `fetch_tokens.py` 可以直接登录小米账号，
把**每个设备的蓝牙地址 + token** 导出来（README 原文示例：
`njcuk.fitting.ad1204 ... address: AA:BB:CC:DD:EE:FF  token: <24 hex>`）。

拿到 address + token 之后就是**定向连接**，和手机一样的机制，绕开「必须扫到才能连」这一步。

⚠️ 但有一条不变：**定向连接也要求充电器处于「可连接广播」状态**。
所以「息屏停播」和「米家 App 占着唯一的那条连接」这两个坑依然存在 ——
保持唤醒、关掉米家 App，这两条仍然要做。

### 还没解决的问题（路径 A）

1. **⚠️ 可能致命的失败模式**：把 C1 关掉之后，如果**没有别的设备插在这个充电器上**，
   充电器进入空闲 → 屏幕息屏 → **停止广播** → 那么就再也连不上它了，
   也就是「关得掉、开不回来」。**这条必须先实测**：关掉 C1 → 等 5 分钟 → 再扫，看还在不在广播。
   在验掉它之前，不要把这个方案当可行。
2. **单连接竞争**：脚本占着 BLE 时，手机上的米家 App 就连不上（反之亦然）。需要设计成
   「按需连接 → 读写 → 立刻断开」，而不是长期占着连接。
3. **Windows 未验证**：两个实现都只在 Linux 上开发测试过。
4. **凭据**：需要设备 MAC + token（12 字节 hex），`kairui1108` 那套还要 **BLE Key（16 字节 hex）**。
   获取方式：`fetch_tokens.py` 登录小米账号（会走 2FA / 图形验证码），
   或 `kairui1108` 的 Web 配置页「小米云扫码」（用米家 App 扫码，不用输密码）。
   **token 会在米家里重新配对后轮换。**
5. **兜底硬件**：如果 Windows 这条走不通，两个仓库都提供 **ESP32 BLE Bridge** 固件 ——
   一块 ¥20–40 的模块放在充电器旁边，直连 BLE，对外暴露 HTTP/MQTT，
   于是 PC 侧只需要走局域网，完全不依赖 PC 的蓝牙。这是路径 A 的稳健形态。

---

## 三、路径 B：智能插座断 220V（当前脚本已实现的那条）

- 成熟、可靠、局域网协议（`python-miio`）。
- **代价**：C1 之外的口也一起断电。如果别的口插着东西，这条路直接不适用。
  （→ 需要 机主 确认：这台充电器其他 3 个口现在插着什么？）
- **另一个代价**：每次通断都是对 120W 电源的一次上电冲击（inrush）。
  按 65/50 的 15% 摆幅、日均耗电 20–45% 推算约每天 1–3 次，可接受；
  如果想更少通断，可以把摆幅放宽（峰值电压会上去，是取舍）。
- 注意：**Python-miio 对 ZNCZ401KK（小米智能插座4）是否已支持也还没验证** ——
  需要 `miiocli` 能否认出该型号，认不出就装 python-miio 开发版。

---

## 四、待验 / 待确认清单

| # | 事项 | 谁来做 | 阻塞了什么 |
|---|---|---|---|
| 1 | 把充电器拿到 PC 旁 1 米内 + 点亮屏幕 + 关掉米家 App，双击 `run_ble_probe.bat` | 我出工具，机主 跑 | 路径 A 是否成立（环境问题已排除） |
| 2 | **关掉 C1 后充电器还广播吗** | 我测 | 路径 A 是否有致命缺陷 |
| 3 | 其他 3 个口现在插着什么 | 机主 | 路径 B 是否可用 |
| 4 | 有没有智能插座（ZNCZ401KK 或别的） | 机主 | 路径 B 的硬件前提 |
| 5 | **走哪条路线**（第五节四条） | 机主 定 | 全部后续工作 |

---

| 文件 | 说明 |
|---|---|
| `ble_probe.py` | BLE 探测：适配器能力 → 监听器状态 → 广播扫描，逐行落盘 |
| `run_ble_probe.bat` | 双击入口（CRLF / ANSI / 纯 ASCII 文件名），跑完自动打印报告 |
| `ble_probe_report.txt` | 运行产物（已 gitignore，不进版本库） |

---

## 五、能不能整套搬到 K70 上跑？—— 能，但有一个硬约束

**硬约束：Android 上跑 BLE 不能走 Termux + pip。**
Termux 里没有 BlueZ / D-Bus，`pip install bleak` 拿不到蓝牙后端。
bleak **确实有** Android 后端（`bleak.backends.android`，2026 年新增），
但它基于 **Chaquopy / python-for-android** —— 也就是**必须把 Python 代码打包成 APK 装进手机**，
不是在 Termux 里跑脚本。构建 python-for-android 要在 Linux / WSL / Docker 里做。

⇒ 一句话：**「手机当大脑」很容易，「手机自己发 BLE」很难。** 这个不对称决定了路线。

### 四条路线

| 路线 | 手机当大脑 | 只切 C1 | 额外硬件 | 工作量 |
|---|---|---|---|---|
| **1. 手机 + ESP32 桥（推荐）** | ✅ Termux 纯 Python | ✅ | ESP32 ￥20–40 | **小** |
| 2. 手机 + 智能插座 | ✅ Termux 纯 Python | ❌ 断整机 | 智能插座 ￥59 | 最小 |
| 3. 手机裸跑 BLE（打包 APK） | ✅ | ✅ | 无 | **大** |
| 4. 维持现状（PC 全包） | ❌ 依赖 PC 常开且近 | ✅ | 无 | 已完成大半 |

**路线 1 的形态**：ESP32 放在充电器旁边，BLE 连它，对外暴露 HTTP / MQTT。
手机 Termux 里读自己的电量（`termux-battery-status`）+ 决策 + 调 ESP32 的 HTTP。

这条路的价值不只是"搬到手机上"——它把前面写的一整堆东西**删掉**：
ADB 无线调试的端口三层兜底、`adb connect` 后 shell 未就绪的空读数、
daemon 被回收、沙箱杀子进程……**全部不再需要**，因为读端和执行端都不出手机。
而且仍然只切 C1 口，不碰 220V，没有对充电器电源的上电冲击。

📄 `kairui1108/cuktech-ble-ha` 的 README 指向一个 `cuktech-ble-esp32` 固件仓库
（支持 ESP32 / S3 / C3，AP 配网 + Web 仪表盘 + HTTP OTA）。**我还没核实该固件仓库的实际内容。**

**路线 3 的代价**（如果不想买任何硬件）：
协议不用重写 —— `ohaiibuzzle` 的 `ad1204_ble.py` 是现成的 Python 实现，可以直接复用。
但要把 Python + bleak 打成 APK，得在 Linux/WSL 里跑 python-for-android，拉 Android SDK/NDK（数 GB），
首次构建耗时长；且手机侧同样要先拿到 token / BLE key。

**路线 2 的注意点**：断插座 = 整个充电器断电（其他口一起断），
且每次通断是对 120W 电源的一次上电冲击。虽然最简单，但它牺牲的正是 机主 最在意的"只切第一个口"。

---

## 六、最终决定：不买硬件，PC 用自带蓝牙直连（2026-09-25 收尾）

机主 明确「不想买」。于是回到路径 A 的**纯软件形态**：PC 的蓝牙适配器已验证可用
（`is_low_energy_supported` / `is_central_role_supported` = True，监听器 STARTED、无 abort），
第三方实现已克隆并**在本机 Windows 上跑通 `--help`**，所以不需要任何新硬件。

### 已核实的协议细节（两个仓库交叉确认，不再是猜测）

| 项 | 值 | 出处 |
|---|---|---|
| 端口开关属性 | `siid=2, piid=16`，4 位掩码 | `cuktech-ble-ha/ble_server/ble_manager.py`：`ctrl.send_miot_command(2, 16, value=new_val)` |
| 位定义 | `c1=bit0, c2=bit1, c3=bit2, a=bit3` | `ble_server/state.py`：`PORT_BITS = {"c1":0,"c2":1,"c3":2,"a":3}` |
| 全开/全关 | `0x0F` / `0x00` | `ble_manager.py` 的 `_handle_port_command` |
| 其他可写属性 | 场景模式（2-5）、息屏（2-6）、语言（2-13）、亮度（2-14）、协议扩展（2-21） | `xiaomi-ad1204-python/ad1204_ble.py` 的 `LABELS` / `ENUMS` |

### 已实现

- `ble_plug.py` —— 执行端，接口对齐原来的 `PlugLink`（`configured()/set_power()/read_power_w()`）。
  不重写协议，调用 vendored 的 `ad1204_ble.py`；**先读掩码再改目标位**，不误动其他口；
  一次连接内完成「写 → 回读 → 确认」。
- `charge_guard.py` 新增 `--actuator {miio,ble}` 与 `--probe-charger`；缺 token 时优雅降级为只读。
  自测仍 54/54。
- 干跑已通：读 ADB（level 95 / 26.7℃ / AC false）→ 判定 off → BLE 执行端 dry-run 输出。

**注：干跑时手机已经 95%** —— 因为一直没有任何东西管它，它就一路充上去了。
这正好说明为什么需要这套东西。

### 剩下的一步（只能 机主 本人做）

```bash
cd third_party/xiaomi-ad1204-python
python fetch_tokens.py --region cn     # 登录小米账号（可能走 2FA/图形码）
```

拿到 `address` + `token` 后注入环境变量 `CHARGER_BLE_TOKEN`，然后：
`--probe-charger` → `--dry-run` → 正式。

### 仍然要先验的生死题

**关掉 C1 之后充电器还广播吗？** 若它没别的负载就息屏停播，会出现「关得掉、开不回来」。
验法：`--probe-charger` 看到掩码 → 关 C1 → 等 5 分钟 → 再 `--probe-charger`。
**在验掉这条之前，不让它无人值守地真控电。**

### 路线 1（手机 + ESP32）怎么办

已经做完的手机侧（`phone/`，Termux 脚本 + 三件套已装）**保留但不启用** ——
它的价值在于把读端也搬进手机、彻底摆脱 ADB 与 PC；哪天想上这个形态，ESP32 是唯一要买的东西。
现在不必买。

---

## 七、★ 追加（2026-09-25 晚）：扫描这条路的判据被削弱，缺一个定案实验

凭据（token + address）已经拿到，但**真机连接还没打通**。这一节记录当晚的新证据，
以及**我们之前一条结论被推翻**的部分 —— 下次接着做时不要引用旧说法。

### 新证据

| 项 | 结果 | 出处 |
|---|---|---|
| 凭据 | `address=3C:CD:73:37:B7:EE`，token 24 位 hex 已取到 | `fetch_tokens.py`（本轮修好 2FA 后才拿到） |
| Windows 蓝牙协议栈 | **好的**：`Get-PnpDevice -Class Bluetooth` 列出 **33 条**（Bose QC35 II、WF-1000XM4/5/6、MX Master 3S…） | `ble_probe.py` [2] 段（已改用 Get-PnpDevice 枚举） |
| 定向连接 | `BleakDeviceNotFoundError: Device with address 3C:CD:73:37:B7:EE was not found` | `charge_guard.py --probe-charger` |
| 广播扫描 | **ACTIVE 与 PASSIVE 两种模式各 12–20s，都是 0 个广播**（累计 5+ 次运行，含两次并发、一次来自双击 bat） | `ble_probe.py` [3] 段 + 临时脚本对比两种模式 |

### 被推翻的旧结论

第四节里写的「监听器 STARTED 且不 abort ⇒ 射频请求被接受，所以 0 个广播只能解释为附近没有设备」
**不成立**，理由是：

- 本机 winrt 绑定**根本没有 `add_aborted`**（只有 `add_stopped`），原代码的订阅被 `try/except`
  静默吞掉 —— 也就是说「无 aborted 事件」这条判据**从来没有被真正检查过**。
- `DeviceInformation.find_all_async` 与 `create_watcher` 在这个 winrt 版本里**一律抛
  `TypeError: Invalid parameter count`**，所以 [2] 段之前的「调用失败」也无诊断价值。
- 两处都已修：`add_aborted` 订阅失败时会明说「本判据不成立」，
  [2] 段改用 `Get-PnpDevice` 枚举（33 条），并且文案明确区分
  「协议栈好」≠「广播扫描可用」。

### 现在缺的定案实验（30 秒，一个动作）

把一个**已知在广播**的设备放到 PC 旁边，再跑一次 `run_charger_check.bat`：

- 手机（K70 Pro）开着蓝牙、屏幕点亮；**或**把蓝牙耳机（WF-1000XM5/XM6）从充电盒里拿出来。

| 现象 | 结论 | 下一步 |
|---|---|---|
| 那个设备出现在扫描结果里 | 扫描是好的 ⇒ 问题在充电器那一侧 | 逐个排除：关米家 App、点亮屏幕、挪到 1 米内、重跑 |
| 它也不出现 | **本机广播扫描这条路是死的**，PC 不适合当 BLE 主机 | 换一台机器 / ESP32 桥（¥20–40）/ 改走智能插座（第五节路线 2） |

在定案之前，不要再假设「PC 能当 BLE 主机」——这是整条路线 A 的地基。

### PC 侧体检结果（2026-09-25 晚，逐项排除）

机主 的手机（K70 Pro）**当场就能扫到充电器**（且此前从未配对）——这条把
「充电器息屏停播」基本排除，矛头指向 PC 的发现链路。于是把 PC 侧逐项查了一遍：

| 检查项 | 结果 | 是否可疑 |
|---|---|---|
| 适配器状态 | Intel 无线 Bluetooth，`Status=OK`、`Problem=CM_PROB_NONE` | 否 |
| 蓝牙支持服务 `bthserv` | Running | 否 |
| 应用访问蓝牙的权限 | `ConsentStore\bluetooth = Allow` | 否 |
| 无线适配器省电（当前电源计划） | 交流/直流都 = `0x0` 最高性能 | 否 |
| 协议栈可用性 | `Get-PnpDevice -Class Bluetooth` = 33 条（耳机/鼠标都在） | 否 |
| **适配器驱动** | **`23.120.0.4`（2025-02-10）** | **⚠️ 是** |

关于驱动：Intel 社区有人报告**升到 23.120.0.4 之后蓝牙「看不到任何设备」**
（Intel 论坛：https://community.intel.com/t5/Wireless/Bluetooth-not-working-after-update/m-p/1684762 ）；
另有技术分析指出这类芯片在特定驱动版本下，`BluetoothLEAdvertisementWatcher`
会**静默失败**（监听器照常 STARTED、`Received` 永远不触发）
（CSDN 分析：https://ask.csdn.net/questions/9244549 ）。
注意：这些都是候选解释，**尚未在本机证实**；也没解释为什么同一驱动下经典设备仍可用。

另一条已知的 Windows 层限制：Windows 的扫描窗口只有约 18 ms / 每 118 ms，
广播本来就容易漏 —— 它解释不了「0 个」，但意味着**不能把「扫到一次」当成稳定可用**。

### 修复阶梯（由轻到重，先做第 1 步）

1. **设置里把蓝牙开关关掉再打开**（必要时重启适配器），然后重跑 `run_charger_check.bat`。
2. **更新或回退 Intel 蓝牙驱动**（23.120.0.4 → 更新版，或回退 23.50.x）。
   ⚠️ Intel 官方对该版本的要求是：**升级前先解除所有已配对设备**（这台有 33 条），
   所以这一步成本实在，放在第 1 步之后。
3. 兜底：换一台机器 / ESP32 桥（¥20–40）/ 智能插座（第五节路线 2）。

### 定案实验（30 秒，两条路一起看）

把**已知在广播的设备**放到 PC 旁边（手机开蓝牙点亮屏幕，或把索尼耳机从充电盒里拿出来），
然后同时看两条路：① Windows 自己的界面（设置 → 蓝牙和其他设备 → 添加设备 → 蓝牙）；
② 同一时刻跑 `run_charger_check.bat`。

| Windows 界面 | 我们的脚本 | 结论 |
|---|---|---|
| 能看到 | 看不到 | 系统发现是好的，卡的是**应用这条路径** → 查权限/API，不必动驱动 |
| 看不到 | 看不到 | **OS 级发现在这台 PC 上是坏的** → 走修复阶梯第 1、2 步 |
| 能看到 | 能看到 | 之前只是充电器没在广播；继续 `--dry-run` 与「关掉 C1 还能连回来」的验证 |

---

## 八、2026-09-25 深夜：PC 直连走到「设备拒绝完成握手」为止（结论：这条路没走通）

凭据、发现、连接、订阅、写入这五关全部打通了，**卡在设备侧的 Mi BLE 登录握手**。
下面每一行都是本机实测，不是推测。

### 打通的部分

| 环节 | 结果 | 关键点 |
|---|---|---|
| 凭据 | ✅ token 与云端一致、未轮换 | 用缓存会话重取一次核对过 |
| 设备身份 | ✅ 云端地址 = Windows 配对地址 `BTHLE\DEV_3CCD7337B7EE` | 地址没有问题 |
| PC 协议栈 | ✅ `Get-PnpDevice -Class Bluetooth` = 33 条 | 耳机/鼠标都正常 |
| 广播发现 | ✅ 修好后能稳定扫到（rssi −54…−66） | 见下面的「发现的三个坑」 |
| GATT 连接 | ✅ `mtu=247`，一次成功（1.9s） | 需 `use_cached_services=False` |
| 通知订阅 | ✅ UPNP(0x0010) / AVDTP(0x0019) / SPEC_RX(0x001b) 全部成功 | 属性是 `write-without-response, notify` |
| 写入 | ✅ 无响应写（no-response）成功 | 用 `response=True` 会被设备以 ATT 0x03 拒绝 |

### 卡住的地方：登录握手

两个实现都试了，**都停在同一类地方**：

| 实现 | 行为 |
|---|---|
| `ohaiibuzzle/xiaomi-ad1204-python` | 直接发 `CMD_LOGIN(0x24)` → 设备回 **`e0000000`（明确拒绝）**。它**缺整个 Phase A（设备初始化 `0xa4`）** |
| `kairui1108/cuktech-ble-server`（持续维护，「对齐米家」） | Phase A（`0xa4` 初始化 → ack → 收密钥交换数据 → 回占位）**全部走通**，再发 `CMD_LOGIN` + `SEND_KEY` → **等不到 `RCV_RDY`**，重试后放弃 |
| 先做 Windows 配对（bond）再跑上面两套 | **结果完全相同** ⇒ 不是「链路未加密」这一条 |

⇒ 设备**收得到我们的写入、也愿意回错误码**，但拒绝完成 Mi 的认证交换。
两个上游实现的开发/测试环境都是 **Linux（BlueZ）**（上游 README 明说），
所以现在的怀疑指向 **Windows/WinRT 这条 BLE 客户端路径**，而不是设备或凭据。
另一条未验证的线索：他们的 Web 端有一个 `/api/xiaomi/beaconkey`「获取 BLE Key」接口
（16 字节）——那份额外凭据是否参与握手，尚未证实。

### 顺手修掉的 5 个真 bug（都会伪装成「设备不行」）

1. **`args.rssi` 在本机 winrt 绑定里不存在** ⇒ 回调一抛异常，整条记录（连地址）被丢弃，
   **明明收到广播也报「0 个」**。改成先记地址、再补次要字段。
2. **`add_aborted` 不存在** ⇒ 上一个会话写的「无 aborted ⇒ 射频通路正常」**从来没被真正检查过**。
3. **vendored 客户端在 `connect()` 自身失败时不断开** ⇒ OS 留着 ACL 链路 ⇒
   充电器（一次只接受一条连接）停止广播 ⇒ 之后一路 `Device with address ... was not found`。
   **设备没坏，是我们自己把它挂死的。**
4. **过滤扫描在本机不可用**：`service_uuids=[FE95]` 与 bleak 内部的
   `find_device_by_address` 都返回 0 / not found，而同一时刻**全量扫描能扫到它**。
   改用「全量扫描 + 把 BLEDevice（或预置地址）交给客户端」。
5. **默认 GATT 缓存模式**在 Windows 上会 `Could not get GATT services: Unreachable`，
   必须 `winrt={'use_cached_services': False}`。

另外两条**环境级**事实（都会让现象看起来像设备问题）：

- **BLE 扫描会卡死，关掉蓝牙再打开即恢复**（同一台机器、同一驱动、5 次运行都是 0）。
  以后遇到「一台都扫不到」先做这个，别先怀疑驱动/硬件。
- **在 Windows 里配对会让 Windows 一直握着连接**（实测 `connection_status=1` 持续 30s+），
  而**已连接的设备不广播** ⇒ 扫描必然扫不到。用 WinRT 的
  `DeviceInformation.pairing.unpair_async()` 可以非管理员解绑，解绑后它立刻恢复广播。

### 协议细节更正（谁接着做必须知道）

`siid=2 piid=6`（息屏时间）的取值，两个实现给的**不一样**：

| 实现 | 取值表 |
|---|---|
| `ohaiibuzzle`（我们 vendor 的那份） | `0:5m, 1:10m, 2:30m, 3:off, 4:1m` |
| `kairui1108/cuktech-ble-server`（维护中） | `1:5min, 2:10min, 3:30min, **4:常亮**, 5:1min` |

⇒ **「常亮」按维护中的那份是 `4`**，不是 `3`。写错会改到别的档位。

### 还剩的三条路（按代价排序）

1. **同一台 PC 上换 Linux 试**（U 盘启动 Live 或装双系统，零采购）：
   上游两套实现都是 Linux/BlueZ 验证过的，用同一块 Intel 网卡、不碰 WinRT。
2. **ESP32 桥**（¥20–40）：`kairui1108/cuktech-ble-esp32` 固件，PC 只走局域网，
   完全不依赖 Windows 的 BLE 栈。这是上游为「Windows 不好用」准备的形态。
3. **改走智能插座**（第五节路线 2）：需要买插座，而且断的是整机 220V。

不建议继续在 Windows 的 BLE 客户端上试错：五关都通了、只差握手，
而握手失败的原因指向平台差异，不是我们改得动的东西。

---

### 顺带的工程改动（第七节的收尾）

- `run_charger_check.bat`：原文件是 **LF 换行 + 混入中文**，cmd.exe 解析时把每行首字符吃掉
  （`echo`→`cho`），已统一为 **CRLF + 纯 ASCII**，并用 `cmd /c call` 实测通过。
- 两个诊断脚本的产物**落盘**：`ble_probe_report.txt`、`charger_probe_report.txt`
  （双击运行时窗口会被卷走，报告留在盘上）。
- `charge_guard.py` 新增**本机覆盖层** `charge_guard.local.json`（已在 `.gitignore`）：
  token 这类密钥写这里，受版本控制的 `charge_guard.json` 保持干净。

---

## 九、路线定案（2026-09-25 深夜）：改走智能插座

第八节把 PC 直连 BLE 这条路判为未走通。机主 随后选定 **路线 2：智能插座断 220V**。
本节记录这次切换做了什么、还缺什么、以及必须接受的代价。

### 为什么是它

| 对比项 | BLE 直控 C1（已放弃） | 智能插座（现在这条） |
|---|---|---|
| 卡点 | 设备拒绝完成 Mi BLE 握手（Windows/WinRT 侧，两个实现都试过） | 无 —— miOT 是**局域网 HTTP 协议**，python-miio 成熟且与平台无关 |
| 需要额外硬件 | 不需要 | **需要**（¥30–60，必须选 Wi-Fi 版） |
| 只切手机那一路 | ✅ | ❌ **整机断电**（C2/C3/A 上的设备一起断） |
| 每次通断 | 无上电冲击 | 对 120W 电源一次上电冲击（inrush） |
| 代码 | `ble_plug.py`（保留备用） | `charge_guard.py::PlugLink`（已完成大半） |

### 已做（本次会话）

- `python-miio 0.5.12` 装进**项目自己的 venv**（`C:\Users\zhiya\.workbuddy\binaries\python\envs\default`），
  `miiocli` 可用。
- `charge_guard.json` 的 `actuator` 改为 `miio`；`charger_ble` 段保留（备用）。
- `--probe-plug` 的枚举范围从 `siid 1-10 × piid 1-10` **扩到 1-16 × 1-16** ——
  米家智能插座3 的属性排到 siid 15，旧范围会漏掉功率（11）与指示灯（13）。
- 文档/注释里写进了 **cuco.plug.v3 的预期编号**（开关 2/1、实时功率 11/2、累计电量 11/1），
  仍然要求以真机 `--probe-plug` 为准。
- 自测仍 **54/54**；`--actuator miio --dry-run --once` 可正常跑（会明确提示「插座未配置，本次只读数」）。

### 还缺什么

1. **插座本体**：必须是 **Wi-Fi 版（miOT 局域网可控）**。
   ⚠️ 不要买「蓝牙 Mesh」版（那类要靠网关转发，等于又回到蓝牙那条路）。
   参考：米家智能插座3（`cuco.plug.v3`）—— 10A/2500W，带电量统计与「充电保护」，足够 120W 充电器。
2. **ip + token**：`miiocli cloud` 登录小米账号（可能又走一次风控验证）→ 抄下插座的 ip 与 token。
   token 写进 `charge_guard.local.json`（不进 git），ip 写 `charge_guard.json`。
3. **跑一次 `--probe-plug`**（只读）确认真机编号，再填 `on_siid/on_piid`（预期 2/1）
   与 `power_siid/power_piid`（预期 11/2）。
4. **读端要恢复**：写这份文档时手机 `192.168.0.120` **ping 不通**（ADB 缓存端口 41723 是 18:24 的），
   需要手机在 Wi-Fi 上、且**无线调试**开着，guard 才读得到电量。

### 断电后 `AC powered` 的回读 —— ✅ 2026-09-25 深夜实测通过

| 阶段 | 手机 | 插座 |
|---|---|---|
| 断电前 | `AC powered: true` / `status: 2` / 100% | on=True，6 W |
| 断电后 8s | `AC powered: false` / `status: 3` | on=False，0 W |
| 恢复后 10s | `AC powered: true` / `status: 2` | on=True，7 W |

⇒ 「断 220V 能停充电、恢复能继续充」成立；写→回读确认这条链同时在真机上跑通。
- **插座属性编号**：上表是 cuco.plug.v3 的官方 spec，真机 `--probe-plug` 为准（型号不同编号会变）。
- 插座自带「充电保护」（按功率自动断）与「快捷倒计时关闭」（siid 8）都没试过，
  可以作为**兜底保险丝**（例如「通电 90 分钟后无条件断电」），但需要单独验证。

### 顺带保留的资产

- `ble_plug.py` + `charger_ble` 配置 + `patches/0001`（含 ad1204_ble 的两处修复）**不删**：
  换 Linux 或上 ESP32 桥时，第八节记录的五个坑和这些补丁仍然有效。
- `patches/test_fetch_tokens_2fa_offline.py`（5/5）与 `ble_probe.py`（现已如实报告）
  也都是下次接着干的工具。

---


## 参考

- `ohaiibuzzle/xiaomi-ad1204-python` — https://github.com/ohaiibuzzle/xiaomi-ad1204-python
- `kairui1108/cuktech-ble-ha` — https://github.com/kairui1108/cuktech-ble-ha
- 拆解（确认 3C1A / 米家蓝牙接入）— https://www.ednchina.com/technews/34867.html
- 首款可接入米家的充电器（OTA 增加多端口协议控制）— https://news.mydrivers.com/1/1068/1068361.htm
- 小米官方：蓝牙网关只能看状态/联动，**不能控制**子设备；远程控制需要蓝牙 Mesh 网关
  — https://cdn.cnbj1.fds.api.mi-img.com/ics-resources/articles/60bf1abd20a523e5ed34b663.html
