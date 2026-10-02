# -*- coding: utf-8 -*-
"""APK 里的 Android 适配层（Chaquopy 入口）。

分工：判定逻辑、miIO 协议、AES、CSV 日志全部复用 charge_guard_phone.py（与 Termux 版
同一个文件，CI 有 diff 检查）；这里只做三件 Android 特有的事：
  1) 电量改从 BatteryManager 读（替代 termux-api），monkeypatch 进 cg.read_battery；
  2) CSV/状态镜像到 /sdcard/charge_guard/（PC 看门狗盯的路径），拿不到就退回 app 私有目录；
  3) 提供前台服务调用的 start_guard / stop_guard 与界面用的 probe_plug / write_test。

线程模型：GuardService 起一个线程跑 start_guard（内部 1s 步进睡眠，stop_guard 后 ≤1s 退出）。
"""
import faulthandler
import json
import os
import signal
import sys
import threading
import time

import charge_guard_phone as cg
from java import jclass

STOP = {"flag": False}
HEARTBEAT = {"t": 0.0}          # 每轮循环刷新；卡死看门狗盯着它
MIRROR = {"gen": 0}             # 镜像线程代际：start_guard 重入时旧线程自动退出

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


def _stall_file(app_dir: str) -> str:
    return os.path.join(app_dir, "stall_count")


def _watchdog(app_dir: str, max_stall: float) -> None:
    """卡死看门狗（2026-10-01 13:24 实测）：循环无异常、无 stop 行、进程未冻结
    （cgroup frozen=0）却长时间无输出 —— 阻塞在某个不超时的调用里。监督线程救不了
    这种死法（它就卡在 callAttr 里）。

    应对（v2.2 定稿）：
      1. faulthandler 把全部线程 Python 栈同时写进 files/stall_dump.txt（追加、带时间戳）
         和 stderr —— logcat 会轮转，落盘才是真证据（14:30 那次就是被轮转吃掉的）；
      2. os.kill(self, SIGKILL) 立即死亡。不用 System.exit()：14:30 实测它在 JVM 关闭
         钩子上挂死（进程拖了 4.7h 才被外力了结）；不用 os.abort()：SIGABRT 会弹
         「应用已停止运行」闪退窗（同日下午吓到机主）。SIGKILL 立死、无弹窗；
         进程死后服务能否被系统拉回取决于 MIUI 自启动设置与 GuardService 的复活闹钟链。"""
    while not STOP["flag"]:
        time.sleep(15)
        t = HEARTBEAT["t"]
        if t and time.monotonic() - t > max_stall:
            try:
                with open(_stall_file(app_dir), "r", encoding="utf-8") as f:
                    prev = int(f.read().strip() or 0)
            except Exception:
                prev = 0
            try:
                with open(_stall_file(app_dir), "w", encoding="utf-8") as f:
                    f.write(str(prev + 1))
            except Exception:
                pass
            try:
                with open(os.path.join(app_dir, "stall_dump.txt"), "a", encoding="utf-8") as f:
                    f.write(f"\n===== stall at {time.strftime('%F %T')} "
                            f"(heartbeat {max_stall:.0f}s+) =====\n")
                    faulthandler.dump_traceback(file=f)
            except Exception:
                pass
            try:
                faulthandler.dump_traceback(file=sys.stderr)
            except Exception:
                pass
            os.kill(os.getpid(), signal.SIGKILL)


def _mirror_loop(app_dir: str, gen: int) -> None:
    """把私有 status.json 尽力而为地镜像到 /sdcard/apk_status.json（PC/adb 观测用）。

    /sdcard 的 FUSE 写有随机卡死前科（2026-10-01 三次取证，见 _watchdog），所以
    守护循环线程上一个 /sdcard I/O 都不碰；这个线程自己卡死也只是镜像停止更新，
    不影响守护。gen 不一致说明有新线程接班，本线程退出。"""
    mirror = os.path.join(SDCARD_DIR, "apk_status.json")
    while MIRROR["gen"] == gen and not STOP["flag"]:
        try:
            with open(os.path.join(app_dir, "status.json"), "r", encoding="utf-8") as f:
                data = f.read()
            tmp = mirror + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, mirror)
        except Exception:
            pass
        for _ in range(60):
            if MIRROR["gen"] != gen or STOP["flag"]:
                return
            time.sleep(1)


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
    """[已退役] 2026-10-01 之前的主路径选择（/sdcard 优先、私有目录兜底）。
    /sdcard FUSE 写三次随机卡死后，循环线程不再碰 /sdcard，CSV 固定私有目录。
    保留此注释防有人找这段历史；函数体已删。"""
    return os.path.join(app_dir, "guard_log.csv")


def _dump_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


class SafeLog:
    """CsvLog 的保险层。

    2026-10-01 停摆教训：CsvLog.row 里任何一处写失败（/sdcard 瞬时 I/O 错误、_prune 的
    write_text 等）都会抛异常，循环的 except 分支里再写一行同样抛 ⇒ 异常逃出 while、
    线程死亡，而前台服务还挂着 —— 守护死了但看起来一切正常，状态停在 12:50:26。
    这一层保证：写日志的失败永不外抛。v2.1 起主路径就是私有目录（ext4），primary 与
    fallback 同路径，连续失败时重建文件句柄再试，行为退化为「纯吞异常 + 自愈句柄」。
    """

    def __init__(self, primary, fallback_path: str, keep_days: int, failover_after: int = 3):
        self._log = primary
        self._primary_path = str(primary.path)
        self._fallback_path = fallback_path
        self._keep_days = keep_days
        self._failover_after = failover_after
        self._fails = 0

    @property
    def path(self) -> str:
        return str(self._log.path)

    def row(self, event: str, reading: dict | None = None,
            action: str = "", reason: str = "", note: str = "") -> None:
        try:
            self._log.row(event, reading=reading, action=action, reason=reason, note=note)
            self._fails = 0
            return
        except Exception:
            self._fails += 1
        if (self._fails >= self._failover_after
                and str(self._log.path) == self._primary_path):
            # 降级到私有目录另起一份；不自动切回主路径 —— 状态里看得见在用哪个，人来了再处理。
            try:
                self._log = cg.CsvLog(self._fallback_path, self._keep_days)
            except Exception:
                pass


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
        # 只写私有目录（ext4）。/sdcard 镜像由 _mirror_loop 独立线程负责——
        # FUSE 写随机卡死（2026-10-01 三次取证）绝不能发生在守护循环线程上。
        _dump_json(os.path.join(app_dir, "status.json"), st)
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

    # CSV 一律落私有目录（ext4，无 FUSE 卡死风险）。/sdcard 的共享 CSV（Termux/PC 看门狗
    # 时代的主路径）已退役：观测走 status.json + _mirror_loop 镜像，历史 CSV 留在 /sdcard 原地。
    csv_path = os.path.join(app_dir, "guard_log.csv")
    cg.read_battery = _make_battery_reader(context)   # 注入 Android 电量源

    keep_days = int(cfg["log"]["keep_days"])
    # primary=fallback 同路径：SafeLog 此时只负责「写失败不外抛」，连续失败会重建文件句柄。
    log = SafeLog(cg.CsvLog(csv_path, keep_days), csv_path, keep_days)
    plug = cg.PlugMiio(cfg["plug"])
    guard = cg.Guard(cfg, plug, log, cg.Alerter(cfg["alert"]))
    try:
        guard.plug_on = plug.read_on()                # 用真实开关状态初始化信念，避免第一轮误判
        log.row("start", note=f"apk guard; csv={csv_path}; plug_on={guard.plug_on}")
    except Exception as e:
        log.row("start", note=f"apk guard; csv={csv_path}; plug read failed: {type(e).__name__}: {e}")

    interval = max(10, int(cfg["loop"]["interval_sec"]))
    HEARTBEAT["t"] = time.monotonic()
    # v2.5：不插电时 Java 侧放掉 wakelock、CPU 深睡，采样靠 5 分钟唤醒闹钟驱动，
    # 心跳合法间隔拉长到 ~300s+；看门狗阈值必须盖过它（原 150s 会每次唤醒都误杀）。
    threading.Thread(target=_watchdog, args=(app_dir, max(600, 8 * interval)),
                     name="guard-stall-watchdog", daemon=True).start()
    MIRROR["gen"] += 1
    threading.Thread(target=_mirror_loop, args=(app_dir, MIRROR["gen"]),
                     name="guard-mirror", daemon=True).start()
    while not STOP["flag"]:
        t0 = time.monotonic()
        # 双层兜底（2026-10-01 实测：任何一层漏网的异常都会无声杀死循环——前台服务还活着，
        # status 却永远停在最后一秒）。tick 内部 catch 控制失败；这里再兜「写日志/写状态本身」。
        try:
            guard.tick()
        except Exception as e:
            try:
                log.row("error", note=f"tick: {type(e).__name__}: {e}")
            except Exception:
                pass                                  # SafeLog 之上的最后一道保险
        try:
            _write_status(app_dir, guard, log.path)
        except Exception:
            pass
        HEARTBEAT["t"] = time.monotonic()             # 喂卡死看门狗
        # 异常秒回时也补齐到整间隔再进下一轮，防止异常风暴刷爆日志；1s 步进保证 stop_guard ≤1s 退出
        remain = max(10, interval - int(time.monotonic() - t0))
        for _ in range(remain):
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
