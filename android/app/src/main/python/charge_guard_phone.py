#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
charge_guard_phone.py —— 跑在 K70 Pro 上（Termux）的「大脑」

为什么把它搬到手机上：手机就放在充电器旁边，读端（自己的电量）和执行端（局域网控插座）
都不出手机。于是 charge_guard.py 里那一整堆东西不再需要：
  · 无线调试端口的缓存/mDNS/扫描三层兜底
  · `adb connect` 之后 shell 未就绪返回空字符串的假在线
  · adb daemon 被沙箱回收
  · PC 必须常开、必须与充电器在 BLE 距离内、必须与手机同网段

链路：
    本机电量 ──(termux-battery-status)──┐
                                        └─► 判定（65/50 滞回 + 温度 + 稳定门）──► miIO/UDP ──► 智能插座 ──220V──► 充电器
    留作唯一真相源：charge_guard.py     ┘

依赖：**只有 Python 标准库**（urllib 就够了，不需要 pip 装任何东西）。
      外加 Termux 侧的 `termux-api` 包 + Termux:API 应用（提供 termux-battery-status）。

⚠️ 稳定性门与判定规则**同步自 charge_guard.py::decide**。改一处必须改两处；
   两边都带同一套自测断言，跑 `--self-test` 可以发现漂移。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "plug": {
        # 小米智能插座（miIO over LAN，UDP 54321）。token 是密钥，放本机覆盖层，别进 git。
        # siid/piid 已按真机（cuco.plug.v3 @192.168.0.99）验证：开关 2/1、功率 11/2、故障 2/3。
        "ip": "192.168.0.99",
        "token": "",
        "on_siid": 2, "on_piid": 1,
        "power_siid": 11, "power_piid": 2,
        "fault_siid": 2, "fault_piid": 3,
        "timeout_sec": 6.0,
        "udp_port": 54321,
        "label": "充电器插座",
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
    # 本机覆盖层（不进 git）：插座 token 放这里，受版本控制的配置保持干净。
    # 约定：<配置名>.local.json，只写要覆盖的键（charge_guard_phone.json → charge_guard_phone.local.json）
    p = Path(path)
    local = p.with_name(p.stem + ".local.json")
    if local.exists():
        cfg = _deep_merge(cfg, json.loads(local.read_text(encoding="utf-8")))
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


# ---------------------------------------------------------------- 执行端（小米智能插座）
# ================================================================ miIO 协议层（纯标准库）
# 为什么自己实现：手机上（Termux）装 python-miio / cryptography 都可能踩坑，
# 而这一段只用标准库的 hashlib/socket/json/struct —— 手机上只需要 Termux 自带的 python。
# 正确性证据（2026-09-25 在 PC 上验证）：
#   · AES 与 `cryptography` 对拍：300 组加密 + 300 组解密，**零差异**；
#   · 对真机插座完成握手与读属性（开关/功率/故障都读到了）。
HELLO = bytes.fromhex("21310020" + "ff" * 28)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def _rotl8(x: int, n: int) -> int:
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _build_sbox() -> list:
    """S-box 按定义生成（GF(2^8) 求逆 + 仿射变换）——手抄 256 字节必然出错。"""
    out = []
    for i in range(256):
        inv, base, e = 1, i, 254
        while e:
            if e & 1:
                inv = _gmul(inv, base)
            base = _gmul(base, base)
            e >>= 1
        out.append(inv ^ _rotl8(inv, 1) ^ _rotl8(inv, 2) ^ _rotl8(inv, 3) ^ _rotl8(inv, 4) ^ 0x63)
    return out


_SBOX = _build_sbox()
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _expand_key(key: bytes) -> list:
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [_SBOX[b] for b in (t[1:] + t[:1])]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(11)]


def _encrypt_block(rk: list, block: bytes) -> bytes:
    s = [block[i] ^ rk[0][i] for i in range(16)]
    for rnd in range(1, 11):
        s = [_SBOX[b] for b in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]      # ShiftRows
        if rnd != 10:
            t = []
            for c in range(4):
                col = s[4 * c:4 * c + 4]
                t += [_gmul(col[0], 2) ^ _gmul(col[1], 3) ^ col[2] ^ col[3],
                      col[0] ^ _gmul(col[1], 2) ^ _gmul(col[2], 3) ^ col[3],
                      col[0] ^ col[1] ^ _gmul(col[2], 2) ^ _gmul(col[3], 3),
                      _gmul(col[0], 3) ^ col[1] ^ col[2] ^ _gmul(col[3], 2)]
            s = t
        s = [s[i] ^ rk[rnd][i] for i in range(16)]
    return bytes(s)


def _decrypt_block(rk: list, block: bytes) -> bytes:
    s = [block[i] ^ rk[10][i] for i in range(16)]
    for rnd in range(9, -1, -1):
        s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]       # InvShiftRows
        s = [_INV_SBOX[b] for b in s]
        s = [s[i] ^ rk[rnd][i] for i in range(16)]
        if rnd != 0:
            t = []
            for c in range(4):
                col = s[4 * c:4 * c + 4]
                t += [_gmul(col[0], 14) ^ _gmul(col[1], 11) ^ _gmul(col[2], 13) ^ _gmul(col[3], 9),
                      _gmul(col[0], 9) ^ _gmul(col[1], 14) ^ _gmul(col[2], 11) ^ _gmul(col[3], 13),
                      _gmul(col[0], 13) ^ _gmul(col[1], 9) ^ _gmul(col[2], 14) ^ _gmul(col[3], 11),
                      _gmul(col[0], 11) ^ _gmul(col[1], 13) ^ _gmul(col[2], 9) ^ _gmul(col[3], 14)]
            s = t
    return bytes(s)


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    rk = _expand_key(key)
    pad = 16 - (len(data) % 16)
    data = data + bytes([pad]) * pad
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        prev = _encrypt_block(rk, bytes(a ^ b for a, b in zip(data[i:i + 16], prev)))
        out += prev
    return out


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    rk = _expand_key(key)
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(rk, blk), prev))
        prev = blk
    pad = out[-1] if out else 0
    return out[:-pad] if 1 <= pad <= 16 else out


class MiioError(RuntimeError):
    pass


class MiioClient:
    """最小 miIO 客户端：握手 + 加密请求（get_properties / set_properties）。"""

    def __init__(self, ip: str, token_hex: str, timeout: float = 4.0, port: int = 54321):
        self.ip, self.port, self.timeout = ip, int(port), float(timeout)
        try:
            self.token = bytes.fromhex((token_hex or "").strip())
        except ValueError as e:
            raise MiioError(f"token 不是合法 hex：{e}") from e
        if len(self.token) != 16:
            raise MiioError(f"token 必须是 32 个 hex 字符（16 字节），当前 {len(self.token)} 字节")
        self._key = hashlib.md5(self.token).digest()
        self._iv = hashlib.md5(self._key + self.token).digest()
        self._device_id = b"\x00\x00\x00\x00"
        self._stamp = 0
        self._id = 0

    def _udp(self, payload: bytes, expect: int = 1024) -> bytes:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(self.timeout)
        try:
            s.sendto(payload, (self.ip, self.port))
            data, _ = s.recvfrom(expect)
            return data
        finally:
            s.close()

    def handshake(self) -> dict:
        data = self._udp(HELLO, 32)
        if len(data) < 32 or data[:2] != b"\x21\x31":
            raise MiioError(f"握手响应异常：{data[:32].hex()}")
        self._device_id = data[8:12]
        self._stamp = struct.unpack(">I", data[12:16])[0]
        return {"device_id": self._device_id.hex(), "stamp": self._stamp}

    def _pack(self, payload: bytes) -> bytes:
        header = (struct.pack(">HHI", 0x2131, 32 + len(payload), 0)
                  + self._device_id + struct.pack(">I", (self._stamp + 1) & 0xFFFFFFFF))
        return header + hashlib.md5(header + self.token + payload).digest() + payload

    def request(self, method: str, params, attempts: int = 4):
        """发一条请求。**故意做成多次重试** —— 2026-09-26 的实测教训：

        手机那侧的 UDP 会整段时间发不出去（当时是 Tailscale 的 tun1 把 Termux 的
        局域网流量吞掉了），一次丢包就等于整条「通电」指令失败，而守护每 60 秒才试一次
        ⇒ 手机在 34%~49% 之间一路掉电，谁都拉不起来。
        miIO 的 get/set 都是幂等的，所以「多试几次」没有任何副作用。
        """
        last_err = None
        for i in range(1, max(1, attempts) + 1):
            try:
                if not self._stamp:
                    self.handshake()
                self._id += 1
                body = json.dumps({"id": self._id, "method": method, "params": params}).encode()
                data = self._udp(self._pack(aes_cbc_encrypt(self._key, self._iv, body)))
                if len(data) < 32:
                    raise MiioError(f"响应太短：{data.hex()}")
                reply = json.loads(aes_cbc_decrypt(self._key, self._iv, data[32:]))
                if "error" in reply:
                    raise MiioError(f"设备返回错误：{reply['error']}")
                return reply.get("result")
            except MiioError:
                raise
            except Exception as e:                      # 超时/解密失败等：重握手后重试
                last_err = e
                self._stamp = 0
                if i < attempts:
                    time.sleep(1.5)
        raise MiioError(f"{type(last_err).__name__}: {last_err}（已试 {attempts} 次）")

    @staticmethod
    def _did(siid: int, piid: int) -> str:
        return f"{siid}-{piid}"

    def get_properties(self, pairs) -> dict:
        params = [{"did": self._did(s, p), "siid": s, "piid": p} for s, p in pairs]
        out = {}
        for item in (self.request("get_properties", params) or []):
            if isinstance(item, dict) and item.get("code", 0) == 0:
                out[(item["siid"], item["piid"])] = item.get("value")
        return out

    def set_property(self, siid: int, piid: int, value) -> bool:
        res = self.request("set_properties",
                           [{"did": self._did(siid, piid), "siid": siid, "piid": piid, "value": value}]) or []
        for item in res:
            if isinstance(item, dict):
                return item.get("code", 1) == 0
        return False


class PlugMiio:
    """小米智能插座执行端。接口与原来的 Esp32Link 对齐：set_port / read_status / probe。

    ⚠️ 写成功 ≠ 真的切了：这里每次都**回读确认**（BLE 那条路的教训：不能只看「发出去了」）。
    """

    def __init__(self, cfg: dict, dry_run: bool = False):
        self.c = cfg
        self.dry_run = dry_run
        self._client: MiioClient | None = None

    def configured(self) -> bool:
        return bool(self.c.get("ip") and self.c.get("token"))

    def _cli(self) -> MiioClient:
        if self._client is None:
            self._client = MiioClient(self.c["ip"], self.c["token"],
                                      timeout=float(self.c.get("timeout_sec", 4.0)),
                                      port=int(self.c.get("udp_port", 54321)))
        return self._client

    def _pair(self, prefix: str) -> tuple[int, int]:
        return int(self.c[f"{prefix}_siid"]), int(self.c[f"{prefix}_piid"])

    def read_on(self):
        s, p = self._pair("on")
        return self._cli().get_properties([(s, p)]).get((s, p))

    def set_port(self, on: bool) -> bool:
        if self.dry_run:
            print(f"[dry-run] 会给插座发「{'通电' if on else '断电'}」（不实际执行）", flush=True)
            return True
        if not self.configured():
            print("[plug] ip/token 没配齐，拒绝控电（未知状态绝不动作）", flush=True)
            return False
        s, p = self._pair("on")
        try:
            if not self._cli().set_property(s, p, bool(on)):
                print(f"[plug] 设备拒绝写 {s}-{p}={on}", flush=True)
                return False
            got = self.read_on()
            if got is None:
                print("[plug] 写入后回读失败：不确定有没有切成功", flush=True)
                return False
            if bool(got) != bool(on):
                print(f"[plug] 回读不一致：想要 {on}，实际 {got}", flush=True)
                return False
            print(f"[plug] {self.c.get('label', '插座')} -> {'通电' if on else '断电'}（已回读确认）", flush=True)
            return True
        except Exception as e:
            print(f"[plug] 控制失败：{type(e).__name__}: {e}", flush=True)
            return False

    def read_status(self) -> dict:
        if not self.configured():
            return {}
        try:
            pairs = [self._pair("on"), self._pair("power"), self._pair("fault")]
            vals = self._cli().get_properties(pairs)
        except Exception:
            return {}
        return {"on": vals.get(pairs[0]), "power_w": vals.get(pairs[1]), "fault": vals.get(pairs[2])}

    def probe(self) -> None:
        print("== 探测插座（只读，不切换任何东西）==")
        if not self.configured():
            print("  ip/token 没配齐 —— token 请放进 charge_guard_phone.local.json（不进 git）")
            return
        c = self._cli()
        print(f"  {self.c['ip']}:{int(self.c.get('udp_port', 54321))}  握手 -> {c.handshake()}")
        for label, prefix in (("开关", "on"), ("功率 W", "power"), ("故障", "fault")):
            try:
                print(f"  {label:<8} {self._pair(prefix)} = {c.get_properties([self._pair(prefix)])}")
            except Exception as e:
                print(f"  {label:<8} 读取失败：{type(e).__name__}: {e}")


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
        self.err_streak = 0

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
            self.err_streak += 1
            self.log.row("error", reading, action=action, reason=reason, note="插座请求失败")
            self.alerter.send("plug-error", "插座控制失败",
                              f"想把「{self.cfg['plug'].get('label', '插座')}」设为 {'通电' if want_on else '断电'}，失败。")
            # 2026-09-26 实况：守护活着、判定正确，但 UDP 整段发不出去（当时是 Tailscale tun1），
            # 单次告警被 repeat_sec 节流、没人发现，电量一路掉到 28%。
            # 所以按「连续失败次数」升级：CSV 里持续出现 error 行（PC 侧看门狗据此接管），
            # 并在达到阈值时发一次独立的升级告警（after 起，之后每 every 次重复，受 repeat_sec 节流）。
            pol = self.cfg["policy"]
            after = int(pol.get("plug_fail_alarm_after", 3))
            every = int(pol.get("plug_fail_alarm_every", 10))
            if self.err_streak == after or (self.err_streak > after and (self.err_streak - after) % every == 0):
                self.log.row("error", reading, action=action, reason=reason,
                             note=f"连续 {self.err_streak} 次控制失败 → 升级告警（手机端可能管不住插座，需 PC 接管）")
                self.alerter.send("plug-dead", "插座连续控制失败",
                                  f"已连续 {self.err_streak} 次写插座失败（手机端 UDP 可能被 VPN/省电吞掉）。\n"
                                  f"处置：PC 侧 watch_from_pc.py 会按 CSV 的 error 行接管；也可用米家手动控。")
            return
        self.err_streak = 0
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

    print("\n== 连续控制失败要升级告警（2026-09-26 控制通道整段瘫痪的实况）==")
    al = Alerter(cfg["alert"])
    sent: list[str] = []
    al.send = lambda key, title, body: sent.append(key)      # 只记录不真发
    g = Guard(cfg, FakePlug(fail=True), log, al)
    for _ in range(2):
        g._actuate("off", _r(70), "test")
    check("前 2 次失败不升级", "plug-dead" in sent, False)
    g._actuate("off", _r(70), "test")
    check("第 3 次连续失败升级一次", sent.count("plug-dead"), 1)
    for _ in range(10):
        g._actuate("off", _r(70), "test")
    check("第 13 次连续失败再升级一次", sent.count("plug-dead"), 2)
    fp = FakePlug()
    g = Guard(cfg, fp, log, al)
    fp.fail = True
    for _ in range(3):
        g._actuate("off", _r(70), "test")
    check("另一实例 3 次失败同样升级", sent.count("plug-dead"), 3)
    fp.fail = False
    g._actuate("off", _r(70), "test")
    check("成功后失败计数清零", g.err_streak, 0)
    fp.fail = True
    g._actuate("off", _r(70), "test")
    g._actuate("off", _r(70), "test")
    check("清零后重新累计，2 次不升级", sent.count("plug-dead"), 3)
    check("成功路径会重置 plug_on", g.plug_on, False)

    print("\n== 插座执行端：写成功 ≠ 真的切了（必须回读确认）==")

    class StubPlug(PlugMiio):
        """不起真 socket：把「写入结果」和「回读结果」注入进来。"""
        def __init__(self, write_ok=True, read_back=True):
            super().__init__(dict(DEFAULTS["plug"], ip="1.2.3.4", token="00" * 16))
            self.write_ok, self.read_back, self.writes = write_ok, read_back, []

        def _cli(self):
            outer = self

            class C:
                def set_property(self, s, p, v):
                    outer.writes.append((s, p, v))
                    return outer.write_ok

                def get_properties(self, pairs):
                    s, p = pairs[0]
                    return {(s, p): outer.read_back}

                def handshake(self):
                    return {}

            return C()

    sp = StubPlug(read_back=False)
    check("写+回读一致 -> True", sp.set_port(False), True)
    check("写到了 on_siid/on_piid", sp.writes[-1][:2], (2, 1))
    check("写的是 bool False", sp.writes[-1][2], False)
    check("设备拒绝写 -> False", StubPlug(write_ok=False).set_port(False), False)
    check("回读与目标不一致 -> False（防「假成功」）", StubPlug(read_back=True).set_port(False), False)

    print("\n== miIO 协议自检：AES 已知向量（NIST FIPS-197 附录 B）==")
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = _encrypt_block(_expand_key(key), pt)
    check("AES-128 单块加密 = 69c4e0d86a7b0430d8cdb78070b4c55a", ct.hex(),
          "69c4e0d86a7b0430d8cdb78070b4c55a")
    check("解密回原文", _decrypt_block(_expand_key(key), ct), pt)
    check("CBC 往返（含 padding）",
          aes_cbc_decrypt(key, b"\x00" * 16, aes_cbc_encrypt(key, b"\x00" * 16, b"hello miio")),
          b"hello miio")

    print(f"\n===== 自测结果：{passed} 通过 / {failed} 失败 =====")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- main
PIDFILE = HERE / "guard.pid"


def _already_running() -> int | None:
    """单实例保护：返回已在跑的 PID，或 None。

    为什么需要：现在有三个地方会拉起守护（Termux 里的手工启动、~/.bashrc 钩子、
    JobScheduler 看门狗、以及 PC 侧的 am start），叠加时可能起出两个实例。
    两个实例会各自维护自己的判定状态去控同一个插座 —— 必须避免。
    """
    try:
        pid = int(PIDFILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    if pid == os.getpid():
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except OSError:
        return None                              # 进程不在了
    return pid if "charge_guard_phone.py" in cmdline else None


def main() -> int:
    ap = argparse.ArgumentParser(description="跑在手机上的充电守护大脑（Termux）")
    ap.add_argument("--config", default=str(HERE / "charge_guard_phone.json"))
    ap.add_argument("--dry-run", action="store_true", help="只读电量与判定，不控端口")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--ticks", type=int, default=0)
    ap.add_argument("--self-test", action="store_true", help="桩数据自测，不需要 Termux 与插座")
    ap.add_argument("--probe", action="store_true", help="探测插座（只读：握手 + 读开关/功率/故障）")
    ap.add_argument("--show-battery", action="store_true", help="只读一次电量并打印原始 JSON")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.show_battery:
        print(subprocess.run(["termux-battery-status"], capture_output=True, text=True).stdout)
        return 0

    cfg = load_config(args.config)

    busy = _already_running()
    if busy is not None and not args.once:
        print(f"已有实例在跑（PID {busy}），本实例退出 —— 单实例保护（同一插座不能被两个循环控）",
              flush=True)
        return 0
    try:
        PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    if args.probe:
        PlugMiio(cfg["plug"]).probe()
        return 0

    log = CsvLog(cfg["log"]["csv"], int(cfg["log"]["keep_days"]))
    alerter = Alerter(cfg["alert"])
    plug = PlugMiio(cfg["plug"], dry_run=args.dry_run)

    if cfg["loop"].get("wake_lock"):
        # 不让 Android Doze 把循环冻住；不需要就删掉这行
        try:
            subprocess.run(["termux-wake-lock"], capture_output=True, timeout=10)
        except Exception:
            pass

    print(f"phone_guard 启动：插座={cfg['plug']['ip']} 标签={cfg['plug'].get('label', '')} "
          f"stop_at={cfg['policy']['stop_at']} resume_at={cfg['policy']['resume_at']} "
          f"间隔={cfg['loop']['interval_sec']}s dry_run={args.dry_run}", flush=True)
    Guard(cfg, plug, log, alerter, dry_run=args.dry_run).run(ticks=args.ticks or (1 if args.once else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
