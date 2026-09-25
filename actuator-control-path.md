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
（2026-09-25 由 Luna 在手机上确认）。

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

**下一步（30 秒，交给 Luna）**：把充电器拿到 PC 旁边 1 米内 → 点亮屏幕/插着手机让它有负载
→ 关掉手机上的米家 App → 双击 `run_ble_probe.bat`。
报告里若出现 `njcuk.fitting.ad1204`，路径 A 就成立了，可以继续去拿 token。

### ★★ 但扫描其实不该拦路：手机能连 ≠ 我需要能扫到

Luna 问「为什么我 K70 Pro 都能轻易连上？」—— 关键在于**两侧用的不是同一个动作**：

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
  （→ 需要 Luna 确认：这台充电器其他 3 个口现在插着什么？）
- **另一个代价**：每次通断都是对 120W 电源的一次上电冲击（inrush）。
  按 65/50 的 15% 摆幅、日均耗电 20–45% 推算约每天 1–3 次，可接受；
  如果想更少通断，可以把摆幅放宽（峰值电压会上去，是取舍）。
- 注意：**Python-miio 对 ZNCZ401KK（小米智能插座4）是否已支持也还没验证** ——
  需要 `miiocli` 能否认出该型号，认不出就装 python-miio 开发版。

---

## 四、待验 / 待确认清单

| # | 事项 | 谁来做 | 阻塞了什么 |
|---|---|---|---|
| 1 | 把充电器拿到 PC 旁 1 米内 + 点亮屏幕 + 关掉米家 App，双击 `run_ble_probe.bat` | 我出工具，Luna 跑 | 路径 A 是否成立（环境问题已排除） |
| 2 | **关掉 C1 后充电器还广播吗** | 我测 | 路径 A 是否有致命缺陷 |
| 3 | 其他 3 个口现在插着什么 | Luna | 路径 B 是否可用 |
| 4 | 有没有智能插座（ZNCZ401KK 或别的） | Luna | 路径 B 的硬件前提 |
| 5 | **走哪条路线**（第五节四条） | Luna 定 | 全部后续工作 |

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
且每次通断是对 120W 电源的一次上电冲击。虽然最简单，但它牺牲的正是 Luna 最在意的"只切第一个口"。

---

## 六、最终决定：不买硬件，PC 用自带蓝牙直连（2026-09-25 收尾）

Luna 明确「不想买」。于是回到路径 A 的**纯软件形态**：PC 的蓝牙适配器已验证可用
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

### 剩下的一步（只能 Luna 本人做）

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

## 参考

- `ohaiibuzzle/xiaomi-ad1204-python` — https://github.com/ohaiibuzzle/xiaomi-ad1204-python
- `kairui1108/cuktech-ble-ha` — https://github.com/kairui1108/cuktech-ble-ha
- 拆解（确认 3C1A / 米家蓝牙接入）— https://www.ednchina.com/technews/34867.html
- 首款可接入米家的充电器（OTA 增加多端口协议控制）— https://news.mydrivers.com/1/1068/1068361.htm
- 小米官方：蓝牙网关只能看状态/联动，**不能控制**子设备；远程控制需要蓝牙 Mesh 网关
  — https://cdn.cnbj1.fds.api.mi-img.com/ics-resources/articles/60bf1abd20a523e5ed34b663.html
