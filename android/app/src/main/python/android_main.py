# -*- coding: utf-8 -*-
"""APK 里的 Android 适配层（Chaquopy 入口）。

分工：判定逻辑、miIO 协议、AES、CSV 日志全部复用 charge_guard_phone.py（与 Termux 版
同一个文件，CI 有 diff 检查）；这里只做三件 Android 特有的事：
  1) 电量改从 BatteryManager 读（替代 termux-api），monkeypatch 进 cg.read_battery；
  2) CSV/状态镜像到 /sdcard/charge_guard/（PC 看门狗盯的路径），拿不到就退回 app 私有目录；
  3) 提供前台服务调用的 start_guard / stop_guard 与界面用的 probe_plug / write_test。

线程模型：GuardService 起一个线程跑 start_guard（内部 1s 步进睡眠，stop_guard 后 ≤1s 退出）。
"""
import json
import os
import time

import charge_guard_phone as cg
from java import jclass

STOP = {"flag": False}

# 同步自 phone/charge_guard_phone.json（含 9-26 验证的插座编号与 9-27 的告警阈值）。
# 这里刻意内嵌成字典：APK 里不依赖文件路径，python 源目录的数据文件加载行为就不赌了。
BASE_CFG = {
    "plug": {
        "ip": "192.168.0.99", "token": "",
        "on_siid": 2, "on_piid": 1,
        "power_siid": 11, "power_piid": 2,
        "fault_siid": 2, "fault_piid": 3,
        "timeout_sec": 4.0, "udp_port": 54321,
        "label": "充电器插座",
    },
    "policy": {
        "stop_at": 65, "resume_at": 50, "stable_readings": 2,
        "immediate_off_margin": 3, "immediate_on_margin": 3,
        "temp_cutoff_c": 45.0, "temp_resume_c": 40.0,
        "plug_fail_alarm_after": 3, "plug_fail_alarm_every": 10,
    },
    "loop": {"interval_sec": 60, "wake_lock": True},
    "lost_contact": {"short_break_sec": 600, "stale_sec": 1800, "stale_action_max_hours": 12},
    "log": {"csv": "phone_guard_log.csv", "keep_days": 60},
    "alert": {"type": "console", "webhook_url": "", "repeat_sec": 21600},
}

SDCARD_DIR = "/sdcard/charge_guard"


def _load_cfg(app_dir: str) -> dict:
    cfg = json.loads(json.dumps(BASE_CFG))          # 深拷贝
    user_path = os.path.join(app_dir, "config.json")
    if os.path.exists(user_path):
        with open(user_path, "r", encoding="utf-8") as f:
            # ⚠️ _deep_merge 是纯函数（返回合并结果、不改 base）——必须接住返回值。
            # 2026-10-01 实测：扔掉返回值 ⇒ 永远用空 token 的 BASE_CFG，
            # 插座指令全部「token 必须是 32 个 hex 字符，当前 0 字节」。
            cfg = cg._deep_merge(cfg, json.load(f))
    return cfg


def _pick_csv(app_dir: str) -> str:
    """优先用与 Termux 版相同的共享路径（PC 看门狗盯的就是它），写不进去再退回私有目录。"""
    cand = os.path.join(SDCARD_DIR, "phone_guard_log.csv")
    try:
        os.makedirs(SDCARD_DIR, exist_ok=True)
        with open(cand, "a", encoding="utf-8"):
            pass
        return cand
    except Exception:
        return os.path.join(app_dir, "guard_log.csv")


def _dump_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _write_status(app_dir: str, guard, csv_path: str) -> None:
    r = guard.last_reading or {}
    st = {
        "ts": time.strftime("%F %T"),
        "level": r.get("level"),
        "temp": r.get("temperature_c"),
        "plug_on": guard.plug_on,
        "err_streak": guard.err_streak,
        "pending": (f"{guard.pending_action} x{guard.pending_count}"
                    if guard.pending_action else "-"),
        "csv_path": csv_path,
    }
    try:
        _dump_json(os.path.join(app_dir, "status.json"), st)
    except Exception:
        pass
    try:
        _dump_json(os.path.join(SDCARD_DIR, "apk_status.json"), st)
    except Exception:
        pass


def _make_battery_reader(context):
    """BatteryManager 的粘性广播最省事：不用请求任何权限，温度单位与 dumpsys 一致（0.1℃）。"""
    IntentFilter = jclass("android.content.IntentFilter")

    def read_battery():
        i = context.registerReceiver(None, IntentFilter("android.intent.action.BATTERY_CHANGED"))
        level = i.getIntExtra("level", -1)
        scale = i.getIntExtra("scale", 100)
        if level < 0 or scale <= 0:
            return None, "no battery data"
        temp_tenths = i.getIntExtra("temperature", -1)
        plugged = i.getIntExtra("plugged", 0)
        status = i.getIntExtra("status", -1)
        plugged_str = {1: "PLUGGED_AC", 2: "PLUGGED_USB", 4: "PLUGGED_WIRELESS"}.get(plugged, "UNPLUGGED")
        reading = {
            "level": round(level * 100 / scale),
            "temperature_c": temp_tenths / 10.0 if temp_tenths >= 0 else None,
            "plugged": plugged_str,
            "ac_powered": plugged != 0,
            "raw_ok": True,
        }
        return reading, f"status={status}"

    return read_battery


def start_guard(app_dir: str, context) -> None:
    STOP["flag"] = False
    cfg = _load_cfg(app_dir)
    csv_path = _pick_csv(app_dir)

    cg.read_battery = _make_battery_reader(context)   # 注入 Android 电量源

    log = cg.CsvLog(csv_path, int(cfg["log"]["keep_days"]))
    plug = cg.PlugMiio(cfg["plug"])
    guard = cg.Guard(cfg, plug, log, cg.Alerter(cfg["alert"]))
    try:
        guard.plug_on = plug.read_on()                # 用真实开关状态初始化信念，避免第一轮误判
        log.row("start", note=f"apk guard; csv={csv_path}; plug_on={guard.plug_on}")
    except Exception as e:
        log.row("start", note=f"apk guard; csv={csv_path}; plug read failed: {type(e).__name__}: {e}")

    interval = max(10, int(cfg["loop"]["interval_sec"]))
    while not STOP["flag"]:
        try:
            guard.tick()
        except Exception as e:
            log.row("error", note=f"{type(e).__name__}: {e}")
        _write_status(app_dir, guard, csv_path)
        for _ in range(interval):                     # 1s 步进，stop_guard 后 ≤1s 退出
            if STOP["flag"]:
                break
            time.sleep(1)
    log.row("stop", note="service stopped")


def stop_guard() -> None:
    STOP["flag"] = True


# ------------------------------------------------------------ 界面按钮用的两个测试
def _plug(app_dir: str):
    return cg.PlugMiio(_load_cfg(app_dir)["plug"])


def probe_plug(app_dir: str) -> str:
    """只读：握手 + 开关/功率/故障。"""
    p = _plug(app_dir)
    lines = [f"目标 {p.c.get('ip')}:{int(p.c.get('udp_port', 54321))}"]
    try:
        p._cli().handshake()
        lines.append("握手 OK")
    except Exception as e:
        return f"握手失败：{type(e).__name__}: {e}"
    for label, key in (("开关", "on"), ("功率", "power_w"), ("故障", "fault")):
        try:
            st = p.read_status()
            lines.append(f"{label} = {st.get(key)}")
            break
        except Exception as e:
            lines.append(f"{label} 读取失败：{type(e).__name__}: {e}")
    return "\n".join(lines)


def write_test(app_dir: str) -> str:
    """写入通路测试：通电 → 回读 → 断电 → 回读（每步都有回读确认）。"""
    p = _plug(app_dir)
    lines = []

    def step(name, fn):
        t0 = time.time()
        try:
            lines.append(f"OK  {name}: {fn()!r} ({time.time() - t0:.1f}s)")
        except Exception as e:
            lines.append(f"ERR {name}: {type(e).__name__}: {e} ({time.time() - t0:.1f}s)")

    step("read_status", p.read_status)
    step("set ON", lambda: p.set_port(True))
    time.sleep(2)
    step("read after ON", p.read_status)
    step("set OFF", lambda: p.set_port(False))
    time.sleep(1)
    step("read after OFF", p.read_status)
    return "\n".join(lines)
