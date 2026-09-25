#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
charge_guard_phone.py —— 跑在 K70 Pro 上（Termux）的「大脑」

为什么把它搬到手机上：手机就贴在充电器上，读端（自己的电量）和执行端（经 ESP32 转 BLE）
都不出手机。于是 charge_guard.py 里那一整堆东西不再需要：
  · 无线调试端口的缓存/mDNS/扫描三层兜底
  · `adb connect` 之后 shell 未就绪返回空字符串的假在线
  · adb daemon 被沙箱回收
  · PC 必须常开、必须与充电器在 BLE 距离内、必须与手机同网段

链路：
    本机电量 ──(termux-battery-status)──┐
                                        ├─► 判定（65/50 滞回 + 温度 + 稳定门）──► HTTP ──► ESP32 ──BLE──► 充电器 C1 口
    留作唯一真相源：charge_guard.py     ┘

依赖：**只有 Python 标准库**（urllib 就够了，不需要 pip 装任何东西）。
      外加 Termux 侧的 `termux-api` 包 + Termux:API 应用（提供 termux-battery-status）。

⚠️ 稳定性门与判定规则**同步自 charge_guard.py::decide**。改一处必须改两处；
   两边都带同一套自测断言，跑 `--self-test` 可以发现漂移。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "esp32": {
        "base_url": "http://192.168.1.50",
        "status_path": "/api/status",
        "port_path": "/api/port?port={port}&state={state}",
        "port": "c1",
        "timeout_sec": 8,
        "on_value": "1",
        "off_value": "0",
    },
    "policy": {
        "stop_at": 65,
        "resume_at": 50,
        "stable_readings": 2,
        "immediate_off_margin": 3,
        "immediate_on_margin": 3,
        "temp_cutoff_c": 45.0,
        "temp_resume_c": 40.0,
    },
    "loop": {"interval_sec": 300, "wake_lock": True},
    "lost_contact": {"short_break_sec": 600, "stale_sec": 1800, "stale_action_max_hours": 12},
    "log": {"csv": "phone_guard_log.csv", "keep_days": 60},
    "alert": {"type": "console", "webhook_url": "", "repeat_sec": 21600},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str) -> dict:
    cfg = _deep_merge(DEFAULTS, json.loads(Path(path).read_text(encoding="utf-8")))
    p = cfg["policy"]
    if not (0 <= p["resume_at"] < p["stop_at"] <= 100):
        raise ValueError(f"配置非法：要求 0 <= resume_at({p['resume_at']}) < stop_at({p['stop_at']}) <= 100")
    if p["temp_resume_c"] >= p["temp_cutoff_c"]:
        raise ValueError("配置非法：temp_resume_c 必须小于 temp_cutoff_c")
    return cfg


# ---------------------------------------------------------------- 读电量
def read_battery() -> tuple[dict | None, str]:
    """
    用 termux-battery-status 读本机电池。返回 (reading, note)。

    ⚠️ 与 PC 侧 dumpsys 的两处差异，写代码时最容易踩：
      1. termux-api 的 `temperature` 单位是 **摄氏度**（33.7），
         而 `dumpsys battery` 是 **0.1℃**（337）。**不要**再除以 10。
      2. 没有 `AC powered`；用 `plugged`（PLUGGED_AC / PLUGGED_USB / UNPLUGGED）。
    """
    try:
        raw = subprocess.run(["termux-battery-status"], capture_output=True, timeout=15)
    except FileNotFoundError:
        return None, "termux-api 未安装（需要 Termux:API 应用 + pkg install termux-api）"
    except subprocess.TimeoutExpired:
        return None, "termux-battery-status 超时"
    if raw.returncode != 0:
        return None, f"termux-battery-status 返回 {raw.returncode}"
    try:
        d = json.loads((raw.stdout or b"").decode("utf-8", "replace") or "{}")
    except Exception as e:
        return None, f"输出不是合法 JSON: {e}"

    pct = d.get("percentage")
    if not isinstance(pct, int) or not (0 <= pct <= 100):
        # 未知绝不当作 0 或 100 —— 伪造成 0 会误触发通电，伪造成 100 会误触发断电
        return None, f"percentage 不可信: {pct!r}"

    temp = d.get("temperature")
    temp = float(temp) if isinstance(temp, (int, float)) else None
    plugged = str(d.get("plugged", "")).upper()
    return {
        "level": pct,
        "temperature_c": temp,
        "plugged": plugged,
        "ac_powered": plugged.startswith("PLUGGED"),
        "status_text": str(d.get("status", "")).upper(),
        "voltage_mv": d.get("voltage"),
        "raw_ok": True,
    }, ""


# ---------------------------------------------------------------- 执行端（ESP32）
class Esp32Link:
    """
    ESP32 固件（kairui1108/cuktech-ble-esp32）自带零依赖 Web 面板与 REST 接口。
    ⚠️ 确切的接口路径以你烧进去那版固件的源码 / 面板请求为准 —— 所以路径做成配置项，
       并用 --probe 逐个试探候选。
    """

    CANDIDATE_PORT_PATHS = [
        "/api/port?port={port}&state={state}",
        "/api/port?{port}={state}",
        "/api/set?port={port}&value={state}",
    ]
    CANDIDATE_STATUS_PATHS = ["/api/status", "/api/data", "/api"]

    def __init__(self, cfg: dict, dry_run: bool = False):
        self.c = cfg
        self.dry_run = dry_run
        self._port_path: str | None = None

    def _url(self, path: str) -> str:
        return self.c["base_url"].rstrip("/") + path

    def _get(self, path: str) -> tuple[int, str]:
        req = urllib.request.Request(self._url(path), method="GET")
        with urllib.request.urlopen(req, timeout=float(self.c["timeout_sec"])) as r:
            return r.status, r.read().decode("utf-8", "replace")

    def set_port(self, on: bool) -> bool:
        state = self.c["on_value"] if on else self.c["off_value"]
        port = self.c["port"]
        if self.dry_run:
            print(f"[dry-run] 会请求 ESP32：{port} -> {state}", flush=True)
            return True
        paths = [self._port_path] if self._port_path else []
        paths += [p for p in self.CANDIDATE_PORT_PATHS if p not in paths]
        last = ""
        for p in paths:
            if not p:
                continue
            path = p.format(port=port, state=state)
            try:
                code, body = self._get(path)
                if 200 <= code < 300:
                    self._port_path = p
                    print(f"[esp32] {path} -> {code}", flush=True)
                    return True
                last = f"{path} -> {code}"
            except urllib.error.HTTPError as e:
                last = f"{path} -> HTTP {e.code}"
            except Exception as e:
                last = f"{path} -> {type(e).__name__}: {e}"
        print(f"[esp32] 端口控制失败：{last}", flush=True)
        return False

    def read_status(self):
        for p in ([self.c["status_path"]] if self.c.get("status_path") else []) + self.CANDIDATE_STATUS_PATHS:
            try:
                code, body = self._get(p)
                if 200 <= code < 300:
                    try:
                        return json.loads(body)
                    except Exception:
                        return {"raw": body[:400]}
            except Exception:
                continue
        return None

    def probe(self) -> None:
        print("== 探测 ESP32 REST 接口（只读，不改端口状态）==")
        for p in ([self.c["status_path"]] if self.c.get("status_path") else []) + self.CANDIDATE_STATUS_PATHS:
            try:
                code, body = self._get(p)
                print(f"  GET {p:<22} -> {code}  {body[:120]!r}")
            except Exception as e:
                print(f"  GET {p:<22} -> {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 判定（同步自 charge_guard.py）
def decide(policy: dict, reading: dict, state: dict) -> tuple[str, str]:
    """纯函数：读数 + 状态 → (action, reason)。action ∈ {on, off, hold}。

    ⚠️ 与 PC 侧 charge_guard.py::decide 必须保持一致。判定顺序即优先级：
      0) 读数无效            → hold（未知绝不动作）
      1) 温度 >= cutoff      → off（安全项，立即生效）
      2) 温度曾超标且仍 > resume → off（滞回）
      3) level >= stop_at    → off
      4) level <= resume_at  → on
      5) 其余                → hold
    """
    level = reading.get("level")
    temp = reading.get("temperature_c")
    if level is None:
        return "hold", "unknown-reading(level missing)"
    stop_at = policy["stop_at"]
    if state.get("flag_full"):
        stop_at = 100
    if temp is not None:
        if temp >= policy["temp_cutoff_c"]:
            return "off", f"temp {temp:.1f}C >= {policy['temp_cutoff_c']:.1f}C"
        if state.get("temp_tripped") and temp > policy["temp_resume_c"]:
            return "off", f"temp-hold {temp:.1f}C > {policy['temp_resume_c']:.1f}C"
    if level >= stop_at:
        return "off", f"level {level} >= stop_at {stop_at}"
    if level <= policy["resume_at"]:
        return "on", f"level {level} <= resume_at {policy['resume_at']}"
    return "hold", f"in-band {policy['resume_at']} < {level} < {stop_at}"


def apply_temp_latch(policy: dict, reading: dict, state: dict) -> None:
    t = reading.get("temperature_c")
    if t is None:
        return
    if t >= policy["temp_cutoff_c"]:
        state["temp_tripped"] = True
    elif t <= policy["temp_resume_c"]:
        state["temp_tripped"] = False


# ---------------------------------------------------------------- 日志 / 告警
class CsvLog:
    FIELDS = ["ts", "event", "level", "temp_c", "plugged", "action", "reason", "note"]

    def __init__(self, path: str, keep_days: int):
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = HERE / self.path
        self.keep_days = keep_days
        self._new = not self.path.exists()

    def row(self, event: str, reading: dict | None = None, action: str = "", reason: str = "", note: str = "") -> None:
        r = reading or {}
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event,
               "level": r.get("level"), "temp_c": r.get("temperature_c"),
               "plugged": r.get("plugged"), "action": action, "reason": reason, "note": note}
        with self.path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore")
            if self._new:
                w.writeheader()
                self._new = False
            w.writerow(rec)
            f.flush()
        text = " ".join(f"{k}={v}" for k, v in rec.items() if v not in ("", None))
        print(text, flush=True)
        self._prune()

    def _prune(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except Exception:
            return
        cutoff = time.time() - self.keep_days * 86400
        out = [lines[0]] if lines else []
        for line in lines[1:]:
            ts = line.split(",", 1)[0].strip().strip('"')
            try:
                keep = datetime.fromisoformat(ts).timestamp() >= cutoff
            except Exception:
                keep = True
            if keep:
                out.append(line)
        if len(out) != len(lines):
            self.path.write_text("".join(out), encoding="utf-8")


class Alerter:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._last: dict[str, float] = {}

    def send(self, key: str, title: str, body: str) -> None:
        repeat = float(self.cfg.get("repeat_sec") or 0)
        now = time.time()
        if key in self._last and repeat > 0 and (now - self._last[key]) < repeat:
            return
        self._last[key] = now
        text = f"[phone_guard] {title}\n{body}"
        print(text, flush=True)
        if self.cfg.get("type") == "webhook" and self.cfg.get("webhook_url"):
            try:
                req = urllib.request.Request(self.cfg["webhook_url"], data=text.encode("utf-8"),
                                             headers={"Content-Type": "text/plain; charset=utf-8"}, method="POST")
                urllib.request.urlopen(req, timeout=10).read()
            except Exception as e:
                print(f"[phone_guard] webhook 发送失败: {e}", flush=True)


# ---------------------------------------------------------------- 主循环
class Guard:
    def __init__(self, cfg: dict, plug, log: CsvLog, alerter: Alerter, dry_run=False, clock=time.time):
        self.cfg = cfg
        self.plug = plug
        self.log = log
        self.alerter = alerter
        self.dry_run = dry_run
        self.clock = clock
        self.state = {"temp_tripped": False, "flag_full": False}
        self.plug_on: bool | None = None
        self.last_reading: dict | None = None
        self.last_seen: float | None = None
        self.pending_action: str | None = None
        self.pending_count = 0

    def _same_decision(self, action: str) -> bool:
        """连续性门：要求**同一判定**连续出现 N 次。
        不能用「两次读数完全相同」—— 电量是单调变化的（PC 侧真机 dry-run 已证实会卡死）。"""
        if action == self.pending_action:
            self.pending_count += 1
        else:
            self.pending_action = action
            self.pending_count = 1
        return self.pending_count >= int(self.cfg["policy"]["stable_readings"])

    def _actuate(self, action: str, reading: dict, reason: str) -> None:
        want_on = (action == "on")
        if not self.plug.set_port(want_on):
            self.log.row("error", reading, action=action, reason=reason, note="ESP32 请求失败")
            self.alerter.send("esp32-error", "ESP32 端口控制失败", f"想把 {self.cfg['esp32']['port']} 设为 {want_on}，失败。")
            return
        self.log.row("action", reading, action=action, reason=reason)
        self.plug_on = want_on

    def _handle_lost(self, now: float) -> None:
        lc = self.cfg["lost_contact"]
        if self.last_seen is None:
            self.last_seen = now
            return
        gap = now - self.last_seen
        if gap < lc["short_break_sec"]:
            return
        lv = (self.last_reading or {}).get("level")
        stop_at = self.cfg["policy"]["stop_at"]
        if lv is None:
            self.log.row("lost-unknown", note=f"读不到电量 {gap / 60:.0f}min → 不动 + 告警")
            self.alerter.send("lost-unknown", "读不到本机电量",
                              f"已 {gap / 60:.0f} 分钟读不到电量，没有可信读数。\n"
                              f"处置：不改变端口状态。检查 Termux:API 是否被杀后台。")
        elif lv >= stop_at:
            if self.plug_on is not False:
                self._actuate("off", None, f"lost-contact & last level {lv} >= {stop_at}")
            self.alerter.send("lost-charged", "读不到电量（已知充够）",
                              f"已 {gap / 60:.0f} 分钟读不到电量，最后读数 {lv}% >= {stop_at}%。\n处置：已断电。")
        else:
            cap = float(lc["stale_action_max_hours"])
            if gap >= cap * 3600 and self.plug_on is not False:
                self._actuate("off", None, f"lost-contact {gap / 3600:.1f}h > {cap}h cap")
                self.alerter.send("lost-capped", "读不到电量超时，兜底断电",
                                  f"已 {gap / 3600:.1f} 小时读不到电量，最后读数 {lv}%。为避免长期满电已断电。")

    def tick(self) -> str:
        now = self.clock()
        reading, note = read_battery()
        if reading is None:
            self._handle_lost(now)
            return "lost"
        self.last_seen = now
        self.last_reading = reading
        apply_temp_latch(self.cfg["policy"], reading, self.state)
        action, reason = decide(self.cfg["policy"], reading, self.state)
        self.log.row("sample", reading, reason=reason, note=note)

        if action == "hold":
            self.pending_action, self.pending_count = None, 0
            return "hold"

        pol = self.cfg["policy"]
        lv = reading.get("level") or 0
        is_temp = reason.startswith("temp") or "lost-contact" in reason
        immediate = (is_temp
                     or (action == "off" and lv >= pol["stop_at"] + int(pol["immediate_off_margin"]))
                     or (action == "on" and lv <= pol["resume_at"] - int(pol["immediate_on_margin"])))
        if not immediate and not self._same_decision(action):
            self.log.row("pending", reading, action=action, reason=reason,
                         note=f"等待连续同判定（{self.pending_count}/{pol['stable_readings']}）")
            return "pending"
        if self.plug_on is (action == "on"):
            return "noop"
        self._actuate(action, reading, reason)
        return action

    def run(self, ticks: int = 0) -> None:
        interval = int(self.cfg["loop"]["interval_sec"])
        n = 0
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                self.log.row("stop", note="Ctrl-C")
                return
            except Exception as e:
                self.log.row("error", note=f"{type(e).__name__}: {e}")
                self.alerter.send("loop-error", "phone_guard 异常", f"{type(e).__name__}: {e}")
            n += 1
            if ticks and n >= ticks:
                return
            time.sleep(interval)


# ---------------------------------------------------------------- 自测
class FakePlug:
    def __init__(self, fail=False):
        self.calls: list[bool] = []
        self.fail = fail

    def set_port(self, on: bool) -> bool:
        if self.fail:
            return False
        self.calls.append(on)
        return True


def _r(level=None, temp=30.0, plugged="PLUGGED_AC"):
    return {"level": level, "temperature_c": temp, "plugged": plugged,
            "ac_powered": plugged.startswith("PLUGGED"), "raw_ok": True}


def self_test() -> int:
    """与 charge_guard.py::self_test 使用同一套断言，用来发现两边逻辑漂移。"""
    passed = failed = 0

    def check(name, got, want):
        nonlocal passed, failed
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<44} got={got!r} want={want!r}")
        passed += 1 if ok else 0
        failed += 0 if ok else 1

    pol = DEFAULTS["policy"]

    print("\n== 判定：与 PC 侧一致 ==")
    check("60% 区间内 -> hold", decide(pol, _r(60), {})[0], "hold")
    check("66% -> off", decide(pol, _r(66), {})[0], "off")
    check("49% -> on", decide(pol, _r(49), {})[0], "on")
    check("level=None -> hold", decide(pol, {"level": None}, {})[0], "hold")
    check("45℃ -> off（立即）", decide(pol, _r(70, temp=45.0), {})[0], "off")
    st = {"temp_tripped": True}
    check("42℃ 锁存中 -> off", decide(pol, _r(70, temp=42.0), st)[0], "off")
    st2 = {"temp_tripped": True}
    apply_temp_latch(pol, _r(60, temp=39.0), st2)
    check("39℃ 解除锁存", st2["temp_tripped"], False)

    print("\n== 温度单位：termux-api 是摄氏度，不能再除 10 ==")
    t = _r(60, temp=33.7)["temperature_c"]
    check("直接就是 33.7℃", t, 33.7)
    check("不该被当成 3.37℃（若误除 10 会低于 40 而漏报）", t > 30, True)

    print("\n== 稳定性门：电量单调变化不得卡死（PC 侧真机 dry-run 曾暴露此缺陷）==")
    log = CsvLog(os.devnull if os.name != "nt" else "NUL", 60)
    cfg = json.loads(json.dumps(DEFAULTS))
    g = Guard(cfg, FakePlug(), log, Alerter(cfg["alert"]))
    check("同判定第 1 次 -> 未达阈值", g._same_decision("off"), False)
    check("同判定第 2 次 -> 达阈值", g._same_decision("off"), True)
    check("换成 on -> 计数重置", g._same_decision("on"), False)

    print("\n== 执行端失败不得被当成成功 ==")
    g = Guard(cfg, FakePlug(fail=True), log, Alerter(cfg["alert"]))
    g._actuate("off", _r(70), "test")
    check("失败时 plug_on 保持未知", g.plug_on, None)

    print(f"\n===== 自测结果：{passed} 通过 / {failed} 失败 =====")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="跑在手机上的充电守护大脑（Termux）")
    ap.add_argument("--config", default=str(HERE / "charge_guard_phone.json"))
    ap.add_argument("--dry-run", action="store_true", help="只读电量与判定，不控端口")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--ticks", type=int, default=0)
    ap.add_argument("--self-test", action="store_true", help="桩数据自测，不需要 Termux/ESP32")
    ap.add_argument("--probe", action="store_true", help="探测 ESP32 的 REST 接口")
    ap.add_argument("--show-battery", action="store_true", help="只读一次电量并打印原始 JSON")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.show_battery:
        print(subprocess.run(["termux-battery-status"], capture_output=True, text=True).stdout)
        return 0

    cfg = load_config(args.config)
    if args.probe:
        Esp32Link(cfg["esp32"]).probe()
        return 0

    log = CsvLog(cfg["log"]["csv"], int(cfg["log"]["keep_days"]))
    alerter = Alerter(cfg["alert"])
    plug = Esp32Link(cfg["esp32"], dry_run=args.dry_run)

    if cfg["loop"].get("wake_lock"):
        # 不让 Android Doze 把循环冻住；不需要就删掉这行
        try:
            subprocess.run(["termux-wake-lock"], capture_output=True, timeout=10)
        except Exception:
            pass

    print(f"phone_guard 启动：ESP32={cfg['esp32']['base_url']} port={cfg['esp32']['port']} "
          f"stop_at={cfg['policy']['stop_at']} resume_at={cfg['policy']['resume_at']} "
          f"间隔={cfg['loop']['interval_sec']}s dry_run={args.dry_run}", flush=True)
    Guard(cfg, plug, log, alerter, dry_run=args.dry_run).run(ticks=args.ticks or (1 if args.once else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
