# 执行端选型：怎么把「停 65%」这个动作落到硬件上

状态：**未定**。本文记录两条候选路径、已验证的证据、以及必须先验掉的失败模式。
日期：2026-09-25

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
| 5 | 要不要上 ESP32 桥 | Luna | 路径 A 的稳健形态 |

---

| 文件 | 说明 |
|---|---|
| `ble_probe.py` | BLE 探测：适配器能力 → 监听器状态 → 广播扫描，逐行落盘 |
| `run_ble_probe.bat` | 双击入口（CRLF / ANSI / 纯 ASCII 文件名），跑完自动打印报告 |
| `ble_probe_report.txt` | 运行产物（已 gitignore，不进版本库） |

---

## 参考

- `ohaiibuzzle/xiaomi-ad1204-python` — https://github.com/ohaiibuzzle/xiaomi-ad1204-python
- `kairui1108/cuktech-ble-ha` — https://github.com/kairui1108/cuktech-ble-ha
- 拆解（确认 3C1A / 米家蓝牙接入）— https://www.ednchina.com/technews/34867.html
- 首款可接入米家的充电器（OTA 增加多端口协议控制）— https://news.mydrivers.com/1/1068/1068361.htm
- 小米官方：蓝牙网关只能看状态/联动，**不能控制**子设备；远程控制需要蓝牙 Mesh 网关
  — https://cdn.cnbj1.fds.api.mi-img.com/ics-resources/articles/60bf1abd20a523e5ed34b663.html
