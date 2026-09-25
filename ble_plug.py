#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ble_plug.py —— 把「酷态科10号超级电能充 Ultra」当智能插座用的执行端（走 BLE 直控 C1 口）

为什么不买硬件也能做：这台充电器本身就能按口开关，而且已经有开源实现把它的
BLE 协议（mible v2 + spec-v2）实现了。所以不需要智能插座、也不需要 ESP32，
PC 用自带的蓝牙适配器直接连它就行。

协议要点（来自两个开源实现，已交叉确认）：
  · 端口开关 = MIoT 属性 (siid=2, piid=16)，值是一个 4 位掩码
      c1 = bit0, c2 = bit1, c3 = bit2, a = bit3     （全开 = 0x0F，全关 = 0x00）
      依据：cuktech-ble-ha/ble_server/state.py  PORT_BITS = {"c1":0,"c2":1,"c3":2,"a":3}
            ble_manager.py  ctrl.send_miot_command(2, 16, value=new_val)
  · 登录需要设备 token（12 字节 hex = 24 字符），从小米云端取
  · 充电器**一次只接受一个 BLE 连接** ⇒ 用它的时候手机上的米家 App 必须关掉

本模块不自己实现协议，而是调用 vendored 的实现
（third_party/xiaomi-ad1204-python/ad1204_ble.py，MIT），
因为它已经过真机验证，重复实现只会引入不一致。

⚠️ 每次调用都会「连接 → 认证 → 读写 → 断开」，实测每次几秒到十几秒；
   这是 BLE 单连接的固有代价，不是实现问题。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# 端口 → 位（来自 cuktech-ble-ha/ble_server/state.py）
PORT_BITS = {"c1": 0, "c2": 1, "c3": 2, "a": 3}
ALL_ON = 0x0F
ALL_OFF = 0x00

# pretty() 的输出形如： "  siid  2 piid 16  misc-u8                                     = 15"
LINE_RE = re.compile(r"^\s*siid\s+(\d+)\s+piid\s+(\d+)\s+.*?=\s*(.+?)\s*$")


class ChargerBleError(RuntimeError):
    pass


class ChargerBle:
    """接口对齐 charge_guard.py 里的 PlugLink：configured() / set_power() / read_power_w()。"""

    def __init__(self, cfg: dict, dry_run: bool = False, log=print):
        self.c = cfg
        self.dry_run = dry_run
        self.log = log
        # token 优先取环境变量，避免写进 git
        self.token = (os.environ.get(cfg.get("token_env") or "CHARGER_BLE_TOKEN")
                      or cfg.get("token") or "").strip()
        self.address = (cfg.get("address") or "").strip()
        self.port = (cfg.get("port") or "c1").lower()
        self.script = self._resolve_script(cfg.get("script"))
        self.python = cfg.get("python_exe") or "python"

    @staticmethod
    def _resolve_script(p) -> Path:
        if not p:
            raise ChargerBleError("charger_ble.script 未配置")
        path = Path(p)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.exists():
            raise ChargerBleError(
                f"找不到 {path}\n"
                f"先克隆：git clone https://github.com/ohaiibuzzle/xiaomi-ad1204-python.git "
                f"third_party/xiaomi-ad1204-python")
        return path

    # ---- 基本状态
    def configured(self) -> bool:
        return bool(self.token)

    def describe(self) -> str:
        if not self.token:
            return "未配置（缺 token）"
        return f"token=已设置({len(self.token)} 字符) address={self.address or '未设置（靠扫描）'} port={self.port}"

    def _check(self) -> None:
        if not self.token:
            raise ChargerBleError(
                "缺 token。先从小米云端取（在 third_party/xiaomi-ad1204-python 下）：\n"
                "  python fetch_tokens.py --region cn\n"
                "然后设进环境变量 CHARGER_BLE_TOKEN（推荐）或 charge_guard.json 的 charger_ble.token")
        if len(self.token.replace(" ", "")) != 24:
            raise ChargerBleError(f"token 必须是 12 字节（24 个 hex 字符），现在是 {len(self.token)} 字符")

    # ---- 调 vendored 实现
    def _run(self, sets: list[str] | None = None, watch: int = 0, use_address: bool = True) -> str:
        self._check()
        cmd = [self.python, "-X", "utf8", str(self.script), "--token", self.token, "--quiet"]
        if use_address and self.address:
            cmd += ["--address", self.address]
        if watch:
            cmd += ["--watch", str(watch)]
        for s in sets or []:
            cmd += ["--set", s]
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=float(self.c.get("timeout_sec", 90)),
                               cwd=str(self.script.parent), env=env)
        except subprocess.TimeoutExpired:
            raise ChargerBleError(f"调用超时（{self.c.get('timeout_sec', 90)}s）——充电器可能在息屏不广播，或米家 App 占着连接")
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        if p.returncode != 0:
            raise ChargerBleError(f"脚本返回 {p.returncode}：{out.strip()[-400:]}")
        return out

    def _run_resilient(self, sets: list[str] | None = None, watch: int = 0) -> str:
        """先用配置里的 address 直连；连不上就退化成「让实现自己扫描找它」。

        为什么需要：这台充电器**息屏会停播**，而且云端记录的地址未必一直有效
        （实测同一地址先能连上、几分钟后变成 not found）。写死地址会让执行端很脆；
        vendored 实现本身支持不带 --address 时按 Mi 服务 UUID(FE95) 扫描再连。
        """
        try:
            return self._run(sets=sets, watch=watch, use_address=True)
        except ChargerBleError as e:
            if not (self.address and "not found" in str(e).lower()):
                raise
            self.log(f"[charger] 用配置地址 {self.address} 连不上，改为扫描查找 …")
            return self._run(sets=sets, watch=watch, use_address=False)

    @staticmethod
    def parse_props(text: str) -> dict[tuple[int, int], object]:
        props: dict[tuple[int, int], object] = {}
        for line in text.splitlines():
            m = LINE_RE.match(line)
            if not m:
                continue
            siid, piid, raw = int(m.group(1)), int(m.group(2)), m.group(3)
            try:
                props[(siid, piid)] = int(raw)
            except ValueError:
                props[(siid, piid)] = raw
        return props

    # ---- 读
    def read_state(self) -> dict:
        """返回 {"mask": int, "props": {...}}。mask 是 siid2.piid16 的位掩码。"""
        text = self._run()
        props = self.parse_props(text)
        mask = props.get((2, 16))
        if not isinstance(mask, int):
            raise ChargerBleError(
                "没能从输出里解析出 siid=2 piid=16（端口位掩码）。\n"
                "可能原因：充电器没广播 / 米家 App 占着连接 / token 过期。\n"
                f"原始输出片段：\n{text.strip()[-600:]}")
        return {"mask": mask, "props": props}

    def read_power_w(self):
        """四个口的功率之和（piid 9..12，单位按实现的猜测是 W；拿不到就返回 None）。"""
        try:
            props = self.read_state()["props"]
        except Exception:
            return None
        vals = [props.get((2, p)) for p in (9, 10, 11, 12)]
        nums = [v for v in vals if isinstance(v, int)]
        return round(sum(nums), 1) if nums else None

    # ---- 写
    def set_power(self, on: bool) -> bool:
        """把目标口设为通/断，并在同一次连接里回读确认。"""
        bit = PORT_BITS.get(self.port)
        if bit is None:
            raise ChargerBleError(f"未知端口 {self.port!r}，可选 {list(PORT_BITS)}")

        if self.dry_run:
            self.log(f"[dry-run] 会请求充电器：{self.port} -> {'ON' if on else 'OFF'}（不实际执行）")
            return True

        # 先读当前掩码，避免动到其他口
        cur = self.read_state()["mask"]
        new = (cur | (1 << bit)) if on else (cur & ~(1 << bit))
        if new == cur:
            self.log(f"[charger] {self.port} 已是 {'ON' if on else 'OFF'}（mask=0x{cur:02X}），无需下发")
            return True

        # 一条命令里完成「写」+「回读全部属性」，省一次 BLE 连接
        text = self._run(sets=[f"2-16={new}"])
        props = self.parse_props(text)
        got = props.get((2, 16))
        ok = (got == new)
        self.log(f"[charger] {self.port} -> {'ON' if on else 'OFF'}：mask 0x{cur:02X} -> 0x{new:02X}，"
                 f"回读 {'0x%02X' % got if isinstance(got, int) else got}  {'✅' if ok else '❌ 未确认'}")
        return ok

    # ---- 排错用
    def probe(self) -> int:
        """打印一次完整属性 dump，用来人工确认协议/端口/信号都通。

        同时逐行写进 charger_probe_report.txt（仓库根，已 gitignore）：
        双击运行时窗口内容会被卷走，报告留在盘上，排错不用再抄一遍。
        """
        report = REPO_ROOT / "charger_probe_report.txt"
        try:
            report.unlink()
        except OSError:
            pass

        def out(s: str = "") -> None:
            print(s, flush=True)
            try:
                with report.open("a", encoding="utf-8") as f:
                    f.write(s + "\n")
                    f.flush()
            except OSError:
                pass

        out(f"[charger] {self.describe()}")
        try:
            text = self._run(watch=int(self.c.get("probe_watch_sec", 5)))
        except Exception as e:
            out(f"[charger] 探测失败：{e}")
            out(f"[charger] 报告：{report}")
            return 1
        for ln in text.splitlines():
            out(ln)
        props = self.parse_props(text)
        mask = props.get((2, 16))
        if isinstance(mask, int):
            on = [k for k, b in PORT_BITS.items() if mask & (1 << b)]
            out(f"\n解析结果：端口掩码 = 0x{mask:02X}，当前开着的口 = {on or '无'}")
            out(f"[charger] 报告：{report}")
            return 0
        out("\n⚠️ 没解析到端口掩码（siid=2 piid=16）")
        out(f"[charger] 报告：{report}")
        return 1
