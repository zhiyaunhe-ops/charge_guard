# charge_guard —— 常驻插电安卓设备的充电区间守护

用小米智能插座通断，把一台**常驻家中、长期接插座**的 Redmi K70 Pro（当安卓模拟器的备用机）
控制在中间电量区间，避免电池长时间停在满电。

策略依据与推导过程见 `charge-strategy-review.md`（含反例与数据来源）。

---

## 当前状态（2026-09-25）

| 部分 | 状态 |
|---|---|
| 端口发现（缓存 → mDNS → 扫描）+ 功能探针 | ✅ **真机验证通过**（K70 Pro @192.168.0.120） |
| `dumpsys battery` 解析 | ✅ **真机验证通过**（含温度 0.1℃ 换算、AC powered） |
| 判定逻辑（65/50 滞回、温度、稳定性门、失联分级） | ✅ 桩数据自测 **54 项全通过**（`--self-test`） |
| 真机 dry-run 全链路 | ✅ 通过，记录见 `dryrun-real-device.txt` |
| **BLE 执行端（直控充电器 C1 口）** | 🔴 **PC 直连未走通（2026-09-25 深夜结论）** —— 凭据/发现/连接/订阅/写入五关全通，卡在设备拒绝完成 Mi BLE 登录握手（ohaiibuzzle 版回 `e0000000`；维护中的 kairui1108 版 Phase A 走通但等不到 `RCV_RDY`；先绑定再试结果相同）。详见 `actuator-control-path.md` 第八节：下一步是「同一台 PC 换 Linux」或「ESP32 桥」 |
| **断电后的 `AC powered` 回读** | ⛔ **未验证** —— dry-run 没真正断电 |
| 端口掩码写入并回读确认 | 🟡 逻辑已实现（`ble_plug.py::set_power`），待真机验 |
| ~~插座通断指令（python-miio）~~ | ⏸ 已放弃该路线：不需要买插座，改用 BLE 直控 |
| 告警通道（webhook / smtp） | ⛔ **未验证** —— 当前配置是 `console` |
| 跨天汇总、日志裁剪、300s 正式间隔 | ⛔ **未验证** —— 只在 3s 间隔下跑过几轮 |

> 也就是说：**读的那一半已经在真机上通了，写的那一半还没有。** 别当成可以直接上线的成品。

---

## 文件

| 文件 | 说明 |
|---|---|
| `charge_guard.py` | 主脚本（单文件，无第三方依赖也能跑 `--self-test`） |
| `charge_guard.json` | 配置；阈值、路径、周期都在这里，不硬编码 |
| `adb_port_cache.json` | 自动生成：上次连通端口（脚本自己写，不用手改） |
| `charge_guard_log.csv` | 自动生成：逐次采样 + 动作 + 每日汇总 |
| `selftest-output.txt` | 自测证据（54 项） |
| `dryrun-real-device.txt` | 真机 dry-run 证据（含 CSV 原始行） |

---

## 快速开始

```bash
# 0) 先确认脚本本身没问题（不需要真机、不需要插座）
python -X utf8 charge_guard.py --self-test

# 1) 填配置：phone.ip、adb_path（建议写全路径）、plug.*
#    本机 adb 全路径：D:\MuMuPlayer-12.0\nx_main\adb.exe

# 2) 只读数不控电，确认真机通路
python -X utf8 charge_guard.py --dry-run

# 3) 定位插座的 siid/piid（只读，不会切来切去）
python -X utf8 charge_guard.py --probe-plug

# 4) 正式运行
python -X utf8 charge_guard.py
```

排错用 `--ticks N`（跑 N 轮就退出）、`--once`（跑一轮）。

---

## 策略与依据

```
stop_at   = 65   达到即断电
resume_at = 50   回落到此才通电
温度 >= 45℃ 强制断电，降到 <= 40℃ 才恢复
```

依 Battery University BU-808（NMC 圆整估算，**不是本机电芯实测**）：

| 停止充电电压 | 约等于 SoC | 循环寿命 |
|---|---|---|
| 4.20 V | 100% | 300–500 |
| 4.06 V | ~81% | 600–1 000 |
| 3.92 V | ~65% | 1 200–2 000 |

停 65% 而不是 80%，换到的是「峰值电压更低 + 摆幅更窄（15% vs 45%）」，
而两者的**平均 SoC 都约 57%**，所以日历老化项相当 —— 差别只在前两项，都利好窄区间。

⚠️ 这条改动的量级是「方向明确、幅度未知」：BU 的 Table 3 只给了 40% 与 100% 两列，
65% 落在两者之间但没有数据点。**要验证它到底有没有用，靠 `charge_guard_log.csv` 攒下来的长期趋势。**

### 一次性满充

平时停在 65%/50%。需要抓起就走时，在脚本目录建一个 `full_charge.once` 文件：

```bash
touch full_charge.once      # Windows: type nul > full_charge.once
```

目标会临时提到 100%，**SoC ≥ 98% 或检测到拔线后自动删除标志并回落 65%**。
理由：100% 的危害来自**停留时间**，充满立刻用掉是唯一近乎无害的满充方式。

---

## 加固项 → 代码位置

| # | 加固项 | 代码位置 |
|---|---|---|
| 1 | 端口三层兜底（缓存 → mDNS → 扫描 30000–50000） | `AdbLink._cached_port` / `_mdns_ports` / `_scan_ports` / `ensure_session` |
| 2 | 失败后清 adb daemon（残留 offline transport 会让后续 connect 全失败） | `AdbLink._try_connect` 里的 `need_reset` 分支 |
| 3 | 同一候选端口试 N 次（息屏 Wi-Fi 省电会随机丢 SYN） | `connect.attempts_per_port` × `_try_connect` 循环 |
| 4 | 在线判定用功能探针，不看 `adb devices` | `AdbLink._probe`（`connect.probe_cmd`） |
| 5 | 严格解析（level 正则 + 0..100、temperature 除以 10） | `AdbLink.parse_battery` |
| 6 | 执行器回读（发完指令回读 `AC powered`） | `Guard._actuate` |
| 7 | 失联分级（短断保持 / 充够就断 / 未知不动 / 超时兜底） | `Guard._handle_lost` + `lost_contact` 配置 |
| 8 | 趋势日志（逐次 + 每日汇总 + 保留 N 天） | `CsvLog` |
| 9 | UTF-8 钉死 | `pin_utf8()` |
| 10 | `--dry-run` | `PlugLink.set_power` 里 **first-line** 判 dry_run |
| + | 稳定性门改为「连续同判定」而非「读数相同」 | `Guard._same_decision` |
| + | 一次性满充标志 | `Guard.refresh_flag` |
| + | 温度锁存（避免 40–45℃ 间反复通断） | `apply_temp_latch` + `decide` 的第 2 条规则 |

### 判定优先级（`decide`）

```
0) 读数无效            → hold         ← 最关键：未知绝不动作
1) 温度 >= 45℃         → off（立即，不等稳定计数）
2) 温度曾超标且仍 >40℃  → off（滞回）
3) level >= stop_at    → off
4) level <= resume_at  → on
5) 其余                → hold
```

`decide()` 是**纯函数**，所以自测能脱离真机穷举边界 —— 这也是为什么自测输出是有意义的证据。

---

## 插座属性探测（唯二剩下的卡点之一）

ZNCZ401KK 是较新型号，`python-miio` 可能没有专用类，所以脚本**不预设任何 siid/piid 编号**。

```bash
pip install -U python-miio
# 若认不出这款插座，装开发版：
pip install git+https://github.com/rytilahti/python-miio.git

miiocli cloud                 # 输小米账号 → 列出所有设备的 IP 与 token
```

把 IP/token 填进 `charge_guard.json` 的 `plug.ip` / `plug.token`，然后：

```bash
python -X utf8 charge_guard.py --probe-plug
```

它只读枚举 siid 1–10 × piid 1–10，**不发任何写指令**（不会把插座切来切去）。
认法：布尔值 → 大概率是开关，填 `plug.on_siid` / `on_piid`；带小数的数值 → 功率，填 `power_siid` / `power_piid`。

### ⚠️ token 是密钥，不要提交进 git

`token` 可以用环境变量注入，会覆盖配置文件里的值：

```bash
# Windows PowerShell
$env:CHARGE_GUARD_PLUG_TOKEN = "..."
# Linux / macOS
export CHARGE_GUARD_PLUG_TOKEN=...
```

仓库里 `charge_guard.json` 的 `plug.token` 保持空字符串即可。

---

## 两个真机踩到的坑

**1. 充电中的 `voltage` 不能用来推 SoC。**
2026-09-25 实机同一台机器：静置 67% 时 3923 mV，充电到 68% 时 4351 mV。
差 400 mV 全是内阻压降。电压法只在**静置**时才近似成立，判 SoC 一律用 `level`。

**2. `temperature` 单位是 0.1℃。**
45℃ 在输出里是 `450`。少除一次 10，阈值就从 45℃ 变成 4.5℃，永远触发不了。

---

## 运行方式

```bash
# Linux / macOS
nohup python3 -X utf8 charge_guard.py > /dev/null 2>&1 &

# Windows：计划任务或 pythonw（避免弹黑框）
pythonw.exe charge_guard.py
```

⚠️ **Windows 上要注意**：把 `adb connect` 与后续操作写在同一条命令里的做法，
在交互式会话里没问题；但计划任务里 adb daemon 的生命周期由任务决定 ——
本脚本每轮都会自己 `ensure_session()`，不做常驻连接，所以这点是安全的。

---

## 执行端：BLE 直控充电器 C1 口（**不需要买任何硬件**）

原来的设计假设执行端是一个小米智能插座。实际上**这台充电器自己就能按口开关**，
而且它的 BLE 协议已有开源实现 —— 所以 PC 用**自带蓝牙适配器**直接连它就行，
不用买插座、也不用买 ESP32。

### 协议要点（来自两个开源实现，交叉确认）

| 项 | 值 | 依据 |
|---|---|---|
| 端口开关属性 | `siid=2, piid=16`，值是 4 位掩码 | `cuktech-ble-ha/ble_server/ble_manager.py`：`send_miot_command(2, 16, value=new_val)` |
| 位定义 | `c1=bit0, c2=bit1, c3=bit2, a=bit3` | 同仓库 `state.py`：`PORT_BITS = {"c1":0,"c2":1,"c3":2,"a":3}` |
| 全开 / 全关 | `0x0F` / `0x00` | 同仓库 `ble_manager.py` |
| 登录凭据 | 设备 token（**12 字节 hex = 24 字符**） | 两个实现的 README |

`ble_plug.py` 不重复实现协议，而是调用 vendored 的
`third_party/xiaomi-ad1204-python/ad1204_ble.py`（MIT，已真机验证过的实现）。
**每次下发都会先读当前掩码再改目标位**，不会误动其他口；
且同一次连接里完成「写 → 回读全部属性 → 确认」，省一次 BLE 往返。

### 一次性准备

```bash
# 1) 拉第三方实现（不进版本库，见 .gitignore）
git clone https://github.com/ohaiibuzzle/xiaomi-ad1204-python.git third_party/xiaomi-ad1204-python

# 2) 装依赖（用托管 venv，别污染系统环境）
C:/Users/zhiya/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
  -m pip install cryptography pycryptodome rich colorama

# 3) 取 token —— 这一步只能你本人做（要登录小米账号，可能走 2FA/图形码）
cd third_party/xiaomi-ad1204-python
python fetch_tokens.py --region cn
#   输出里找 njcuk.fitting.ad1204 那一条，抄下 address 与 token

# 4) 注入凭据（不进 git）—— 两种方式任选
#    a) 写进本机覆盖层 charge_guard.local.json（已在 .gitignore 里，最省事）：
#       {"charger_ble": {"token": "<24位hex>"}}      ← 会覆盖 charge_guard.json 的同名键
#    b) 环境变量（优先级最高）：
export CHARGER_BLE_TOKEN=<24位hex>
#       Windows PowerShell:  $env:CHARGER_BLE_TOKEN = "<24位hex>"
#    address 填进 charge_guard.json 的 charger_ble.address（不是密钥，可以进 git）
```

### 验证顺序（别跳步）

```bash
python -X utf8 charge_guard.py --probe-charger          # 连一次，dump 全部属性，确认掩码能解析
python -X utf8 charge_guard.py --actuator ble --dry-run --ticks 3
python -X utf8 charge_guard.py --actuator ble           # 正式
```

### ⚠️ 三个必须知道的坑

1. **充电器一次只接受一个 BLE 连接** —— 用脚本时**手机上的米家 App 必须关掉**，
   否则两边互相挤。反之手机打开米家，脚本会连不上。
2. **充电器息屏会停止广播** —— 那时脚本连不上（`--probe-charger` 会超时）。
   让它有负载/屏幕亮着即可。
3. **「关掉 C1 之后还连不连得上」必须先实测。** 如果关掉 C1 后没有别的负载、
   充电器进入空闲息屏并停播，就会出现「**关得掉、开不回来**」。
   实测方法：`--probe-charger` 看到掩码 → 关掉 C1 → 等 5 分钟 → 再 `--probe-charger`。
   **在验掉这条之前，不要让它无人值守地真控电。**

---

## 变更记录

- **2026-09-25 v1** —— 初版。相比原方案（80/35）的三处实质改动：
  1. 档位改为 **65/50**（常驻插电设备，不是随身机）；
  2. 稳定性门从「两次读数相同」改为「连续同判定」（真机 dry-run 发现前者会被单调变化的电量卡死）；
  3. 失联兜底从「一律保持通电」改为分级 + 12h 上限（一律保持通电等于把主目标丢掉）。
