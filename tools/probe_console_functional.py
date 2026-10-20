#!/usr/bin/env python
"""console 真机功能测试 —— 只有 COM5 锐捷 S2928G-E 可用时的回归轮。

走应用自己的控制口（control.json + /session/*），等价于用户在该会话上操作：
  T1 命令回显完整性（不重字、不吞字、提示符回得来）
  T2 分页与大输出（terminal length 0 + show running-config 全量落地）
  T3 连续命令压测（15 条连发，全部有应答、无超时）
  T4 断线重连（close → open → 重新 show version）
  T5 会话日志正确性（有内容、含命令、无 \\x00 残留）

用法：.venv-train\\Scripts\\python.exe tools\\probe_console_functional.py [--key con1]
退出码 0 = 全过。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

CTRL = Path(os.path.expanduser("~")) / ".aiterminal" / "control.json"
LOGS = Path(os.path.expanduser("~")) / ".aiterminal" / "logs"
KEY = "con1"

PROMPT_RE = re.compile(r"Ruijie\s*[#>]\s*$", re.MULTILINE)
TIME_RE = re.compile(r"\d{1,2}:\d{2}:\d{2}")

RESULTS: list[tuple[str, bool, str]] = []


def cfg() -> dict:
    return json.loads(CTRL.read_text(encoding="utf-8"))


def post(path: str, payload: dict, timeout: float = 130.0) -> dict:
    c = cfg()
    req = urllib.request.Request(
        c["url"].rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + c["token"]},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send(command: str, timeout_ms: int = 30000, quiet_ms: int = 500,
         max_chars: int = 400000) -> dict:
    t0 = time.monotonic()
    try:
        r = post("/session/send", {"key": KEY, "command": command,
                                   "timeout_ms": timeout_ms,
                                   "quiet_ms": quiet_ms,
                                   "max_chars": max_chars})
    except Exception as exc:                      # noqa: BLE001
        return {"ok": False, "output": "", "error": str(exc),
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    r["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return r


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("  " + detail if detail else ""))


def t1_echo() -> None:
    print("--- T1 命令回显完整性 ---")
    cmds = [
        "show clock",
        "show version | include uptime",
        "show ip interface brief",
        "show vlan",
        "show mac-address-table",
        "show spanning-tree summary",
        "show lldp neighbors",
        "show dot1x summary",
    ]
    for cmd in cmds:
        r = send(cmd, timeout_ms=25000, quiet_ms=600)
        out = r.get("output") or ""
        hits = [ln.strip() for ln in out.splitlines() if cmd in ln]
        echo_ok = bool(hits) and hits[0].endswith(cmd) and hits[0][:len(hits[0]) - len(cmd)].strip() in ("", "Ruijie>", "Ruijie#")
        prompt_ok = bool(PROMPT_RE.search(out))
        dup = bool(re.search(r"shhooww|ssooww|RuijieRuijie", out))
        record(f"回显 {cmd!r}",
               echo_ok and prompt_ok and not dup,
               f"回显行={len(hits)} 提示符={'有' if prompt_ok else '无'} 重字={'有' if dup else '无'} "
               f"{r['elapsed_ms']}ms")


def t2_paging() -> None:
    print("--- T2 分页与大输出 ---")
    send("terminal length 0", timeout_ms=15000, quiet_ms=400)
    r = send("show running-config", timeout_ms=60000, quiet_ms=900)
    out = r.get("output") or ""
    m = re.search(r"Current configuration\s*:\s*(\d+)\s*bytes", out)
    declared = int(m.group(1)) if m else 0
    more_left = "More" in out or "--More--" in out
    has_end = bool(re.search(r"^\s*end\s*$", out, re.MULTILINE))
    # 判据用设备自己报的字节数，别用硬编码长度（配置大小随设备变化）
    record("show running-config 全量落地",
           declared > 0 and len(out) >= declared and not more_left,
           f"设备声明 {declared} B / 抓回 {len(out)} 字符，More残留={'有' if more_left else '无'}")
    record("配置输出完整（含 end + 提示符）", has_end and bool(PROMPT_RE.search(out)),
           f"end={'有' if has_end else '无'}")
    back = send("terminal length 24", timeout_ms=15000, quiet_ms=400)
    record("恢复 terminal length 24", back.get("output") is not None, "")


def t3_soak() -> None:
    print("--- T3 连续命令压测 ---")
    n = 15
    oks, times = 0, []
    for i in range(n):
        r = send("show clock", timeout_ms=20000, quiet_ms=350)
        out = r.get("output") or ""
        if TIME_RE.search(out) and PROMPT_RE.search(out):
            oks += 1
        times.append(r["elapsed_ms"])
    record(f"{n} 条连发全部有应答", oks == n,
           f"{oks}/{n}，耗时 min={min(times)} 均={sum(times)//len(times)} max={max(times)} ms")


def t4_reconnect() -> None:
    print("--- T4 断线重连 ---")
    try:
        post("/session/close", {"key": KEY})
        closed = True
    except Exception as exc:                      # noqa: BLE001
        closed = False
        print(f"    关闭异常：{exc}")
    time.sleep(2)
    try:
        r = post("/session/open", {"protocol": "serial", "serial_port": "COM5",
                                   "baudrate": 9600, "key": KEY,
                                   "name": "Console交换机"})
        opened = r.get("state") == "connected"
    except Exception as exc:                      # noqa: BLE001
        opened = False
        r = {"error": str(exc)}
    time.sleep(3)
    after = send("show version", timeout_ms=25000, quiet_ms=700)
    ok = closed and opened and "Ruijie" in (after.get("output") or "")
    record("close → open → 再次执行命令", ok,
           f"关闭={closed} 重开={opened} 回显={'有' if 'Ruijie' in (after.get('output') or '') else '无'}")


def t5_log() -> None:
    print("--- T5 会话日志正确性 ---")
    day = time.strftime("%Y-%m-%d")
    d = LOGS / day
    files = sorted(d.glob("Console交换机_*.log"), key=lambda p: p.stat().st_mtime)
    if not files:
        record("会话日志存在", False, f"{d} 下没有 Console交换机_*.log")
        return
    f = files[-1]
    raw = f.read_bytes()
    text = raw.decode("utf-8", "replace")
    ok = len(raw) > 2000 and "show running-config" in text and "\x00" not in text
    record("会话日志完整", ok,
           f"{f.name} {len(raw)} B，含 show running-config={'是' if 'show running-config' in text else '否'}，"
           f"NUL={'有' if chr(0) in text else '无'}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"控制口: {cfg()['url']}  pid={cfg()['pid']}")
    t1_echo()
    t2_paging()
    t3_soak()
    t5_log()
    t4_reconnect()          # 放最后：重连后会话回到用户模式
    send("enable", timeout_ms=15000, quiet_ms=400)

    ok_n = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n结论：{ok_n}/{len(RESULTS)} 通过")
    out = Path(os.environ.get("TEMP", ".")) / "ait_console_functional.json"
    out.write_text(json.dumps([{"name": n, "ok": o, "detail": d} for n, o, d in RESULTS],
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"明细：{out}")
    return 0 if ok_n == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
