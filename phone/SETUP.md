# 手机端部署手册（K70 Pro + Termux + 小米智能插座）

目标形态：**手机自己当大脑** —— 读自己的电量、自己判定、局域网直接控插座。
**PC 完全不参与，也不需要无线 ADB**（ADB 只用来推一次文件，之后彻底不用）。

```
手机 ──(本机电量 termux-battery-status)──┐
                                        ├─► 判定（65/50 滞回 + 温度 + 稳定门）
                                        └─► miIO/UDP(54321) ──► 智能插座 ──220V──► 充电器 ──► 手机
```

**为什么不是 BLE 直控 C1 口**：那条路在 Windows 侧走不通（设备拒绝完成 Mi BLE 握手），
详见仓库根目录 `actuator-control-path.md` 第八节。插座这条路的代价是**整机断电**
（充电器四个口一起断），换来的是「协议成熟 + 手机自己就能干 + 不依赖 PC 的蓝牙与 ADB」。

---

## 进度（2026-09-25 深夜更新）

| 步骤 | 状态 |
|---|---|
| 手机上装 Termux / Termux:API | ✅ 已用 ADB 装好（0.118.3 / 0.53.0） |
| 手机端脚本（判定逻辑） | ✅ `--self-test` **21/21 通过** |
| **执行端：局域网控插座** | ✅ 已写完，并在 PC 上对真机验证（握手 + 读开关/功率/故障） |
| **Python 依赖** | ✅ **零第三方依赖**：miIO 协议与 AES-128 都用标准库实现 |
| 插座凭据 | ✅ 已取到（`cuco.plug.v3 @192.168.0.99`），编号已按真机核对 |
| 推到手机 + 跑起来 | ✅ **2026-09-25 22:26 已在手机上正式运行** |
| 「断电后手机是否真的不再充电」 | ✅ **已实测通过（2026-09-25 深夜）**，数据见第七节 |

### 执行端为什么不装 python-miio

手机里装 `python-miio` / `cryptography` 都可能踩编译坑，所以把 miIO 协议（UDP 握手 +
AES-128-CBC 加密请求）用**标准库**实现了（`charge_guard_phone.py` 里「miIO 协议层」那一段）。
正确性有三层独立证据：

- **AES 对拍**：与 `cryptography` 的 AES-CBC 对拍 300 组加密 + 300 组解密，**零差异**；
- **权威已知向量**：NIST FIPS-197 附录 B 的 AES-128 单块向量（`69c4e0d8…c55a`）写进了自测；
- **真机**：对 `192.168.0.99` 完成握手，读到 开关=`True` / 功率=`0` / 故障=`0`。

⇒ 手机上只需要 **Termux 自带的 python**，`pkg install` 一个第三方库都不用。

---

## 一、插座侧（一次性）

1. **插座必须是 Wi-Fi 版（miOT 局域网可控）**，不要蓝牙 Mesh 版。当前是
   米家智能插座3（`cuco.plug.v3`），编号已验证：开关 `2/1`、实时功率 `11/2`、
   故障 `2/3`、插座温度 `12/2`。
2. **把充电器插到这个插座上**。⚠️ 断电时充电器的 C2/C3/A 会一起断（已确认可接受）。
3. 路由器上给插座**绑静态 IP**（当前 `192.168.0.99`），否则 IP 变了就连不上。
4. 手机必须和插座在**同一个 Wi-Fi**。

---

## 二、Termux 里的一次性初始化

打开 Termux，依次执行：

```bash
termux-setup-storage                 # 出现系统弹窗要点允许
pkg update -y && pkg upgrade -y
pkg install -y python termux-api     # ⚠️ 不需要 pip 装任何东西
```

把三个文件放到 `~/charge_guard/`：

| 文件 | 说明 |
|---|---|
| `charge_guard_phone.py` | 主程序（判定 + miIO 执行端，纯标准库） |
| `charge_guard_phone.json` | 配置（可进 git 的那份，**不含 token**） |
| `charge_guard_phone.local.json` | **本机覆盖层：插座 token + 日志路径**（不进 git） |

推送方式（任选）：

- **ADB**（一次性，推完就不再用；无线调试掉线时重开即可）：
  ```bash
  adb push phone/charge_guard_phone.py         /sdcard/charge_guard/
  adb push phone/charge_guard_phone.json       /sdcard/charge_guard/
  adb push phone/charge_guard_phone.local.json /sdcard/charge_guard/
  # 手机上再：cp /sdcard/charge_guard/* ~/charge_guard/
  ```
- 或者用你自己的 TailShare 传到手机，再 `cp` 进 `~/charge_guard/`。

`charge_guard_phone.local.json` 的内容（token 用插座那条，32 个 hex 字符）：

```json
{ "plug": { "token": "你的插座token" } }
```

---

## 三、跑起来

```bash
cd ~/charge_guard

python charge_guard_phone.py --self-test           # 21 项（含 AES 已知向量），不需要插座
python charge_guard_phone.py --show-battery        # 看一次原始电量（确认字段名与单位）
python charge_guard_phone.py --probe               # 只读探测插座：握手 + 开关/功率/故障
python charge_guard_phone.py --dry-run --ticks 3   # 只判定不控电
python charge_guard_phone.py                       # 正式运行
```

**字段确认要点**：`termux-battery-status` 的 `temperature` 单位是 **摄氏度**（如 `33.7`），
而 PC 侧 `dumpsys battery` 是 **0.1℃**（`337`）。脚本按摄氏度处理，**不要再除以 10**。
自测里有断言钉住这件事（误除 10 会让 45℃ 阈值形同虚设）。

### 手机上的实际落位（2026-09-25 部署结果）

| 项 | 值 |
|---|---|
| 代码目录 | `~/charge_guard/`（3 个文件已拷入） |
| python | 用 `pkg install -y python` 装好（含依赖约 97 MB） |
| 存储权限 | `appops set com.termux MANAGE_EXTERNAL_STORAGE allow`（否则读不了 /sdcard） |
| 日志 | `/sdcard/charge_guard/guard.out`、`/sdcard/charge_guard/phone_guard_log.csv`（放在共享存储，方便外部读） |
| 自启脚本 | `~/.termux/boot/charge_guard.sh`（⚠️ **需要手动打开一次 Termux:Boot 应用**才会生效） |
| 已写入的系统设置 | `wifi_sleep_policy=2`、`com.android.shell` 进 Doze 白名单 |

---

## 四、常驻与开机自启

```bash
termux-wake-lock                                   # loop.wake_lock=true 时脚本也会自己调
cd ~/charge_guard && nohup python charge_guard_phone.py >> guard.out 2>&1 &
tail -f ~/charge_guard/guard.out
```

**开机自启（Termux:Boot）**：

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/charge_guard.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/charge_guard && python charge_guard_phone.py >> guard.out 2>&1 &
EOF
chmod +x ~/.termux/boot/charge_guard.sh
```

然后**手动打开一次 Termux:Boot 应用**（它需要被系统记录一次才会生效）。

---

## 五、澎湃 OS 保活（不做这步会被杀）

这台是 HyperOS，后台管控很激进。逐项设置：

1. **设置 → 应用设置 → 应用管理 → Termux → 省电策略 → 无限制**
2. 同页 → **自启动 → 允许**（Termux、Termux:API、Termux:Boot 三个都要）
3. 同页 → **权限 → 后台弹出界面 → 允许**（部分版本需要）
4. **设置 → 电池 → 应用智能省电**里把 Termux 移出省电名单
5. 顺手：**开发者选项 → 充电时保持唤醒**。理由不是防锁屏，而是**防 Wi-Fi 省电**：
   手机息屏久了 Wi-Fi 会进省电，而控插座走的是局域网 UDP，同样会受影响。

---

## 六、排错

| 现象 | 原因与处置 |
|---|---|
| `termux-battery-status: command not found` | 没 `pkg install termux-api`，或 Termux:API 应用没装 |
| `termux-battery-status` 无输出 | Termux:API 应用被冻结/没授权；去应用管理里允许自启动 |
| `percentage 不可信: None` | 脚本拒绝把未知当 0 或 100（故意的）。检查 Termux:API 权限 |
| `[plug] 未配置 ip/token，拒绝控电` | `charge_guard_phone.local.json` 没放对位置，或 token 不是 32 位 hex |
| `MiioError: 握手响应异常` / UDP 超时 | 插座 IP 变了（去路由器绑静态 IP）、手机不在同一 Wi-Fi、或插座被断电 |
| `token 必须是 32 个 hex 字符` | 把充电器的 BLE token（24 位）错当成插座 token 了 |
| 切了插座但充电没停 | 用 `--probe` 确认 `开关` 真的变了；再看手机自己的 AC 状态（第七节） |
| 跑几小时后停了 | 保活没做（第五节）；看 `guard.out` 有没有被 kill |

---

## 七、还没验的（别当成品用）

1. ~~「断电后手机是否真的不再充电」~~ ✅ **已实测通过（2026-09-25 23:0x）**：

   | 阶段 | 手机（`dumpsys battery`，与 termux-battery-status 同源） | 插座 |
   |---|---|---|
   | 断电前 | `AC powered: true`，`status: 2`（充电中），100% | on=True，**6 W** |
   | 断电后 8s | `AC powered: false`，`status: 3`（放电中） | on=False，**0 W** |
   | 断电后 15s | `AC powered: false`，`status: 3` | — |
   | 恢复通电 10s | `AC powered: true`，`status: 2` | on=True，**7 W** |

   结论：**断 220V 确实能停住充电、恢复后确实继续充**——整条路线的核心假设成立。
   同一次操作也顺带验掉了「写 → 回读确认」这条链（日志里会打印「已回读确认」）。
2. **插座自带的「充电保护」与「倒计时关闭」**（`siid 4` / `siid 8`）没试过，
   可以当独立保险丝（例如「通电 90 分钟后无条件断电」），但要单独验证。
3. **长期可靠性**：手机侧脚本目前只在 PC 上跑过自测与真机只读探测，
   在手机 Termux 里连续跑几天才算数。
