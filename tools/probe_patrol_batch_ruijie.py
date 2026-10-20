#!/usr/bin/env python
"""巡检 / 批量下发 在锐捷(串口)上的功能验证 —— 用假连接器复演真机行为。

为什么用假连接器：这台机器现在只有 COM5 一台设备，而且串口被 GUI 独占；
用 stub 复演"设备怎么回显"就能把产品侧的逻辑（厂商识别、关分屏、命令循环、
提示符判定、危险命令拦截、结果落盘）完整跑通，且不占用串口、不改设备。

覆盖：
  P0 厂商识别 guess_vendor（锐捷 → ruijie）
  P1 巡检 patrol_device（protocol=Serial）：关分屏 + 3 条命令全部拿回
  P2 disable_paging 对 ruijie 用的命令是不是 terminal length 0
  P3 批量下发 BatchRunner：顺序、状态、结果
  P4 巡检的危险命令拦截（reload 必须 blocked）

用法：.venv-train\\Scripts\\python.exe tools\\probe_patrol_batch_ruijie.py
退出码 0 = 全过。
"""
from __future__ import annotations

import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.batch as batch                     # noqa: E402
import core.patrol as patrol                   # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

PROMPT = "Ruijie#"

#: 复演真机回显（真机上这几条命令的输出形态就是这样的）
BODIES = {
    "terminal length 0": "",
    "terminal length 24": "",
    "show version": ("System description      : Ruijie Full Gigabit Security & Intelligence "
                     "Access Switch (S2928G-E)\nSystem uptime           : 24:21:26:3\n"
                     "System software version : RGOS 10.4(2b12) Release(154722)"),
    "show ip interface brief": ("Interface                        IP-Address(Pri)      OK?       Status\n"
                                "VLAN 1                           172.18.19.1          YES       UP"),
    "show vlan": ("VLAN Name                             Status    Ports\n"
                  "---- -------------------------------- --------- -----\n"
                  "   1 VLAN0001                         STATIC    Gi0/1, Gi0/2"),
}


class FakeSerial:
    """只实现 CommandWaiter / disable_paging / BatchRunner 用到的那几个方法。"""

    def __init__(self) -> None:
        self._listeners: list = []
        self.sent: list[str] = []
        self.raw: list[str] = []
        self.connected = True

    # --- 连接器接口 ---
    def add_output_listener(self, cb) -> None:
        self._listeners.append(cb)

    def remove_output_listener(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def is_connected(self) -> bool:
        return self.connected

    def _emit(self, text: str) -> None:
        for cb in list(self._listeners):
            cb(text)

    def send(self, command: str) -> None:
        self.sent.append(command)
        body = BODIES.get(command.strip(), "")
        self._emit(f"{command}\r\n{body}\r\n{PROMPT}")

    def send_raw(self, data: str) -> None:
        self.raw.append(data)
        if data == " ":
            self._emit(" ")


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("  " + detail if detail else ""))


# ---------------------------------------------------------------- P0
def p0_vendor() -> None:
    print("--- P0 厂商识别 ---")
    v1 = patrol.guess_vendor({"name": "锐捷S2928G-E", "protocol": "Serial"},
                             "Ruijie Full Gigabit Security & Intelligence Access Switch (S2928G-E)")
    record("锐捷设备 → ruijie", v1 == "ruijie", f"guess={v1}")
    v2 = patrol.guess_vendor({"name": "Console交换机", "protocol": "Serial"})
    record("未识别时退回 unknown（不影响后续候选命令）", v2 in ("unknown", "ruijie"), f"guess={v2}")


# ---------------------------------------------------------------- P1/P2
def p1_patrol() -> None:
    print("--- P1 巡检（Serial）+ P2 关分屏 ---")
    fake = FakeSerial()
    orig_build, orig_connect = patrol.build_connector, patrol.connect_device
    patrol.build_connector = lambda cfg: (fake, "Serial")           # noqa: E731
    patrol.connect_device = lambda conn, proto, cfg, timeout=10.0: (True, "")
    try:
        notes: list[str] = []
        res = patrol.patrol_device(
            {"name": "锐捷S2928G-E", "protocol": "Serial", "serial_port": "COM5", "baudrate": 9600},
            ["show version", "show ip interface brief", "show vlan"],
            timeout=5.0, prompt_timeout=5.0, quiet_time=0.25, silence_timeout=1.5,
            on_note=notes.append)
        cmds = res.get("commands") or []
        statuses = [c.get("status") for c in cmds]
        record("巡检跑完 3 条命令", len(cmds) == 3 and all(s == "prompt" for s in statuses),
               f"状态={statuses} 厂商判定={res.get('paging', '')!r}")
        record("关分屏命令 = terminal length 0", res.get("paging") == "terminal length 0",
               f"paging={res.get('paging')!r} 实际下发={fake.sent[:2]}")
        record("命令输出真的落进结果", any("RGOS" in (c.get("output") or "") for c in cmds),
               f"第一条含 RGOS={'RGOS' in (cmds[0].get('output') or '') if cmds else False}")
        record("过程信息有上报（连上/关分屏）", any("分屏" in n for n in notes), f"notes={notes[:2]}")
        record("巡检结果整体 ok", bool(res.get("ok")), f"ok={res.get('ok')} err={res.get('error')!r}")

        # P2：单独再验一次 disable_paging 的返回值
        fake2 = FakeSerial()
        waiter = batch.CommandWaiter(fake2, quiet_time=0.25, timeout=5.0)
        waiter.start()
        off_ok, used, _text = batch.disable_paging(fake2, waiter, vendor="ruijie",
                                                   quiet_time=0.25, timeout=5.0)
        waiter.stop()
        record("disable_paging(ruijie) 生效", off_ok and used == "terminal length 0",
               f"ok={off_ok} used={used!r}")
    finally:
        patrol.build_connector, patrol.connect_device = orig_build, orig_connect


# ---------------------------------------------------------------- P3
def p3_batch() -> None:
    print("--- P3 批量下发 ---")
    fake = FakeSerial()
    runner = batch.BatchRunner(fake, ["show version", "show vlan", "show ip interface brief"],
                               delay_ms=50)
    ok_start = runner.start()
    deadline = time.time() + 30
    while runner.running and time.time() < deadline:
        time.sleep(0.05)
    results = runner.get_results()
    sent_ok = fake.sent == ["show version", "show vlan", "show ip interface brief"]
    record("批量下发 3 条按顺序发完", ok_start and len(results) == 3 and sent_ok,
           f"start={ok_start} 结果={len(results)} 顺序={sent_ok} 实发={fake.sent}")
    record("批量下发每条都有输出", all((r.get("output") or "").strip() for r in results),
           f"首条输出长度={len(results[0].get('output') or '') if results else 0}")


# ---------------------------------------------------------------- P4
def p4_dangerous() -> None:
    print("--- P4 危险命令拦截 ---")
    fake = FakeSerial()
    orig_build, orig_connect = patrol.build_connector, patrol.connect_device
    patrol.build_connector = lambda cfg: (fake, "Serial")           # noqa: E731
    patrol.connect_device = lambda conn, proto, cfg, timeout=10.0: (True, "")
    try:
        res = patrol.patrol_device(
            {"name": "锐捷S2928G-E", "protocol": "Serial", "serial_port": "COM5"},
            ["reload", "show version"], timeout=5.0, prompt_timeout=5.0,
            quiet_time=0.25, block_dangerous=True)
        cmds = res.get("commands") or []
        blocked = [c for c in cmds if c.get("status") == "blocked"]
        record("reload 被拦截且未下发到设备",
               len(blocked) == 1 and "reload" not in fake.sent,
               f"blocked={len(blocked)} 实发={fake.sent}")
    finally:
        patrol.build_connector, patrol.connect_device = orig_build, orig_connect


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for step in (p0_vendor, p1_patrol, p3_batch, p4_dangerous):
        try:
            step()
        except Exception:                            # noqa: BLE001
            traceback.print_exc()
            record(f"{step.__name__} 未跑通", False, "异常见上")
    ok_n = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n结论：{ok_n}/{len(RESULTS)} 通过")
    return 0 if ok_n == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
