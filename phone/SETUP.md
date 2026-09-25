# 手机端部署手册（K70 Pro + Termux + ESP32 桥）

目标形态：**手机自己当大脑** —— 读自己的电量、自己判定、经 ESP32 转 BLE 去开关充电器的 C1 口。
PC 完全不参与，也不需要无线 ADB。

```
手机 ──(本机电量)──► 判定(65/50 + 温度 + 稳定门) ──HTTP──► ESP32 ──BLE──► 酷态科10号 C1 口
```

---

## 进度（2026-09-25）

| 步骤 | 状态 |
|---|---|
| 手机上装 Termux / Termux:API / Termux:Boot | ✅ 已用 ADB 装好（0.118.3 / 0.53.0 / —） |
| 手机端脚本 + 配置 | ✅ 已写好并推送到 `/sdcard/charge_guard/`（自测 13/13 通过） |
| ESP32 硬件 | ⬜ **待买**（ESP32 / ESP32-S3 / ESP32-C3 任一款，¥20–40） |
| 烧固件 + 配网 + 取凭据 | ⬜ 待做（见第一节） |
| Termux 内初始化 + 跑起来 | ⬜ 待做（见第二、三节） |

---

## 一、ESP32 侧（需要先买一块）

1. **买**：ESP32 / ESP32-S3 / ESP32-C3 任一款即可，**无需外接蓝牙适配器**。
   建议带 USB 转串口的那类开发板（烧录方便）。
2. **烧固件**：`kairui1108/cuktech-ble-esp32` 的 Releases 里有预编译固件；
   或自行编译（需要 ESP-IDF v5.3）：`idf.py set-target esp32 && idf.py build && idf.py -p <串口> flash`
3. **配网**：烧好后 ESP32 会开一个 AP ——
   - 连 WiFi：`CUKTECH-Setup`，密码 `12345678`
   - 浏览器打开 `http://192.168.4.1/`
   - 填：家里 WiFi、MQTT（可留空）、**设备凭据**
4. **设备凭据**（三个都要，从米家云端取）：
   - **MAC 地址**（6 字节 hex）
   - **Token**（12 字节 hex）
   - **BLE Key**（16 字节 hex）
   - 取法：`Xiaomi-cloud-tokens-extractor`（https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor ）
     —— 登录小米账号后它会列出账号下所有设备，找 `njcuk.fitting.ad1204` 那一条。
     ⚠️ 这三个都是密钥，别提交进 git。
5. **在路由器上给 ESP32 绑静态 IP**，然后把 IP 填进 `charge_guard_phone.json` 的 `esp32.base_url`。

> 固件自带**零依赖 Web 面板**（不需要 HA、不需要 MQTT）：浏览器访问 ESP32 的 IP
> 就能看各口电压/电流/功率、开关端口、切协议、切场景模式。
> 先用这个面板人工确认「**能开关 C1 口**」，再让脚本接管。

---

## 二、Termux 里的一次性初始化

打开 Termux，依次执行：

```bash
# 1) 给 Termux 存储权限（出现系统弹窗要点允许）
termux-setup-storage

# 2) 更新并装依赖（python + termux-api 命令）
pkg update -y && pkg upgrade -y
pkg install -y python termux-api

# 3) 把推送过来的脚本拷进家目录
mkdir -p ~/charge_guard
cp /sdcard/charge_guard/* ~/charge_guard/
cd ~/charge_guard
ls -l

# 4) 验证 termux-api 通了（应输出一段 JSON，含 percentage / temperature / plugged）
termux-battery-status
```

⚠️ 如果第 4 步报 `command not found` 或没有输出：
`termux-api` 这个包只提供命令行工具，**真正读数据的是 Termux:API 应用**——确认它已安装且给过权限。

---

## 三、跑起来

```bash
cd ~/charge_guard

# 1) 自测（不需要 ESP32，验证判定逻辑）
python charge_guard_phone.py --self-test

# 2) 看一次原始电量（确认字段名与单位）
python charge_guard_phone.py --show-battery

# 3) 探测 ESP32 的 REST 接口（只读，不动端口）
#    先把 charge_guard_phone.json 里的 esp32.base_url 改成 ESP32 的实际 IP
python charge_guard_phone.py --probe

# 4) 只读数不控口，跑 3 轮
python charge_guard_phone.py --dry-run --ticks 3

# 5) 正式运行
python charge_guard_phone.py
```

**字段确认要点**：`termux-battery-status` 的 `temperature` 单位是 **摄氏度**（如 `33.7`），
而 PC 侧 `dumpsys battery` 是 **0.1℃**（`337`）。脚本按摄氏度处理，**不要再除以 10**。
自测里专门有一条断言钉住这件事（误除 10 会让 45℃ 阈值形同虚设）。

---

## 四、常驻与开机自启

```bash
# 不让 Android Doze 把循环冻住（脚本里 loop.wake_lock=true 时也会自己调）
termux-wake-lock

# 后台跑
cd ~/charge_guard && nohup python charge_guard_phone.py >> guard.out 2>&1 &

# 看日志
tail -f ~/charge_guard/guard.out
tail -20 ~/charge_guard/phone_guard_log.csv
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

然后**手动打开一次 Termux:Boot 应用**（它的权限需要被系统记录一次才会生效）。

---

## 五、澎湃 OS 保活（不做这步会被杀）

这台是 HyperOS，后台管控很激进。逐项设置：

1. **设置 → 应用设置 → 应用管理 → Termux → 省电策略 → 无限制**
2. 同页 → **自启动 → 允许**（Termux、Termux:API、Termux:Boot 三个都要）
3. **设置 → 应用设置 → 应用管理 → Termux → 权限 → 后台弹出界面 → 允许**（部分版本需要）
4. **设置 → 电池 → 应用智能省电**里把 Termux 移出省电名单
5. 顺手：**开发者选项 → 充电时保持唤醒**（这条与之前的结论一致，理由是防 Wi-Fi 省电，不是防锁屏）

---

## 六、排错

| 现象 | 原因与处置 |
|---|---|
| `termux-battery-status: command not found` | 没 `pkg install termux-api`，或 Termux:API 应用没装 |
| `termux-battery-status` 无输出 | Termux:API 应用被冻结/没授权；到应用管理里允许自启动 |
| `percentage 不可信: None` | 脚本拒绝把未知当 0 或 100（故意的）。检查 Termux:API 权限 |
| ESP32 请求全失败 | `base_url` 不对，或用 `--probe` 试出的路径与固件实际不符 |
| 端口切了但充电没停 | 先确认 C1 口；再用固件 Web 面板人工切一次对照 |
| 跑几小时后停了 | 保活没做（第五节）；看 `guard.out` 有没有被 kill |

---

## 七、还没验的两件事（别当成品用）

1. **「关掉 C1 后充电器还广播吗」** —— 如果关掉就没别的负载了，充电器可能息屏停止广播。
   对 ESP32 桥来说这条**影响小得多**（ESP32 一直握着连接，不像手机是临时连），
   但仍需实测：关掉 C1 → 等 5 分钟 → 看 ESP32 面板还能不能切回来。
   ⚠️ 如果切不回来，就意味着「关得掉、开不回来」，必须在脚本里加保护。
2. **ESP32 固件的 REST 接口确切路径** —— README 没写，`--probe` 会试几个候选；
   最终以 `http_server.c` 源码或面板自身请求为准。
