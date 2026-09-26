"""PC 侧兜底守护：手机端守护被 MIUI 强制停止时，从 PC 把它救回来。

为什么需要（2026-09-25/26 实测）：
  · 手机端守护凌晨 00:58 被 MIUI 杀死；Android 规则是「应用被强制停止 ⇒ 它的
    JobScheduler 任务全部取消、也收不到开机广播」，所以手机侧的看门狗一起失效
    （实测 `termux-job-scheduler --pending` 返回 "No jobs found"）。
  · 唯一能解除「强制停止」的动作是**有人把它启动起来**：`adb shell am start` 就可以。
  · 手机端已装 ~/.bashrc 钩子：Termux 一旦被启动，钩子会自动把守护拉起。

本脚本因此做两件事：
  1) 每 interval 秒检查一次手机端守护；不在就 `am start` 启动 Termux（钩子接管拉起）；
  2) 兜底保险：如果守护死活起不来、但我们又能读到电量，
     电量 <= resume_at 就把插座通电（避免手机耗尽关机），
     电量 >= stop_at 就把插座断电（避免一直顶在满电）。
     注意这是「兜底」，正常情况应该由手机端守护管理。

用法：
    python phone/watch_from_pc.py                 # 前台跑（Ctrl-C 停）
    python phone/watch_from_pc.py --interval 120  # 更勤快一点
日志：phone/watch_from_pc.log
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOG = HERE / "watch_from_pc.log"
ADB = r"D:\MuMuPlayer-12.0\nx_main\adb.exe"
PHONE_IP = "192.168.0.120"
PHONE_HOME = "/sdcard/charge_guard"

# 看门狗用**自己的 adb server 端口**，不跟 5037 混用。原因（2026-09-27 凌晨实测）：
# 5037 上有别的程序共享（MuMu 模拟器会 kill/start-server 折腾它），还出现过别的
# 上下文起的、本进程杀不掉的残留 server —— 那时 adb connect 会挂死到超时。
# 独立端口后，第一个 adb 调用会自己在这个端口拉起 server，生命周期归我们管。
ADB_SERVER_PORT = "5038"
ADB_ENV = {**os.environ, "ANDROID_ADB_SERVER_PORT": ADB_SERVER_PORT}


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def adb(*args, timeout: float = 20.0) -> tuple[int, str]:
    try:
        p = subprocess.run([ADB, *args], capture_output=True, timeout=timeout, env=ADB_ENV)
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 124, "adb 超时"
    except FileNotFoundError:
        return 127, f"找不到 adb：{ADB}"


def open_ports(lo: int = 30000, hi: int = 51000) -> list[int]:
    def probe(p: int) -> int | None:
        s = socket.socket()
        s.settimeout(0.2)
        try:
            s.connect((PHONE_IP, p))
            return p
        except Exception:
            return None
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=128) as ex:
        return [r for r in ex.map(probe, range(lo, hi), chunksize=128) if r]


def mdns_ports() -> list[int]:
    rc, out = adb("mdns", "services", timeout=40)
    ports = []
    for line in out.splitlines():
        m = re.search(rf"{re.escape(PHONE_IP)}:(\d{{2,5}})", line)
        if m and int(m.group(1)) not in ports:
            ports.append(int(m.group(1)))
    return ports


def _online_serial() -> str | None:
    """只认 `adb devices` 里状态为 device（而不是 offline/unauthorized）的本机条目。"""
    for line in adb("devices")[1].splitlines():
        if PHONE_IP in line and line.rstrip().endswith("device"):
            return line.split("\t")[0]
    return None


def ensure_connected() -> str | None:
    """返回可用的 <ip:port> 串号，或 None。

    铁律（2026-09-26 实测）：**连上必须再验一次在线状态**。
    `adb connect` 对 offline 残留条目也会回 "already connected"，但这种串号
    后续所有 shell 都会失败 —— 必须以 `adb devices` 里的 device 状态为准。
    （2026-09-27 补充实测：手机端 adbd 卡死时端口仍接受 TCP，connect 后状态是
    offline —— 同样不能算连上。）"""
    if (serial := _online_serial()):
        return serial
    for p in mdns_ports():
        adb("connect", f"{PHONE_IP}:{p}", timeout=25)
        if (serial := _online_serial()):
            return serial
    for p in open_ports():
        adb("connect", f"{PHONE_IP}:{p}", timeout=25)
        if (serial := _online_serial()):
            return serial
    return None


AGE_CHECK = (
    "now=$(date +%s); last=$(stat -c %Y /sdcard/charge_guard/phone_guard_log.csv 2>/dev/null || echo 0); "
    "echo $((now - last))"
)

GUARD_CHECK = (
    "for p in $(pgrep -f charge_guard_phone.py 2>/dev/null); do "
    "grep -qa '^python' /proc/$p/cmdline 2>/dev/null && "
    "grep -qa 'charge_guard_phone.py' /proc/$p/cmdline 2>/dev/null && echo $p; done"
)


def guard_age_sec(serial: str) -> int | None:
    """守护写 CSV 的"年龄"（秒）。这是主判据：跨 UID 读 /proc 会失败、裸 pgrep 会自匹配，
    只有"它有没有在按周期写采样"是可靠的（2026-09-26 实测教训）。"""
    rc, out = adb("-s", serial, "shell", AGE_CHECK)
    out = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return int(out) if out.isdigit() else None


def guard_pids(serial: str) -> list[str]:
    """严格判活。

    踩过的两个坑（2026-09-26）：
      · `ps -A | grep charge_guard_phone.py` 假阴性 —— Android 的 ps 只显示进程名（python），
        命令行匹配不上；
      · 裸 `pgrep -f charge_guard_phone.py` 假阳性 —— 调用者自己的命令行里含这个字符串，
        会把自己也算上。
    所以逐个核对 /proc/<pid>/cmdline。
    """
    rc, out = adb("-s", serial, "shell", GUARD_CHECK)
    return [l.strip() for l in out.splitlines() if l.strip().isdigit()]


CSV_TAIL = "tail -6 /sdcard/charge_guard/phone_guard_log.csv"


def recent_rows(serial: str) -> list[dict]:
    """读 CSV 最近几行。这是判断「守护活着但控不动插座」的关键证据：
    控制失败时守护会写 `,error,...,插座请求失败`，连续出现就说明它发不出指令。"""
    rc, out = adb("-s", serial, "shell", CSV_TAIL)
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 5 or parts[0] == "ts":
            continue
        rows.append({"ts": parts[0], "event": parts[1], "level": parts[2],
                     "action": parts[5] if len(parts) > 5 else "", "note": parts[-1]})
    return rows


def battery_level(serial: str) -> int | None:
    rc, out = adb("-s", serial, "shell", "dumpsys battery | grep level")
    m = re.search(r"level:\s*(\d+)", out)
    return int(m.group(1)) if m else None


# ---- 插座兜底（只在手机端守护起不来时用）
def plug_client():
    sys.path.insert(0, str(HERE))
    import charge_guard_phone as cg  # noqa: E402
    cfg = cg.load_config(str(HERE / "charge_guard_phone.json"))
    return cg.PlugMiio(cfg["plug"])


def main() -> int:
    ap = argparse.ArgumentParser(description="PC 侧兜底：把手机端守护救回来（必要时兜底控插座）")
    ap.add_argument("--interval", type=int, default=300, help="检查间隔秒（默认 300）")
    ap.add_argument("--ticks", type=int, default=0, help="跑 N 轮就退出（0=常驻）")
    args = ap.parse_args()

    sys.path.insert(0, str(HERE))
    import charge_guard_phone as cg  # noqa: E402
    cfg = cg.load_config(str(HERE / "charge_guard_phone.json"))
    resume_at = int(cfg["policy"]["resume_at"])
    stop_at = int(cfg["policy"]["stop_at"])
    plug = cg.PlugMiio(cfg["plug"])
    log(f"启动：每 {args.interval}s 检查一次；resume_at={resume_at} stop_at={stop_at}")

    n = 0
    while True:
        n += 1
        log(f"--- 第 {n} 轮开始 ---")          # 心跳：卡住时能立刻定位
        # ── 插座遥测：每轮先记一行（走 miIO，完全不依赖 ADB）──
        # 2026-09-27 凌晨的教训：ADB 整段不可用时，插座是唯一可信的外部证据 ——
        # on/power 能看出「手机在不在充电、守护有没有动作」，没有它整夜就只能干瞪眼。
        try:
            st = plug.read_status()
            log(f"    插座遥测: on={st.get('on')} power={st.get('power_w')}W fault={st.get('fault')}"
                if st else "    插座遥测: 读取失败（记空行，不据此做任何动作）")
        except Exception as e:
            log(f"    插座遥测: 异常 {type(e).__name__}: {e}")
        serial = ensure_connected()
        log(f"    (连接阶段结束: {serial or '未连上'})")
        if not serial:
            log("ADB 不可用（端口变了/无线调试被关/需授权）—— 本轮无法判断，跳过；"
                "注意手机端守护是自治的，ADB 断不影响它")
        else:
            age = guard_age_sec(serial)
            pids = guard_pids(serial)
            if age is None:
                log("读不到 CSV 时间戳（本轮无法判断），跳过 —— 不做任何假设")
            elif (age is not None and age < 180) or pids:
                lvl = battery_level(serial)
                log(f"守护在跑（CSV {age}s 前写过；PID={','.join(pids) or '?'}）  电量={lvl}%")
            else:
                lvl = battery_level(serial)
                log(f"⚠️ 守护不在（CSV {age}s 未更新，电量={lvl}%）—— am start 启动 Termux（钩子会拉起它）")
                adb("-s", serial, "shell", "am start -n com.termux/.app.TermuxActivity")
                time.sleep(8)
                pids = guard_pids(serial)
                if pids:
                    log(f"✅ 已救回：PID={','.join(pids)}")
                else:
                    log("❌ am start 后守护仍不在；走兜底：按电量直接控插座")
                    if lvl is None:
                        log("   读不到电量，兜底不动插座（未知不动作）")
                    else:
                        st = plug.read_status()
                        on = st.get("on")
                        if lvl <= resume_at and on is False:
                            log(f"   电量 {lvl}% ≤ {resume_at}% 且插座是断的 → 通电（防手机耗尽）")
                            plug.set_port(True)
                        elif lvl >= stop_at and on is True:
                            log(f"   电量 {lvl}% ≥ {stop_at}% 且插座是通的 → 断电（防顶在满电）")
                            plug.set_port(False)
                        else:
                            log(f"   兜底无需动作（插座 on={on}，电量 {lvl}%）")
        # ── 新增：控制通道健康度检查 ──
        # 场景（2026-09-26 实况）：守护活着、判定正确，但每条插座指令都超时
        # （手机那侧 UDP 发不出去），于是电量一路掉到 28% 没人管。
        # 判据：CSV 里连续出现 error 行 ⇒ 手机端控不动 ⇒ PC 按 CSV 里的电量接管。
        if serial:
            rows = recent_rows(serial)
            errs = [r for r in rows if r["event"] == "error"]
            if len(errs) >= 3 and rows and rows[-1]["event"] == "error":
                lvl_s = rows[-1]["level"]
                lvl = int(lvl_s) if lvl_s.isdigit() else None
                st = plug.read_status()
                on = st.get("on")
                log(f"⚠️ 手机端连续 {len(errs)} 次控制失败（最近电量 {lvl}%）⇒ PC 接管")
                if lvl is None:
                    log("   读不到电量，接管动作跳过（未知不动作）")
                elif lvl <= resume_at and on is False:
                    log(f"   电量 {lvl}% ≤ {resume_at}% 且插座断开 → PC 通电")
                    plug.set_port(True)
                elif lvl >= stop_at and on is True:
                    log(f"   电量 {lvl}% ≥ {stop_at}% 且插座连通 → PC 断电")
                    plug.set_port(False)
                else:
                    log(f"   无需动作（插座 on={on}，电量 {lvl}%）")
            elif rows:
                log(f"控制通道正常（最近一条 {rows[-1]['event']}，电量 {rows[-1]['level']}%）")

        if args.ticks and n >= args.ticks:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
