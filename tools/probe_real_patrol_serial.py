#!/usr/bin/env python
"""真机巡检：对 COM5 锐捷 S2928G-E 跑一次 patrol_device（串口）。

串口被 GUI 独占，所以流程是：控制口 close(con1) 释放串口 → 直接跑巡检 → 重新 open(con1)。
跑完会把会话恢复成"已连接 + enable"，用户那边的页签继续可用。

用法：.venv-train\\Scripts\\python.exe tools\\probe_real_patrol_serial.py
退出码 0 = 巡检通过。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.patrol import patrol_device          # noqa: E402

CTRL = os.path.join(os.path.expanduser("~"), ".aiterminal", "control.json")
KEY = "con1"
PORT = "COM5"
DEVICE = {"name": "锐捷S2928G-E", "protocol": "Serial", "serial_port": PORT, "baudrate": 9600}
COMMANDS = ["show version", "show ip interface brief", "show vlan",
            "show mac-address-table", "show spanning-tree summary"]


def cfg() -> dict:
    with open(CTRL, encoding="utf-8") as fh:
        return json.load(fh)


def post(path: str, payload: dict) -> dict:
    c = cfg()
    req = urllib.request.Request(
        c["url"].rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + c["token"]},
    )
    with urllib.request.urlopen(req, timeout=130) as resp:
        return json.loads(resp.read().decode("utf-8"))


def restore() -> None:
    """把 GUI 那个会话重新拉起来（等于用户重新点连接）"""
    try:
        post("/session/open", {"protocol": "serial", "serial_port": PORT,
                               "baudrate": 9600, "key": KEY, "name": "Console交换机"})
        time.sleep(2)
        post("/session/send", {"key": KEY, "command": "enable",
                               "timeout_ms": 15000, "quiet_ms": 400})
        print(f"  已恢复会话 {KEY}（COM5）")
    except Exception as exc:                     # noqa: BLE001
        print(f"  恢复会话失败：{exc}（请手动重连）")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"控制口 {cfg()['url']}  pid={cfg()['pid']}")
    print(f"1) 释放串口 {PORT}（关闭 {KEY} 会话）")
    try:
        post("/session/close", {"key": KEY})
    except Exception as exc:                     # noqa: BLE001
        print(f"   关闭异常：{exc}")
    time.sleep(3)

    print(f"2) 真机巡检：{len(COMMANDS)} 条命令（9600 波特，慢）")
    notes: list[str] = []
    t0 = time.time()
    res = patrol_device(DEVICE, COMMANDS, timeout=60.0, prompt_timeout=45.0,
                        quiet_time=0.4, silence_timeout=2.5, on_note=notes.append)
    wall = time.time() - t0

    print(f"3) 结果：ok={res.get('ok')} 协议={res.get('protocol')} "
          f"关分屏={res.get('paging')!r} 墙钟={wall:.1f}s")
    print(f"   过程信息：{notes}")
    all_ok = True
    for i, c in enumerate(res.get("commands") or [], start=1):
        out_len = len((c.get("output") or "").strip())
        print(f"   [{i}] {c.get('command'):<32} status={c.get('status'):<8} "
              f"{c.get('elapsed', 0):.2f}s {out_len} 字符")
        if c.get("status") != "prompt" or out_len == 0:
            all_ok = False
    if res.get("error"):
        print(f"   error={res['error']!r}")
        all_ok = False
    if res.get("paging") != "terminal length 0":
        all_ok = False

    print("4) 恢复 GUI 会话")
    restore()

    ok = bool(res.get("ok")) and all_ok
    print(f"\n结论：{'通过' if ok else '未通过'}（{wall:.1f}s，{len(res.get('commands') or [])} 条命令）")
    out = os.path.join(os.environ.get("TEMP", "."), "ait_real_patrol_serial.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"result": res, "wall": wall, "notes": notes}, fh, ensure_ascii=False, indent=1)
    print(f"明细：{out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
