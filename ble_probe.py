#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ble_probe.py —— 判定「PC 能不能用 BLE 看到酷态科10号充电器」

为什么要单独做这个脚本：
在 agent 的命令环境里跑扫描，结果是 0 个设备（bleak 15s + WinRT 原生 12s/25s 都是 0，
沙箱内外一致）。但 0 有两种解释，必须分开：
  (甲) 本机进程环境拿不到无线设备（枚举能力被限制）
  (乙) 环境正常，只是充电器当时没在广播 / 不在 BLE 距离内

[1] 适配器能力 与 [2] 已配对设备枚举 都不需要射频扫描：
  如果 [2] 能列出你配对过的耳机/手表，说明环境正常 → 那就是 (乙)；
  如果 [2] 也是 0，说明是 (甲)，得换台机器或由你手工在普通终端里跑。
[3] 才是真正的广播扫描。

用法（建议在充电器旁边跑，且先关掉手机上的米家 App）：
    run_ble_probe.bat            # 双击即可，会在同目录生成 ble_probe_report.txt
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

REPORT = Path(__file__).with_name("ble_probe_report.txt")
# 充电器已知的型号串与关键词（来自开源实现的 README + Luna 在米家里看到的）
KEYWORDS = ("njcuk", "ad1204", "cuktech", "酷态科")
SCAN_SECONDS = 20.0

lines: list[str] = []


def out(s: str = "") -> None:
    print(s, flush=True)
    lines.append(s)
    # 逐行落盘：WinRT 在本机偶发段错误（exit 139），一次性写在崩溃时会全丢
    try:
        with REPORT.open("a", encoding="utf-8") as f:
            f.write(s + "\n")
            f.flush()
    except Exception:
        pass


async def step1_adapter() -> None:
    out("[1] 蓝牙适配器能力（不依赖射频扫描）")
    try:
        from winrt.windows.devices.bluetooth import BluetoothAdapter
        a = await BluetoothAdapter.get_default_async()
    except Exception as e:
        out(f"    查询失败: {type(e).__name__}: {e}")
        return
    if a is None:
        out("    ✗ 没有默认蓝牙适配器")
        return
    out(f"    地址: {a.bluetooth_address:#014x}   设备ID: {a.device_id}")
    for k in ("is_low_energy_supported", "is_central_role_supported",
              "is_classic_supported", "is_peripheral_role_supported"):
        try:
            out(f"    {k} = {getattr(a, k)}")
        except Exception as e:
            out(f"    {k} -> 不可用 ({type(e).__name__})")


async def step2_paired() -> int:
    out("")
    out("[2] 已配对/已缓存的蓝牙设备（DeviceInformation 枚举，不走射频）")
    found = 0
    try:
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.devices.bluetooth import BluetoothDevice
        selectors = []
        try:
            selectors.append(("BluetoothDevice.get_device_selector()",
                              BluetoothDevice.get_device_selector()))
        except Exception as e:
            out(f"    取 selector 失败: {e}")
        try:
            selectors.append(("...from_pairing_state(True)",
                              BluetoothDevice.get_device_selector_from_pairing_state(True)))
        except Exception:
            pass
        for label, sel in selectors:
            res = None
            errs = []
            # PyWinRT 3.x 只暴露了单个重载，且 doc 为 None —— 逐个试参数个数
            for args in ((sel,), (sel, []), (sel, None)):
                try:
                    res = await DeviceInformation.find_all_async(*args)
                    out(f"    {label}: 枚举成功（参数个数={len(args)}）→ {len(res)} 个")
                    break
                except Exception as e:
                    errs.append(f"{len(args)}参: {type(e).__name__}: {e}")
            if res is None:
                out(f"    {label}: ❌ 调用失败（**这与「枚举到 0 个设备」不是一回事**）")
                for e in errs:
                    out(f"        {e}")
                continue
            for d in res:
                found += 1
                out(f"        {d.name!r}  id={d.id}")
    except Exception as e:
        out(f"    步骤异常: {type(e).__name__}: {e}")
    if found == 0:
        out("    ⇒ 本次没拿到设备列表。注意：这不能直接推出「本机拿不到无线设备」——")
        out("      得先看上面是「调用失败」还是「真的 0 个」。调用失败只是本脚本的 API")
        out("      签名没试对，对判定没有价值。")
    else:
        out(f"    ⇒ 列出了 {found} 条，说明设备枚举走通了（倾向：只是没扫到广播）")
    return found


async def step3_scan() -> int:
    out("")
    out(f"[3] BLE 广播扫描 {SCAN_SECONDS:.0f}s（真的在用射频）")
    seen: dict[int, tuple] = {}
    try:
        from winrt.windows.devices.bluetooth.advertisement import (
            BluetoothLEAdvertisementWatcher, BluetoothLEScanningMode)
        w = BluetoothLEAdvertisementWatcher()
        try:
            w.scanning_mode = BluetoothLEScanningMode.ACTIVE
        except Exception:
            pass

        def on_recv(sender, args):
            try:
                adv = args.advertisement
                nm = adv.local_name
                if not nm and adv.data_sections:
                    for s in adv.data_sections:
                        if s.data_type == 0x09:
                            nm = bytes(s.data).decode("utf-8", "replace")
                            break
                seen[args.bluetooth_address] = (nm, args.rssi)
            except Exception:
                pass

        aborted = []

        def on_aborted(sender, args):
            try:
                aborted.append(getattr(args, "error", None))
            except Exception:
                aborted.append("unknown")

        w.add_received(on_recv)
        try:
            w.add_aborted(on_aborted)
        except Exception:
            pass
        w.start()
        # start() 后的状态是关键判据：Started = 射频请求被接受；Aborted = 射频被拒
        await asyncio.sleep(1.0)
        try:
            st = w.status
            st_name = getattr(st, "name", str(st))
        except Exception as e:
            st_name = f"读不到({e})"
        out(f"    监听器状态：{st_name}（Started = 射频请求已被接受）")
        await asyncio.sleep(SCAN_SECONDS)
        w.stop()
        if aborted:
            out(f"    ⚠️ 触发 aborted 事件：{aborted} ⇒ 射频被拒，是环境/驱动问题")
        else:
            out("    无 aborted 事件 ⇒ 射频通路正常")
    except Exception as e:
        out(f"    WinRT 监听失败: {type(e).__name__}: {e}")
        try:
            from bleak import BleakScanner
            devs = await BleakScanner.discover(timeout=SCAN_SECONDS, return_adv=True)
            for a, (d, adv) in devs.items():
                seen[int(a.replace(':', ''), 16)] = (d.name, adv.rssi)
        except Exception as e2:
            out(f"    bleak 也失败: {type(e2).__name__}: {e2}")

    out(f"    收到 {len(seen)} 个广播")
    hits = []
    for addr, (nm, rssi) in sorted(seen.items()):
        name = str(nm)
        flag = "   <== 疑似充电器" if any(k in name.lower() for k in KEYWORDS) else ""
        if flag:
            hits.append((addr, nm, rssi))
        out(f"        {addr:#014x}  rssi={rssi}  name={nm!r}{flag}")
    out("")
    if hits:
        out("⇒ 结果：找到疑似充电器。记下上面的地址，下一步才能拿 token 连它。")
    else:
        out("⇒ 结果：没找到充电器。但先看上面的监听器状态：")
        out("   · 状态=Started 且无 aborted ⇒ 射频通路正常，那 0 个就是「附近没有设备在广播」，")
        out("     按顺序试：① 关掉手机米家 App（充电器一次只接受一个 BLE 连接）")
        out("     ② 点亮充电器屏幕/插个设备保持唤醒 ③ 把充电器挪到 PC 旁边 1 米内 ④ 重跑本脚本")
        out("   · 状态=Aborted 或出现 aborted 事件 ⇒ 是环境/驱动把射频拦了，那就不该指望 PC 当 BLE 主机，")
        out("     考虑 ESP32 桥（仓库里有固件），或换一台机器跑")
    return len(seen)


async def main() -> int:
    try:
        REPORT.unlink()
    except FileNotFoundError:
        pass
    out("BLE 探测报告  " + datetime.now().isoformat(timespec="seconds"))
    out("=" * 62)
    await step1_adapter()
    paired = await step2_paired()
    n = await step3_scan()
    out("")
    out(f"小结：已缓存设备 {paired} 条；扫描到广播 {n} 个。")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    print(f"\n报告已写入 {REPORT}")
    sys.exit(rc)
