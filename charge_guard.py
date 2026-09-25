#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
charge_guard.py —— 常驻插电安卓设备的充电区间守护

受控对象：Redmi K70 Pro（Xiaomi 23117RK66C / manet / Android 16），常驻家中长期接在插座上。
读取端  ：主机经「无线 ADB」读 `dumpsys battery`
执行端  ：小米智能插座 4（ZNCZ401KK）经 python-miio 通断

=====================================================================
控制策略（按「常驻插电设备」定，不是随身机）
=====================================================================
  stop_at   = 65   达到即断电
  resume_at = 50   回落到此才通电
  依据（Battery University BU-808，NMC 圆整估算，非本机电芯实测）：
    · Table 4：4.06V(≈80% SoC) 仅 600-1000 循环，3.92V(≈65%) 有 1200-2000
    · Table 2：40% DoD ≈ 1000 循环，20% DoD ≈ 2000 循环
    · Table 3：25℃ 存放一年，100% SoC 剩 80%，40% SoC 剩 96%
    完整的推导与反例见 charge-strategy-review.md。
  ⚠️ 原方案的 80/35 是「随身机」档位；本机不移动，改停中间区间。

=====================================================================
加固点（编号对应 charge-strategy-review.md 第四节 / 第五节）
=====================================================================
  1  端口三层兜底：缓存 → mDNS → 并行扫描(30000-50000)，候选合并后逐个用功能探针筛
  2  残留 offline transport 会让之后所有 connect 失败 ⇒ 失败后清 daemon 再试
  3  同一候选端口最多试 N 次（息屏时 Wi-Fi 省电会随机丢 SYN）
  4  在线判定用 `shell echo ok`，不信 `adb devices`（刚 connect 上时 shell 未就绪会返回空串）
  5  解析严格化：level 必须 ^level: (\\d+)$ 且 0..100；temperature 是 0.1℃
  6  执行器回读：发完通断指令后回读 `AC powered` 确认生效
  7  失联分级：短断保持；>stale_sec 且已知充够→断电告警；读数未知→保持通电告警；超时改判
  8  趋势日志：CSV 逐次记录 + 每日汇总，保留 keep_days 天
  9  UTF-8 钉死（无控制台进程里 Python 会退回 GBK）
  10 --dry-run 只读数不控电

用法：
  python charge_guard.py --dry-run       # 只读数、打印判定，不控电
  python charge_guard.py                 # 正式运行
  python charge_guard.py --once          # 跑一轮就退出（排错用）
  python charge_guard.py --self-test     # 桩数据自测，不需要真机与插座
  python charge_guard.py --config x.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from pathlib import Path


# ---------------------------------------------------------------- 9. UTF-8 钉死
def pin_utf8() -> None:
    """无控制台的进程里 Python 会退回 GBK 输出，父进程按 UTF-8 解码 → 乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------- 配置
DEFAULTS = {
    "adb_path": "",
    "actuator": "miio",
    "charger_ble": {
        "python_exe": "",
        "script": "third_party/xiaomi-ad1204-python/ad1204_ble.py",
        "token_env": "CHARGER_BLE_TOKEN",
        "token": "",
        "address": "",
        "port": "c1",
        "timeout_sec": 90,
        "probe_watch_sec": 5,
    },
    "phone": {"ip": "", "port_cache_file": "adb_port_cache.json"},
    "scan": {"low": 30000, "high": 50000, "timeout_sec": 0.35, "workers": 600},
    "connect": {
        "attempts_per_port": 3,
        "restart_server_before_attempt": "auto",
        "probe_cmd": "echo ok",
        "probe_expect": "ok",
    },
    "policy": {
        "stop_at": 65,
        "resume_at": 50,
        "stable_readings": 2,
        "immediate_off_margin": 3,
        "immediate_on_margin": 3,
        "temp_cutoff_c": 45.0,
        "temp_resume_c": 40.0,
        "full_charge_flag_file": "full_charge.once",
        "full_charge_target": 100,
        "full_charge_done_at": 98,
    },
    "plug": {
        "enabled": True,
        "ip": "",
        "token": "",
        "model": "",
        "on_siid": None,
        "on_piid": None,
        "power_siid": None,
        "power_piid": None,
        "readback_timeout_sec": 20,
    },
    "loop": {"interval_sec": 300, "readback_poll_sec": 2},
    "lost_contact": {"short_break_sec": 600, "stale_sec": 1800, "stale_action_max_hours": 12},
    "log": {"csv": "charge_guard_log.csv", "keep_days": 30},
    "alert": {"type": "console", "webhook_url": "", "repeat_sec": 21600, "smtp": {}},
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
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = _deep_merge(DEFAULTS, cfg)

    # 本机覆盖层（不进 git）：密钥、只在本机成立的路径放这里，受版本控制的配置保持干净。
    # 约定：<配置名>.local.json，内容只写要覆盖的键（charge_guard.json → charge_guard.local.json）
    p = Path(path)
    local = p.with_name(p.stem + ".local.json")
    if local.exists():
        cfg = _deep_merge(cfg, json.loads(local.read_text(encoding="utf-8")))

    # 密钥不进 git：插座 / 充电器 token 都可用环境变量覆盖前面的值
    env_token = os.environ.get("CHARGE_GUARD_PLUG_TOKEN")
    if env_token:
        cfg["plug"]["token"] = env_token
    env_ble = os.environ.get(cfg["charger_ble"].get("token_env") or "CHARGER_BLE_TOKEN")
    if env_ble:
        cfg["charger_ble"]["token"] = env_ble

    p = cfg["policy"]
    if not (0 <= p["resume_at"] < p["stop_at"] <= 100):
        raise ValueError(
            f"配置非法：要求 0 <= resume_at({p['resume_at']}) < stop_at({p['stop_at']}) <= 100"
        )
    if p["temp_resume_c"] >= p["temp_cutoff_c"]:
        raise ValueError("配置非法：temp_resume_c 必须小于 temp_cutoff_c")
    if not cfg["phone"]["ip"]:
        raise ValueError("配置非法：phone.ip 为空")
    return cfg


# ---------------------------------------------------------------- 8. 日志
class CsvLog:
    FIELDS = [
        "ts", "event", "level", "temp_c", "status", "ac_powered", "voltage_mv",
        "charge_counter_uah", "plug_power_w", "action", "reason", "port", "note",
    ]

    def __init__(self, path: str, keep_days: int):
        self.path = Path(path)
        self.keep_days = keep_days
        self._new = not self.path.exists()
        self._day = date.today()
        self._acc = {"samples": 0, "on": 0, "off": 0, "min_level": None, "max_level": None,
                     "max_temp": None, "errors": 0}

    def _open(self):
        f = self.path.open("a", newline="", encoding="utf-8")
        w = csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore")
        if self._new:
            w.writeheader()
            self._new = False
        return f, w

    def row(self, event: str, reading: dict | None = None, action: str = "",
            reason: str = "", port: str = "", plug_power_w=None, note: str = "") -> None:
        r = reading or {}
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "level": r.get("level"),
            "temp_c": r.get("temperature_c"),
            "status": r.get("status"),
            "ac_powered": r.get("ac_powered"),
            "voltage_mv": r.get("voltage_mv"),
            "charge_counter_uah": r.get("charge_counter_uah"),
            "plug_power_w": plug_power_w,
            "action": action,
            "reason": reason,
            "port": port,
            "note": note,
        }
        f, w = self._open()
        try:
            w.writerow(rec)
            f.flush()
        finally:
            f.close()
        self._accumulate(rec)

    def _accumulate(self, rec: dict) -> None:
        lv, tp = rec.get("level"), rec.get("temp_c")
        if isinstance(lv, int):
            self._acc["samples"] += 1
            self._acc["min_level"] = lv if self._acc["min_level"] is None else min(self._acc["min_level"], lv)
            self._acc["max_level"] = lv if self._acc["max_level"] is None else max(self._acc["max_level"], lv)
        if isinstance(tp, (int, float)):
            self._acc["max_temp"] = tp if self._acc["max_temp"] is None else max(self._acc["max_temp"], tp)
        if rec.get("action") == "on":
            self._acc["on"] += 1
        if rec.get("action") == "off":
            self._acc["off"] += 1
        if rec.get("event") == "error":
            self._acc["errors"] += 1

    def maybe_daily_summary(self) -> None:
        today = date.today()
        if today == self._day:
            return
        a = self._acc
        f, w = self._open()
        try:
            w.writerow({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": "daily-summary",
                "note": (f"day={self._day} samples={a['samples']} on={a['on']} off={a['off']} "
                         f"level {a['min_level']}-{a['max_level']} max_temp={a['max_temp']} errors={a['errors']}"),
            })
            f.flush()
        finally:
            f.close()
        self._day = today
        self._acc = {"samples": 0, "on": 0, "off": 0, "min_level": None, "max_level": None,
                     "max_temp": None, "errors": 0}
        self._prune()

    def _prune(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except Exception:
            return
        if not lines:
            return
        cutoff = time.time() - self.keep_days * 86400
        out = [lines[0]]
        for line in lines[1:]:
            ts = line.split(",", 1)[0].strip().strip('"')
            try:
                t = datetime.fromisoformat(ts).timestamp()
            except Exception:
                out.append(line)
                continue
            if t >= cutoff:
                out.append(line)
        if len(out) != len(lines):
            self.path.write_text("".join(out), encoding="utf-8")


# ---------------------------------------------------------------- 告警
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
        kind = self.cfg.get("type", "none")
        text = f"[charge_guard] {title}\n{body}"
        if kind in ("console", "none"):
            print(text, flush=True)
            return
        if kind == "webhook" and self.cfg.get("webhook_url"):
            try:
                req = urllib.request.Request(
                    self.cfg["webhook_url"], data=text.encode("utf-8"),
                    headers={"Content-Type": "text/plain; charset=utf-8"}, method="POST")
                urllib.request.urlopen(req, timeout=10).read()
            except Exception as e:
                print(f"[charge_guard] 告警发送失败(webhook): {e}", flush=True)
            return
        if kind == "smtp":
            s = self.cfg.get("smtp") or {}
            if not all([s.get("host"), s.get("user"), s.get("password"), s.get("to")]):
                print("[charge_guard] 告警发送失败: smtp 配置不完整", flush=True)
                return
            try:
                import smtplib
                from email.mime.text import MIMEText
                msg = MIMEText(body, "plain", "utf-8")
                msg["Subject"] = title
                msg["From"] = s["user"]
                msg["To"] = s["to"]
                if int(s.get("port", 465)) == 465:
                    with smtplib.SMTP_SSL(s["host"], 465, timeout=15) as sv:
                        sv.login(s["user"], s["password"])
                        sv.send_message(msg)
                else:
                    with smtplib.SMTP(s["host"], int(s["port"]), timeout=15) as sv:
                        sv.starttls()
                        sv.login(s["user"], s["password"])
                        sv.send_message(msg)
            except Exception as e:
                print(f"[charge_guard] 告警发送失败(smtp): {e}", flush=True)


# ---------------------------------------------------------------- 4/5. ADB
class AdbLink:
    """
    负责：找 adb → 发现候选端口（缓存/mDNS/扫描）→ 连接 → 用功能探针确认在线 → 读电池。
    """

    def __init__(self, cfg: dict, log: CsvLog, alerter: Alerter):
        self.cfg = cfg
        self.log = log
        self.alerter = alerter
        self.adb = self._resolve_adb(cfg["adb_path"])
        self.cache_file = Path(cfg["phone"]["port_cache_file"])
        self.session: dict | None = None

    # ---- adb 可执行文件
    @staticmethod
    def _resolve_adb(configured: str) -> str:
        if configured and Path(configured).exists():
            return configured
        import shutil
        found = shutil.which("adb")
        if found:
            return found
        raise FileNotFoundError(
            f"找不到 adb。配置里的路径不存在：{configured!r}，PATH 里也没有 adb。\n"
            r"本机可用：D:\MuMuPlayer-12.0\nx_main\adb.exe"
        )

    def _run(self, args: list[str], timeout: int = 20) -> tuple[int, str]:
        try:
            p = subprocess.run([self.adb] + args, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, ""
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, (out + err).replace("\r", "")

    def _server_cycle(self) -> None:
        self._run(["kill-server"], timeout=15)
        self._run(["start-server"], timeout=30)

    # ---- 1. 候选端口
    def _cached_port(self) -> int | None:
        try:
            d = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if d.get("ip") == self.cfg["phone"]["ip"] and isinstance(d.get("port"), int):
                return d["port"]
        except Exception:
            pass
        return None

    def _save_cache(self, port: int) -> None:
        try:
            self.cache_file.write_text(
                json.dumps({"ip": self.cfg["phone"]["ip"], "port": port,
                            "ts": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    def _mdns_ports(self) -> list[int]:
        """mDNS 既可能过期（广告已关闭的端口）也可能不全 —— 只当候选，别当判据。
        2026-09-25 实机：广告 38179(死) + 41723(活)，而实际开放的是 40023 + 41723。"""
        ports: list[int] = []
        ip = self.cfg["phone"]["ip"]
        for _ in range(3):  # daemon 刚启动那几秒必定是空的，要轮询
            rc, out = self._run(["mdns", "services"], timeout=15)
            for line in out.splitlines():
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", line)
                if not m:
                    continue
                if m.group(1) != ip:
                    continue
                p = int(m.group(2))
                if p not in ports:
                    ports.append(p)
            if ports:
                break
            time.sleep(1)
        return ports

    def _scan_ports(self) -> list[int]:
        s = self.cfg["scan"]
        ip = self.cfg["phone"]["ip"]

        def probe(p: int) -> int | None:
            sk = socket.socket()
            sk.settimeout(float(s["timeout_sec"]))
            try:
                sk.connect((ip, p))
                return p
            except Exception:
                return None
            finally:
                sk.close()

        found: list[int] = []
        rng = range(int(s["low"]), int(s["high"]) + 1)
        with ThreadPoolExecutor(max_workers=int(s["workers"])) as ex:
            for r in ex.map(probe, rng):
                if r:
                    found.append(r)
        return sorted(found)

    # ---- 3/4. 连接 + 功能探针
    def _try_connect(self, port: int) -> dict | None:
        ip = self.cfg["phone"]["ip"]
        serial = f"{ip}:{port}"
        attempts = int(self.cfg["connect"]["attempts_per_port"])
        mode = self.cfg["connect"].get("restart_server_before_attempt", "auto")
        need_reset = (mode == "always")
        for i in range(attempts):
            if need_reset:
                self._server_cycle()
            rc, out = self._run(["connect", serial], timeout=25)
            if "connected" in out.lower():
                if self._probe(serial):
                    return {"serial": serial, "ip": ip, "port": port}
                # 连上了但 shell 还没就绪 → 短暂等待后重探，仍不通就当失败
                time.sleep(2)
                if self._probe(serial):
                    return {"serial": serial, "ip": ip, "port": port}
            self._run(["disconnect", serial], timeout=15)
            need_reset = True  # 2. 失败之后一律清 daemon（残留 offline transport）
        return None

    def _probe(self, serial: str) -> bool:
        """4. 唯一可靠的在线判据：能跑通一条 shell 命令。
        ⚠️ 刚 connect 上时 `adb shell ...` 会返回空字符串且不报错，
        只看 `adb devices` 里的 device 会得到「在线但全空」的假数据。"""
        cmd = self.cfg["connect"]["probe_cmd"]
        expect = self.cfg["connect"]["probe_expect"]
        rc, out = self._run(["-s", serial, "shell", cmd], timeout=15)
        return expect in out.strip()

    def ensure_session(self) -> dict | None:
        if self.session and self._probe(self.session["serial"]):
            return self.session
        self.session = None

        candidates: list[int] = []
        cached = self._cached_port()
        if cached:
            candidates.append(cached)
        for p in self._mdns_ports():
            if p not in candidates:
                candidates.append(p)

        for p in candidates:
            s = self._try_connect(p)
            if s:
                self.session = s
                self._save_cache(p)
                return s

        # 兜底：并行扫描。只有在缓存与 mDNS 都失效时才付这个代价（实测约 12s）。
        self.log.row("scan", note=f"缓存与 mDNS 均失败（候选={candidates}），启动端口扫描")
        for p in self._scan_ports():
            if p in candidates:
                continue
            s = self._try_connect(p)
            if s:
                self.session = s
                self._save_cache(p)
                return s
        return None

    # ---- 5. 读电池（严格解析）
    @staticmethod
    def parse_battery(text: str) -> dict:
        """
        严格解析 dumpsys battery。返回的 level 为 None 表示「未知」，调用方不得动作。
        ⚠️ temperature 单位是 0.1℃（45℃ = 450）；level 缺失/空串绝不能当 0 或当满。
        """
        out: dict = {
            "level": None, "temperature_c": None, "status": None, "ac_powered": None,
            "voltage_mv": None, "charge_counter_uah": None, "health": None,
            "raw_ok": False,
        }
        if not text or "level:" not in text:
            return out
        out["raw_ok"] = True

        m = re.search(r"^\s*level:\s*(\d+)\s*$", text, re.M)
        if m:
            v = int(m.group(1))
            if 0 <= v <= 100:
                out["level"] = v

        m = re.search(r"^\s*temperature:\s*(\d+)\s*$", text, re.M)
        if m:
            out["temperature_c"] = int(m.group(1)) / 10.0  # 0.1℃ → ℃

        m = re.search(r"^\s*status:\s*(\d+)\s*$", text, re.M)
        if m:
            out["status"] = int(m.group(1))  # 2=充电 3=放电 4=未充电 5=满

        m = re.search(r"^\s*AC powered:\s*(true|false)\s*$", text, re.M | re.I)
        if m:
            out["ac_powered"] = m.group(1).lower() == "true"

        m = re.search(r"^\s*voltage:\s*(\d+)\s*$", text, re.M)
        if m:
            out["voltage_mv"] = int(m.group(1))

        m = re.search(r"^\s*Charge counter:\s*(\d+)\s*$", text, re.M)
        if m:
            out["charge_counter_uah"] = int(m.group(1))

        m = re.search(r"^\s*health:\s*(\d+)\s*$", text, re.M)
        if m:
            out["health"] = int(m.group(1))
        return out

    def read_battery(self) -> tuple[dict | None, bool]:
        """返回 (reading, session_ok)。reading 为 None 表示本轮无有效读数。"""
        s = self.ensure_session()
        if not s:
            return None, False
        rc, out = self._run(["-s", s["serial"], "shell", "dumpsys battery"], timeout=20)
        reading = self.parse_battery(out)
        if not reading["raw_ok"]:
            # 输出为空 / 没有 level 字段 → 可能是 session 已死，用探针复核一次
            if not self._probe(s["serial"]):
                self._run(["disconnect", s["serial"]], timeout=15)
                self.session = None
                return None, False
            return reading, True  # 在线，但读数确实拿不到（记 unknown，不动作）
        return reading, True


# ---------------------------------------------------------------- 6. 插座
class PlugLink:
    """python-miio 控制。⚠️ 需在真机上实测 siid/piid 后再填配置；本类不提供默认编号。"""

    def __init__(self, cfg: dict, log: CsvLog, alerter: Alerter, dry_run: bool = False):
        self.cfg = cfg
        self.log = log
        self.alerter = alerter
        self.dry_run = dry_run
        self._path_used: str | None = None

    def configured(self) -> bool:
        c = self.cfg
        return bool(c.get("enabled") and c.get("ip") and c.get("token")
                    and c.get("on_siid") is not None and c.get("on_piid") is not None)

    def _device(self):
        from miio import MiotDevice  # 延迟导入：没装 python-miio 也能跑 --self-test
        if self.cfg.get("model"):
            return MiotDevice(ip=self.cfg["ip"], token=self.cfg["token"], model=self.cfg["model"])
        return MiotDevice(ip=self.cfg["ip"], token=self.cfg["token"])

    def set_power(self, on: bool) -> bool:
        # dry-run 必须在「已配置」之前判：dry-run 的意义就是还没有插座凭据时先验通路
        if self.dry_run:
            print(f"[dry-run] 会给插座发 {'通电' if on else '断电'}（不实际执行）", flush=True)
            return True
        if not self.configured():
            self.alerter.send("plug-misconfig", "插座未配置",
                              "plug.ip / plug.token / plug.on_siid / plug.on_piid 未填齐，无法控电。")
            return False
        siid = int(self.cfg["on_siid"])
        piid = int(self.cfg["on_piid"])
        try:
            dev = self._device()
            value = bool(on)
            if self._path_used in (None, "set_property_by"):
                try:
                    dev.set_property_by(siid, piid, value)
                    self._path_used = "set_property_by"
                    return True
                except Exception:
                    self._path_used = None
            # 回退路径：MIoT 通用 set_properties（python-miio 未适配该型号时用）
            dev.send("set_properties", [{"did": f"set-{siid}-{piid}", "siid": siid,
                                         "piid": piid, "value": value}])
            self._path_used = "set_properties"
            return True
        except Exception as e:
            self.alerter.send("plug-error", "插座指令失败", f"set_power({on}) 失败：{e}")
            return False

    def read_power_w(self):
        c = self.cfg
        if not (c.get("power_siid") is not None and c.get("power_piid") is not None):
            return None
        if self.dry_run:
            return None
        try:
            v = self._device().get_property_by(int(c["power_siid"]), int(c["power_piid"]))
            return round(float(v), 1) if v is not None else None
        except Exception:
            return None


class FakePlug:
    """自测用：记录调用、可注入失败。"""

    def __init__(self, fail: bool = False, power_w=None):
        self.calls: list[bool] = []
        self.fail = fail
        self.power_w = power_w
        self.enabled = True

    def configured(self) -> bool:
        return True

    def set_power(self, on: bool) -> bool:
        if self.fail:
            return False
        self.calls.append(on)
        return True

    def read_power_w(self):
        return self.power_w


# ---------------------------------------------------------------- 决策（纯函数）
def decide(policy: dict, reading: dict, state: dict) -> tuple[str, str]:
    """
    纯函数：读数 + 状态 → (action, reason)。action ∈ {on, off, hold}。
    做成纯函数是为了让自测有真实意义 —— 可以脱离真机与插座穷举边界。

    判定顺序（顺序即优先级）：
      0) 读数无效            → hold（未知不动作，这是最关键的一条）
      1) 温度 >= cutoff      → off（安全项，立即生效，不等稳定计数）
      2) 温度曾超标且仍 > resume → off（滞回，避免在 40-45℃ 间反复通断）
      3) 已在区间上沿/超过   → off
      4) 已到区间下沿        → on
      5) 其余                → hold
    """
    level = reading.get("level")
    temp = reading.get("temperature_c")

    if level is None:
        return "hold", "unknown-reading(level missing)"

    stop_at = policy["stop_at"]
    if state.get("flag_full"):
        stop_at = policy["full_charge_target"]

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
    """温度锁存：>=cutoff 置位，<=resume 复位。与 decide 分开，保持 decide 纯净。"""
    t = reading.get("temperature_c")
    if t is None:
        return
    if t >= policy["temp_cutoff_c"]:
        state["temp_tripped"] = True
    elif t <= policy["temp_resume_c"]:
        state["temp_tripped"] = False


def probe_plug(cfg: dict) -> int:
    """
    只读枚举 MIoT 属性表，帮你在真机上定位 siid/piid。
    ⚠️ 不发送任何写指令（不会把插座切来切去），可以放心跑。
    ZNCZ401KK 是较新型号，python-miio 可能没有专用类，所以不预设编号。
    """
    plug = cfg["plug"]
    if not (plug.get("ip") and plug.get("token")):
        print("需要先在 charge_guard.json 里填 plug.ip 与 plug.token（用 miiocli cloud 获取）。")
        return 2
    try:
        from miio import MiotDevice
    except ImportError:
        print("没装 python-miio：pip install -U python-miio\n"
              "若认不出 ZNCZ401KK，装开发版：pip install git+https://github.com/rytilahti/python-miio.git")
        return 2

    kw = {"ip": plug["ip"], "token": plug["token"]}
    if plug.get("model"):
        kw["model"] = plug["model"]
    dev = MiotDevice(**kw)

    pairs = [(siid, piid) for siid in range(1, 11) for piid in range(1, 11)]
    print(f"探测 {plug['ip']}：枚举 siid 1-10 × piid 1-10（只读）")
    results = []
    try:
        props = [{"siid": s, "piid": p} for s, p in pairs]
        resp = dev.get_properties(props)
        for r in resp:
            if isinstance(r, dict) and r.get("code", 0) == 0:
                results.append((r.get("siid", -1), r.get("piid", -1), r.get("value")))
    except Exception:
        for s, p in pairs:  # 批量不行就逐个来（慢，但更兼容）
            try:
                results.append((s, p, dev.get_property_by(s, p)))
            except Exception:
                pass

    if not results:
        print("没有取到任何属性。检查 token 是否为该设备当前 token（换过 Wi-Fi/重置后 token 会变）。")
        return 1
    print(f"{'siid':>4} {'piid':>4}  value")
    for s, p, v in sorted(results):
        print(f"{s:>4} {p:>4}  {v!r}")
    print("\n怎么认：布尔值 -> 大概率是开关（填进 plug.on_siid / on_piid）；"
          "带小数的数值 -> 功率/电量（填 power_siid / power_piid）。\n"
          "确认后建议先用 --dry-run 跑一轮，再正式运行。")
    return 0


# ---------------------------------------------------------------- 主循环
class Guard:
    def __init__(self, cfg: dict, adb, plug, log: CsvLog, alerter: Alerter,
                 dry_run: bool = False, clock=time.time):
        self.cfg = cfg
        self.adb = adb
        self.plug = plug
        self.log = log
        self.alerter = alerter
        self.dry_run = dry_run
        self.clock = clock
        self.state = {"temp_tripped": False, "flag_full": False}
        self.plug_on: bool | None = None      # 已知的插座状态
        self.last_reading: dict | None = None
        self.last_seen: float | None = None
        self.last_act_key: str | None = None
        self.last_act_ts: float = 0.0
        self.pending_action: str | None = None
        self.pending_count = 0
        self.stale_alert_ts: float | None = None

    # ---- 一次性满充标志
    def _flag_path(self) -> Path:
        return Path(self.cfg["policy"]["full_charge_flag_file"])

    def refresh_flag(self, reading: dict | None) -> None:
        p = self._flag_path()
        if not p.exists():
            self.state["flag_full"] = False
            return
        self.state["flag_full"] = True
        lv = (reading or {}).get("level")
        done_at = self.cfg["policy"]["full_charge_done_at"]
        ac = (reading or {}).get("ac_powered")
        if lv is not None and lv >= done_at:
            p.unlink(missing_ok=True)
            self.state["flag_full"] = False
            self.log.row("flag-cleared", reading, note=f"一次性满充达成（{lv} >= {done_at}）")
        elif ac is False:
            p.unlink(missing_ok=True)
            self.state["flag_full"] = False
            self.log.row("flag-cleared", reading, note="检测到拔线，撤销一次性满充")

    # ---- 稳定性过滤
    def _same_decision(self, action: str) -> bool:
        """稳定性门：要求**同一判定**连续出现 N 次。

        ⚠️ 不能用「两次读数完全相同」——电量在充放过程中是单调变化的，
        65/66/65/66 这种相邻不同值会让门永远打不开（2026-09-25 真机 dry-run 直接暴露：67→66 就把计数重置了）。
        生产间隔 300s 能掩盖它（1% 要几十分钟），但那是运气不是设计。
        改比判定结果：既能过滤 ADB 抖动（抖动会翻转判定），又不会因为真实电量在动而卡死。
        """
        if action == self.pending_action:
            self.pending_count += 1
        else:
            self.pending_action = action
            self.pending_count = 1
        return self.pending_count >= int(self.cfg["policy"]["stable_readings"])

    # ---- 6. 执行 + 回读
    def _actuate(self, action: str, reading: dict, reason: str) -> None:
        want_on = (action == "on")
        ok = self.plug.set_power(want_on)
        if not ok:
            self.log.row("error", reading, action=action, reason=reason, note="插座指令失败")
            return
        self.log.row("action", reading, action=action, reason=reason)
        # 回读确认：断电后 AC powered 应变 false；通电后应能变 true（手机未满时）。
        if self.dry_run or not reading:
            self.plug_on = want_on
            return
        timeout = float(self.cfg["plug"]["readback_timeout_sec"])
        poll = float(self.cfg["loop"]["readback_poll_sec"])
        # 注意：这里用真实时间，不用可注入的 clock —— 否则桩永丬9匹配时会死循环
        t0 = time.time()
        confirmed = False
        while time.time() - t0 <= timeout:
            time.sleep(poll)
            r, ok2 = self.adb.read_battery()
            if not ok2 or not r:
                break
            if r.get("ac_powered") is want_on:
                confirmed = True
                self.last_reading = r
                break
        if confirmed:
            self.plug_on = want_on
            self.log.row("readback-ok", self.last_reading, action=action, reason=reason)
        else:
            self.plug_on = None
            self.log.row("readback-mismatch", self.last_reading, action=action, reason=reason,
                         note=f"{timeout:.0f}s 内未观测到 AC powered={want_on}")
            self.alerter.send("readback-mismatch", "插座动作未被确认",
                              f"发出「{'通电' if want_on else '断电'}」后 {timeout:.0f}s 内没读到 AC powered={want_on}。\n"
                              f"原因可能是手机正被别处供电、miio 指令未生效、或读端失联。")

    # ---- 7. 失联分级
    def _handle_lost(self, now: float) -> None:
        lc = self.cfg["lost_contact"]
        if self.last_seen is None:
            self.last_seen = now
            return
        gap = now - self.last_seen
        if gap < lc["short_break_sec"]:
            self.log.row("lost-short", note=f"失联 {gap:.0f}s，保持原状")
            return
        last = self.last_reading or {}
        lv = last.get("level")
        stop_at = self.cfg["policy"]["stop_at"]
        if lv is None:
            self.plug_on = None
            self.log.row("lost-unknown", note=f"失联 {gap / 60:.0f}min 且最后一次读数未知 → 保持通电 + 告警")
            if gap >= lc["stale_sec"]:
                self.alerter.send("lost-unknown", "充电守护失联（读数未知）",
                                  f"已失联 {gap / 60:.0f} 分钟，且没有可信的历史读数。\n"
                                  f"处置：保持插座通电（没电比过充危险），但无法判断电量。请检查手机 Wi-Fi 与无线调试开关。")
        elif lv >= stop_at:
            if self.plug_on is not False:
                self._actuate("off", None, f"lost-contact & last level {lv} >= stop_at {stop_at}")
            if gap >= lc["stale_sec"]:
                self.alerter.send("lost-charged", "充电守护失联（已知充够）",
                                  f"已失联 {gap / 60:.0f} 分钟，最后读数 {lv}% >= stop_at {stop_at}%。\n"
                                  f"处置：断电，避免电池长时间停在满电。请检查手机 Wi-Fi 与无线调试开关。")
        else:
            max_h = float(lc["stale_action_max_hours"])
            if gap >= max_h * 3600 and self.plug_on is not False:
                # 一直通电 + 长期失联 = 电池可能已到 100% 并停在那里，正是本系统要防的事
                self._actuate("off", None, f"lost-contact {gap / 3600:.1f}h exceeded {max_h}h cap")
                self.alerter.send("lost-capped", "充电守护失联超时，已按兜底断电",
                                  f"失联 {gap / 3600:.1f} 小时（上限 {max_h}h），最后读数 {lv}%。\n"
                                  f"为避免电池长时间停在满电，已执行断电。")
            else:
                self.log.row("lost-keepon", note=f"失联 {gap / 60:.0f}min，最后读数 {lv}% 低于 stop_at → 保持通电")
                if gap >= lc["stale_sec"]:
                    self.alerter.send("lost-keepon", "充电守护失联",
                                      f"已失联 {gap / 60:.0f} 分钟，最后读数 {lv}%。\n"
                                      f"处置：保持通电。请检查手机 Wi-Fi 与无线调试开关。")

    # ---- 单轮
    def tick(self) -> str:
        now = self.clock()
        reading, session_ok = self.adb.read_battery()
        if not session_ok:
            self._handle_lost(now)
            return "lost"

        self.last_seen = now
        if reading:
            self.last_reading = reading
        self.refresh_flag(reading)
        if not reading:
            self.log.row("unknown", note="在线但读数不可用 → 不动作")
            return "unknown"

        apply_temp_latch(self.cfg["policy"], reading, self.state)
        action, reason = decide(self.cfg["policy"], reading, self.state)
        power_w = self.plug.read_power_w() if hasattr(self.plug, "read_power_w") else None
        self.log.row("sample", reading, action="", reason=reason, plug_power_w=power_w,
                     port=(self.adb.session or {}).get("port", "") if hasattr(self.adb, "session") else "")

        if action == "hold":
            self.pending_action = None
            self.pending_count = 0
            return "hold"

        # 安全项（温度）立即执行；明显越界也立即执行；其余要求连续同判定
        pol = self.cfg["policy"]
        is_temp = reason.startswith("temp") or "lost-contact" in reason
        lv = reading.get("level") or 0
        immediate = (
            is_temp
            or (action == "off" and lv >= pol["stop_at"] + int(pol["immediate_off_margin"]))
            or (action == "on" and lv <= pol["resume_at"] - int(pol["immediate_on_margin"]))
        )
        if not immediate and not self._same_decision(action):
            self.log.row("pending", reading, action=action, reason=reason,
                         note=f"等待连续同判定（{self.pending_count}/{pol['stable_readings']}）")
            return "pending"
        if self.plug_on is (action == "on"):
            return "noop"

        self._actuate(action, reading, reason)
        return action

    def run(self, ticks: int = 0) -> None:
        """ticks=0 常驻；ticks=N 跑 N 轮后退出（排错用）。"""
        interval = int(self.cfg["loop"]["interval_sec"])
        n = 0
        while True:
            try:
                self.tick()
                self.log.maybe_daily_summary()
            except KeyboardInterrupt:
                self.log.row("stop", note="收到 Ctrl-C，退出")
                return
            except Exception as e:
                self.log.row("error", note=f"{type(e).__name__}: {e}")
                self.alerter.send("loop-error", "充电守护异常", f"{type(e).__name__}: {e}")
            n += 1
            if ticks and n >= ticks:
                return
            time.sleep(interval)


# ---------------------------------------------------------------- 自测（桩数据）
class FakeAdb:
    """桩：按脚本返回读数；脚本耗尽后返回 tail（用于测回读成功/失败），可模拟空读数与失联。"""

    def __init__(self, script: list, tail=None):
        self.script = list(script)
        self.tail = tail
        self.session = {"serial": "stub:0", "ip": "0.0.0.0", "port": 0}

    def read_battery(self):
        if self.script:
            item = self.script.pop(0)
            return (None, False) if item is None else (item, True)
        if self.tail is not None:
            return self.tail, True
        return None, False


def _r(level=None, temp=30.0, ac=True, status=None):
    return {"level": level, "temperature_c": temp, "ac_powered": ac,
            "status": status if status is not None else (2 if ac else 3),
            "voltage_mv": 3900, "charge_counter_uah": 2400000, "raw_ok": True}


def self_test() -> int:
    """桩数据自测：不需要真机、不需要插座。穷举边界与故障分支。"""
    pin_utf8()
    passed = failed = 0

    def check(name: str, got, want) -> None:
        nonlocal passed, failed
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<46} got={got!r} want={want!r}")
        if ok:
            passed += 1
        else:
            failed += 1

    base = json.loads(json.dumps(DEFAULTS))
    base["phone"]["ip"] = "0.0.0.0"
    base["alert"]["type"] = "none"

    def new_guard(script, plug=None, temp_f=None, dry=False, tail=None,
                  readback=2.0, poll=0.05):
        cfg = json.loads(json.dumps(base))
        cfg["plug"]["readback_timeout_sec"] = readback
        cfg["loop"]["readback_poll_sec"] = poll
        log = CsvLog(os.devnull, 30)
        al = Alerter(cfg["alert"])
        clock = temp_f or time.time
        return Guard(cfg, FakeAdb(script, tail=tail), plug or FakePlug(), log, al,
                     dry_run=dry, clock=clock)

    print("\n== 解析：dumpsys battery 真实输出（2026-09-25 真机抓取，已改数值） ==")
    real = ("Current Battery Service state:\n  AC powered: false\n  USB powered: false\n"
            "  Max charging current: 0\n  Charge counter: 2483000\n  status: 3\n  health: 2\n"
            "  present: true\n  level: 67\n  scale: 100\n  voltage: 3964\n  temperature: 337\n"
            "  technology: Li-poly\n")
    got = AdbLink.parse_battery(real)
    check("level", got["level"], 67)
    check("temperature 337 -> 33.7C（0.1℃ 单位）", got["temperature_c"], 33.7)
    check("status=3 放电", got["status"], 3)
    check("AC powered=false（这是断电回读的判据）", got["ac_powered"], False)
    check("voltage", got["voltage_mv"], 3964)
    check("Charge counter", got["charge_counter_uah"], 2483000)

    print("\n== 解析：空串 / 缺 level 必须判未知，不得当 0 或当满 ==")
    check("空串 raw_ok", AdbLink.parse_battery("")["raw_ok"], False)
    check("空串 level", AdbLink.parse_battery("")["level"], None)
    check("无 level 字段 level", AdbLink.parse_battery("Current Battery state:\n  status: 2\n")["level"], None)
    check("level 越界(101) 判未知", AdbLink.parse_battery("  level: 101\n  scale: 100\n")["level"], None)
    check("level 前后有杂字符不匹配", AdbLink.parse_battery("  level: 6a5\n")["level"], None)

    print("\n== 决策：65/50 滞回 + 稳定计数 ==")
    g = new_guard([_r(60), _r(66), _r(66)], tail=_r(66, ac=False))
    check("60% 落在区间内 -> hold", g.tick(), "hold")
    check("66% 第 1 次 -> 待稳定", g.tick(), "pending")
    check("66% 第 2 次 -> 断电", g.tick(), "off")
    check("插座收到的是断电", g.plug.calls, [False])

    print("\n== 决策：回到下沿才通电 ==")
    g = new_guard([_r(52), _r(49), _r(49)], tail=_r(49, ac=True))
    check("52% 仍在下沿之上 -> hold", g.tick(), "hold")
    check("49% 第 1 次 -> 待稳定", g.tick(), "pending")
    check("49% 第 2 次 -> 通电", g.tick(), "on")
    check("插座收到的是通电", g.plug.calls, [True])

    print("\n== 决策：整数抖动不会卡死（stop_at+3 立即断电） ==")
    g = new_guard([_r(65), _r(68)], tail=_r(68, ac=False))
    check("65% 第 1 次 -> 待稳定（未越界）", g.tick(), "pending")
    check("68% >= 65+3 -> 立即断电", g.tick(), "off")

    g = new_guard([_r(48)], tail=_r(48, ac=True))
    check("48% 未越界 -> 待稳定", g.tick(), "pending")
    g = new_guard([_r(46)], tail=_r(46, ac=True))
    check("46% <= 50-3 -> 立即通电（不等稳定）", g.tick(), "on")

    print("\n== 决策：温度安全项立即生效，不做稳定过滤 ==")
    g = new_guard([_r(70, temp=45.0)], tail=_r(70, temp=45.0, ac=False))
    check("45.0℃ 首次即断电", g.tick(), "off")
    check("温度锁存已置位", g.state["temp_tripped"], True)
    g2 = new_guard([_r(70, temp=42.0)])
    g2.state["temp_tripped"] = True
    check("42℃ 在 40-45 之间 -> 保持断电", decide(DEFAULTS["policy"], _r(70, temp=42.0), g2.state), ("off", "temp-hold 42.0C > 40.0C"))
    g3 = new_guard([_r(55, temp=39.0)])
    g3.state["temp_tripped"] = True
    apply_temp_latch(DEFAULTS["policy"], _r(55, temp=39.0), g3.state)
    check("39℃ 解除锁存", g3.state["temp_tripped"], False)

    print("\n== 决策：读数未知绝不动作 ==")
    check("level=None -> hold", decide(DEFAULTS["policy"], {"level": None, "temperature_c": 30.0}, {}),
          ("hold", "unknown-reading(level missing)"))
    g = new_guard([None, None])
    check("失联第 1 轮", g.tick(), "lost")
    check("失联第 2 轮", g.tick(), "lost")
    check("失联期间没碰过插座", g.plug.calls, [])

    print("\n== 回归：电量单调变化时稳定门不得卡死（真机 dry-run 暴露） ==")
    g = new_guard([_r(67), _r(66)], tail=_r(66, ac=False))
    check("67% -> 待连续同判定", g.tick(), "pending")
    check("66%（与上次不同値）仍应断电", g.tick(), "off")
    check("插座收到断电", g.plug.calls, [False])
    g = new_guard([_r(52), _r(50), _r(49)], tail=_r(49, ac=True))
    check("52% 区间内 -> hold", g.tick(), "hold")
    check("50% 触下沿 -> 待连续同判定", g.tick(), "pending")
    check("49%（仍在下降）第 2 次同判定 -> 通电", g.tick(), "on")
    check("插座收到通电", g.plug.calls, [True])

    print("\n== 6. 执行器回读：AC powered 是「断电到底生效没」的判据 ==")
    g = new_guard([_r(70)], tail=_r(70, ac=False))
    check("越界立即断电", g.tick(), "off")
    check("回读到 AC powered=false → 确认生效", g.plug_on, False)

    g = new_guard([_r(70)], tail=_r(70, ac=True))
    check("回读一直不匹配仍下达断电", g.tick(), "off")
    check("不冒认成功（plug_on 保持未知）", g.plug_on, None)

    print("\n== 故障：插座指令失败 → 记 error，不改内部状态 ==")
    g = new_guard([_r(70), _r(70)], plug=FakePlug(fail=True))
    g.tick()
    check("插座失败时返回 off（判定已下达）", g.tick(), "off")
    check("失败不被当作成功（plug_on 仍未知）", g.plug_on, None)

    print("\n== 故障：失联分级（注入时钟） ==")
    class Clock:
        def __init__(self): self.t = 1000.0
        def __call__(self): return self.t
    ck = Clock()
    g = new_guard([_r(67), None, None, None], temp_f=ck)
    g.tick()                      # 建立 last_seen 与 last_reading(67%)
    ck.t += 120                   # 失联 2min < short_break
    check("失联 2min -> 保持原状", g.tick(), "lost")
    check("短断不动作", g.plug.calls, [])
    ck.t += 1900                  # 累计失联 > 30min
    check("失联 >30min 且最后读数 67>=65 -> 断电", g.tick(), "lost")
    check("已发断电指令", g.plug.calls, [False])

    ck2 = Clock()
    g = new_guard([_r(48), None, None], temp_f=ck2)
    g.tick()
    ck2.t += 1900
    g.tick()
    check("失联 >30min 且读数 48<65 -> 保持通电", g.plug.calls, [])
    ck2.t += 13 * 3600            # 超过 12h 上限
    g.tick()
    check("失联超 12h 上限 -> 兜底断电", g.plug.calls, [False])

    print("\n== 一次性满充标志 ==")
    flag = Path("full_charge.once")
    try:
        cfg_p = DEFAULTS["policy"]
        g = new_guard([_r(66)])
        flag.write_text("1", encoding="utf-8")
        g.refresh_flag(_r(66))
        check("标志存在 -> 目标提升到 100", g.state["flag_full"], True)
        check("66% 在 100 目标下不应断电", decide(cfg_p, _r(66), g.state)[0], "hold")
        g2 = new_guard([_r(99)])
        g2.refresh_flag(_r(99))
        check("达到 98% 后标志自动清除", g2.state["flag_full"], False)
        check("标志文件已删除", flag.exists(), False)
    finally:
        flag.unlink(missing_ok=True)

    print(f"\n===== 自测结果：{passed} 通过 / {failed} 失败 =====")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- main
def main() -> int:
    pin_utf8()
    ap = argparse.ArgumentParser(description="常驻插电安卓设备的充电区间守护")
    ap.add_argument("--config", default="charge_guard.json")
    ap.add_argument("--dry-run", action="store_true", help="只读数不控电")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--ticks", type=int, default=0, help="跑 N 轮后退出（0=常驻，排错用）")
    ap.add_argument("--self-test", action="store_true", help="桩数据自测，不需要真机与插座")
    ap.add_argument("--probe-plug", action="store_true", help="只读枚举插座属性，定位 siid/piid")
    ap.add_argument("--actuator", choices=["miio", "ble"], help="执行端：miio=智能插座，ble=充电器端口直控")
    ap.add_argument("--probe-charger", action="store_true", help="BLE 执行端：连一次充电器并 dump 全部属性")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    cfg = load_config(args.config)
    if args.actuator:
        cfg["actuator"] = args.actuator

    if args.probe_plug:
        return probe_plug(cfg)

    if args.probe_charger:
        from ble_plug import ChargerBle
        return ChargerBle(cfg["charger_ble"]).probe()

    log = CsvLog(cfg["log"]["csv"], int(cfg["log"]["keep_days"]))
    alerter = Alerter(cfg["alert"])
    adb = AdbLink(cfg, log, alerter)

    if cfg["actuator"] == "ble":
        from ble_plug import ChargerBle
        plug = ChargerBle(cfg["charger_ble"], dry_run=args.dry_run,
                          log=lambda m: print(m, flush=True))
    else:
        plug = PlugLink(cfg["plug"], log, alerter, dry_run=args.dry_run)

    if not plug.configured():
        if cfg["actuator"] == "ble":
            print("⚠️ BLE 执行端未配置（缺 token）——本次将只读数、不控电。\n"
                  "   1) 取 token：cd third_party/xiaomi-ad1204-python && python fetch_tokens.py --region cn\n"
                  "   2) 填进环境变量 CHARGER_BLE_TOKEN（或 charge_guard.json 的 charger_ble.token）\n"
                  "   3) 可用 --probe-charger 先验一遍通路", flush=True)
        else:
            print("⚠️ 插座未配置（plug.ip / token / siid / piid 有空）——本次将只读数、不控电。\n"
                  "   属性探测方法见 README「插座属性探测」。", flush=True)

    guard = Guard(cfg, adb, plug, log, alerter, dry_run=args.dry_run)
    print(f"charge_guard 启动：执行端={cfg['actuator']} ip={cfg['phone']['ip']} "
          f"stop_at={cfg['policy']['stop_at']} resume_at={cfg['policy']['resume_at']} "
          f"间隔={cfg['loop']['interval_sec']}s dry_run={args.dry_run}", flush=True)
    guard.run(ticks=args.ticks or (1 if args.once else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
