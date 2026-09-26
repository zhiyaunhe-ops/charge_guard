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

## 四之二、自愈看门狗（2026-09-25 实装，**这一层比保活设置更可靠**）

实测：**从最近任务划掉 Termux，澎湃 OS 会杀掉整个应用进程，里面的 python 一起死**
（日志证据：CSV 停在 22:31:06，之后 python 进程数从 1 变 0）。`nohup` + 唤醒锁挡不住这个。

所以加了 Android 原生的一层自愈：

```bash
# 已装好（脚本在 ~/charge_guard/ensure_running.sh）
termux-job-scheduler --script ~/charge_guard/ensure_running.sh   --job-id 1 --period-ms 900000 --persisted true
# 查看已登记任务
termux-job-scheduler --pending
```

- **每 15 分钟**（Android N 起的最小周期）由系统唤醒 Termux 跑一次 `ensure_running.sh`；
  发现守护不在就拉起来，日志写 `/sdcard/charge_guard/watchdog.log`。
- `--persisted true` ⇒ **重启后任务仍在**（配合 Termux:Boot 双保险）。
- 代价：被杀后最多 15 分钟无人管理。**这段时间里的安全性由「插座保持最后状态」兜住**
  （死在断电之后就不会再充；死在通电之后最多把电充到 100%，不会更糟）。

---

## 四之四、采样间隔决定了「冲过头」多少（2026-09-26 实测）

**现象**：目标 65% 停，但实测冲到 **71%/74%** 才断电 —— 看着像「没停」。

**原因**：不是判据错，而是 `loop.interval_sec` 原来设的是 **300 s**。
手机在 40–60 W 快充下，5 分钟能涨约 5%：
guard.out 里 13:44:38 采样到 64%（区间内 → 不动），下一次是 13:49:40，已经 74% → 才断电。
**两次采样之间越过阈值，是必然会发生的**，越障高度 ≈ 充电速率 × 采样间隔。

**处置**：`interval_sec` 由 300 改为 **60**（手机侧读本机电量、控局域网插座，1 分钟一次成本可忽略），
越障高度随之降到约 1%。改完日志立刻显示 `间隔=60s`。

**推论**（要更准就往这个公式调）：越障 ≈ 速率 × 间隔。若想贴住 65%，间隔 ≤ 60 s 就够；
若想减少写插座的次数，可以把 `stop_at` 设低一点（例如 63）来抵消越障，而不是拉长间隔。

---

## 四之三、被 MIUI「强制停止」怎么办（2026-09-26 实测案例）

**实况**：手机端守护 22:33 起来后一直跑到 **00:58** 被杀；手机侧看门狗日志最后一条是 **23:06**，
之后 14 小时一次都没跑；`termux-job-scheduler --pending` 返回 **"No jobs found"**。
⇒ 结论：**MIUI 把 Termux 强制停止时，Android 会连它的 JobScheduler 任务一起取消**
（并且此后收不到 BOOT_COMPLETED，Termux:Boot 也失效），直到有人再次启动该应用。
所以「手机侧自愈」这一层是会被一次性废掉的。

**代价**：那段时间守护不在 ⇒ 插座保持最后状态 ⇒ 手机从 98% 掉到 39% 一直没充上
（用户插上线时无人接管）。这不是危险，但显然不符合预期。

### 两层补救（已实装）

1. **`~/.bashrc` 钩子**（手机上）：Termux 一被启动，就自动执行
   `~/charge_guard/ensure_running.sh`，守护随之回来。
   ⇒ 于是「如何启动 Termux」成了唯一需要外部解决的问题。
2. **PC 侧看门狗**（`run_watch_from_pc.bat` / `phone/watch_from_pc.py`）：
   每 interval 秒检查一次；`am start com.termux` **可以解除强制停止状态**，钩子接管拉起守护。
   若始终救不回来，还有兜底：能读到电量时按策略直接控插座
   （≤resume_at 通电防耗尽，≥stop_at 断电防顶满）。

⚠️ PC 侧看门狗要求手机的**无线调试开着**；PC 关机时它不在岗（那时只能靠上面第 1 层 + 手点 MIUI 设置）。

**PC 侧看门狗的两条铁律**（2026-09-26 实测踩到后加的）：

1. **连接必须过探针**：`adb devices` 里一条 `offline` 残留会让 `adb connect` 回「connected」，
   但之后所有读取都失败 → 看门狗会误报「守护不在」，还因为读不到电量走了兜底分支。
   所以连上后先跑 `echo PROBE_OK` 验证，通过才算可用。
2. **连不上 / 读不到状态时，只能记「无法判断」并跳过** —— 绝不能据此判死或去动插座。
   （手机端守护是自治的，ADB 断不影响它运行。）

### 实测：进游戏 / 游戏加速清理**不会**杀掉它（2026-09-26，20 分钟监控）

在《Skullgirls》全程前台（`com.autumn.skullgirls`）+ 游戏加速执行过清理的情况下：

- 监控每 30 秒一次，**38 次报活、0 次真死**；
- **PID 全程只有 3865 一个**（一次都没被重启过）；
- 守护自己的采样每 60 秒一条，全程连续（13:58 → 14:19），电量 72% → 65%；
- 期间出现过 1 次「失联」误报（14:08:05），但**前后两次检查都是同一个 PID**，
  该次 adb shell 整体返回空 —— 属于**测量失败**，不是守护死亡。这也是为什么判活要改看 CSV 新鲜度。

⇒ 结论：**进游戏不影响**；真正的杀手是「把 Termux 从最近任务划掉」（那会杀掉整个应用进程）。

### 判活：只认「采样文件新鲜度」（2026-09-26 定稿）

| 写法 | 结果 |
|---|---|
| `ps -A \| grep charge_guard_phone.py` | **假阴性** —— Android 的 ps 只显示进程名 `python` |
| 裸 `pgrep -f charge_guard_phone.py` | **假阳性** —— 调用者自己命令行里含这个字符串，会自匹配 |
| `grep -qa ^python /proc/<pid>/cmdline` | **在 adb shell 里假阴性** —— 跨 UID 读不到别的应用的 cmdline（游戏期间实测误报过一次） |
| ✅ **看 `/sdcard/charge_guard/phone_guard_log.csv` 的时间戳** | 可靠：守护每 `interval_sec` 秒写一行，超过 180 s 没更新才算不在 |

两个看门狗（手机侧 `ensure_running.sh`、PC 侧 `watch_from_pc.py`）现在都改用第 4 种。
**这套判活顺带能抓到「进程活着但卡住」**——那是最难查的一类故障。

另外 `ensure_running.sh` 现在**自更新**：发现 `/sdcard/charge_guard/ensure_running.sh` 比自身新，
就自我替换并重跑。原因是 adb shell 写不进 Termux 私有目录，只能推 /sdcard，
而这样升级就不再需要用户手动拷贝或打断正在进行的操作。

### 判活的两个坑（都踩过，写代码时别再犯）

| 写法 | 结果 |
|---|---|
| `ps -A \| grep charge_guard_phone.py` | **假阴性** —— Android 的 ps 只显示进程名 `python` |
| 裸 `pgrep -f charge_guard_phone.py` | **假阳性** —— 调用者自己的命令行里含这个字符串，会自匹配 |
| ✅ `grep -qa '^python' /proc/$p/cmdline` + `grep -qa 'charge_guard_phone.py' ...` | 准确 |

另外守护自身加了**单实例保护**（`~/charge_guard/guard.pid`）：现在有四处会拉起它
（手工、.bashrc 钩子、JobScheduler、PC 侧 am start），两个实例会各自控同一个插座，必须防。

---

## 五、澎湃 OS 保活（不做这步会被杀）

这台是 HyperOS，后台管控很激进。逐项设置：

1. **设置 → 应用设置 → 应用管理 → Termux → 省电策略 → 无限制**
2. 同页 → **自启动 → 允许**（Termux、Termux:API、Termux:Boot 三个都要）
3. 同页 → **权限 → 后台弹出界面 → 允许**（部分版本需要）
4. **设置 → 电池 → 应用智能省电**里把 Termux 移出省电名单
5. 顺手：**开发者选项 → 充电时保持唤醒**。理由不是防锁屏，而是**防 Wi-Fi 省电**：
   手机息屏久了 Wi-Fi 会进省电，而控插座走的是局域网 UDP，同样会受影响。
6. 在「最近任务」里把 Termux 卡片**下拉锁定**（MIUI 最狠的保活手段）。

用 ADB 能代劳的部分（本机已设）：`RUN_ANY_IN_BACKGROUND`/`RUN_IN_BACKGROUND = allow`、
Doze 白名单 `+com.termux`、以及 MIUI 专属 op `10021/10020/10016 = allow`（10021 即 MIUI 自启动）。
**但 MIUI 自家存储里的「省电策略/自启动」ADB 改不了**（`appops ... AUTO_START` 会报
`Unknown operation string`），那两项仍需手点。

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
