# -*- coding: utf-8 -*-
"""android_main 的离线回归测试（纯标准库，不需要 Android 设备 / Chaquopy）。

背景（2026-10-01 停摆事故）：CsvLog.row 写失败会抛异常 → 循环 except 分支里再写一行
同样抛 → 异常逃出 while → Python 循环无声死亡，而前台服务还挂着（status 停在
12:50:26，守护已死却无人知晓）。本测试用假插座 / 假电量源复现该类场景，断言：

  A) tick 持续抛异常时循环不退出（error 行持续增长；STOP 后有 stop 行）；
  B) 主 CSV 持续写失败时 SafeLog 自动降级到备用路径，日志行数继续增长；
  C) start_guard 全程不向外抛异常（Java 监督线程之外的最后一道防线）。

跑法：python -X utf8 scripts/test_android_main_offline.py
"""
from __future__ import annotations

import copy
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "android" / "app" / "src" / "main" / "python"

# android_main 在模块顶层 `from java import jclass`（Chaquopy 提供）——离线环境给个假货。
java_stub = types.ModuleType("java")
java_stub.jclass = lambda name: type("JClass_" + name.replace(".", "_"), (), {})
sys.modules["java"] = java_stub
sys.path.insert(0, str(SRC))

import android_main as am  # noqa: E402
import charge_guard_phone as cg  # noqa: E402

REAL_SLEEP = time.sleep


class AlwaysFailLog:
    """模拟 /sdcard 永久写失败（EIO）。"""

    def __init__(self, base: Path):
        self.path = base / "primary_never_writes.csv"

    def row(self, *args, **kwargs) -> None:
        raise OSError("EIO: simulated /sdcard failure")


class FakePlug:
    def __init__(self, cfg: dict):
        self.c = cfg

    def read_on(self) -> bool:
        return False

    def read_status(self) -> dict:
        return {"on": False, "power_w": 0.0, "fault": 0}


class BoomGuard:
    """tick 每次都炸 —— 复现 2026-10-01「循环被异常杀死」的场景。"""

    def __init__(self, cfg, plug, log, alerter):
        self.cfg, self.plug, self.log = cfg, plug, log
        self.last_reading = None
        self.plug_on = False
        self.err_streak = 0
        self.pending_action = None
        self.pending_count = 0

    def tick(self) -> str:
        raise RuntimeError("boom (simulated tick failure)")


def patch_env(tmp: Path) -> None:
    """把 /sdcard 重定向到临时目录、1s 步进改 10ms、插座/电量源换成假的。"""
    am.SDCARD_DIR = str(tmp / "sdcard")
    (tmp / "sdcard").mkdir(parents=True, exist_ok=True)

    time.sleep = lambda s: REAL_SLEEP(0.01)

    am._make_battery_reader = lambda ctx: (
        lambda: ({"level": 51, "temperature_c": 30.0, "plugged": "UNPLUGGED",
                  "ac_powered": False, "raw_ok": True}, "status=3"))
    cg.PlugMiio = FakePlug
    cg.Guard = BoomGuard


def make_cfg() -> dict:
    cfg = copy.deepcopy(am.BASE_CFG)
    cfg["loop"] = {"interval_sec": 10, "wake_lock": False}
    return cfg


def test_safelog_failover(tmp: Path) -> None:
    fallback = tmp / "fallback.csv"
    log = am.SafeLog(AlwaysFailLog(tmp), str(fallback), keep_days=60)
    for i in range(5):
        log.row("sample", note=f"row {i}")           # 不许外抛（断言 B）
    text = fallback.read_text(encoding="utf-8").splitlines()
    data = [line for line in text if line and not line.startswith("ts,")]
    assert len(data) == 2, f"降级后备用 CSV 应有 2 行，实际 {len(data)}：{text}"
    assert log.path == str(fallback), "降级后 path 应指向备用文件"
    print("    ✓ B：主路径连续写失败 3 次后降级备用 CSV，行不丢、异常不外抛")


def test_loop_survives_raising_ticks(tmp: Path) -> None:
    am._load_cfg = lambda app_dir: make_cfg()
    csv_path = tmp / "guard_log.csv"                 # v2.1 起 CSV 固定私有目录
    sdcard_csv = tmp / "sdcard" / "phone_guard_log.csv"
    am.STOP["flag"] = False

    done = threading.Event()

    def run():
        try:
            am.start_guard(str(tmp), context=None)   # 不许向外抛（断言 C）
        finally:
            done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()

    deadline = time.time() + 15
    errors = 0
    while time.time() < deadline:
        if csv_path.exists():
            errors = sum(1 for line in csv_path.read_text(encoding="utf-8").splitlines()
                         if ",error," in line)
            if errors >= 3:
                break
        REAL_SLEEP(0.05)
    assert errors >= 3, f"循环疑似死亡：只观察到 {errors} 条 error 行"

    am.stop_guard()
    assert done.wait(10), "stop_guard 后循环 10s 内没退出"
    rows = csv_path.read_text(encoding="utf-8").splitlines()
    assert any(",stop," in line for line in rows), "缺 stop 行"
    assert (tmp / "status.json").exists(), "status.json 没产出"
    assert not sdcard_csv.exists(), "循环线程不应再写 /sdcard CSV（v2.1 硬规则）"
    print(f"    ✓ A/C：tick 连炸 {errors} 次循环不退、stop 行正常、status.json 已产出、无外抛")


def test_mirror_thread_copies_status(tmp: Path) -> None:
    """v2.1：循环线程只写私有目录；/sdcard 只由镜像线程搬 status.json（尽力而为）。"""
    am._load_cfg = lambda app_dir: make_cfg()
    csv_private = tmp / "guard_log.csv"
    mirror = tmp / "sdcard" / "apk_status.json"
    am.STOP["flag"] = False

    done = threading.Event()

    def run():
        try:
            am.start_guard(str(tmp), context=None)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()

    deadline = time.time() + 15
    while time.time() < deadline and not mirror.exists():
        REAL_SLEEP(0.05)
    assert mirror.exists(), "镜像线程没把 status.json 搬到 /sdcard/apk_status.json"
    assert csv_private.exists(), "私有 CSV 没产出"
    assert not (tmp / "sdcard" / "phone_guard_log.csv").exists(), "循环线程不应写 /sdcard CSV"
    am.stop_guard()
    assert done.wait(10), "stop_guard 后没退出"
    print("    ✓ D：私有 CSV 直写 + 镜像线程搬运 status + 循环线程零 /sdcard I/O")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name, fn in (("b", test_safelog_failover),
                         ("a", test_loop_survives_raising_ticks),
                         ("d", test_mirror_thread_copies_status)):
            tmp = root / name            # 每个测试独立目录，别互相踩 CSV
            tmp.mkdir(parents=True, exist_ok=True)
            patch_env(tmp)
            fn(tmp)
    print("== 全部通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
