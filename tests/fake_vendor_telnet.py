#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多厂商假设备（Telnet）—— 在没有真机、也还没下到 EVE-NG 镜像时，
用它来验证 NetOpsTerminal 的**厂商相关逻辑**：提示符嗅探、提示符识别、
按视图过滤补全、各厂商的擦除/分页行为。

为什么值得有这个东西
--------------------
真机（或 EVE-NG）覆盖的是"厂商 CLI 回显的**真实字节**"；假设备覆盖的是
"**提示符形态与视图层级**"。后者恰恰是嗅探/视图过滤的输入，而它**零下载、
可重复、可对每种形态造边界用例**（比如把提示符切成两帧发）。
两者互补，不能互相替代 —— 真机那份在 `hcl_e2e.py` 里。

覆盖的 5 种提示符体系（这是 App 里 `sniffVendor`/提示符识别要认的东西）：

  vendor   用户视图        特权/系统视图      配置视图
  h3c      <H3C>          [H3C]            [H3C-GigabitEthernet1/0/1]
  huawei   <HUAWEI>       [HUAWEI]         [HUAWEI-GigabitEthernet0/0/1]
  ruijie   Ruijie>        Ruijie#          Ruijie(config-if-GigabitEthernet 0/1)#
  cisco    Router>        Router#          Router(config-if)#
  juniper  user@host>     user@host#       (JunOS 是单层)

⚠ 注意 juniper：JunOS 的 `user@host>` **不带厂商名**，和 H3C/华为/锐捷那种
  "提示符里就写着厂商" 的形态不同 —— 这正是 App 可能认不出来的地方，值得一测。

用法
----
  python tests/fake_vendor_telnet.py --vendor huawei --port 2401
  python tests/fake_vendor_telnet.py --vendor cisco --port 2402 --split-prompt
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time

IAC, DONT, DO, WONT, WILL = 255, 254, 253, 252, 251
SB, SE = 250, 240


# ---------------------------------------------------------------- 厂商档案
# 每个档案只放"提示符体系 + 少量 canned 回显"。canned 命令用该厂商**自己的语法**
# （display vs show），因为 App 的补全索引是按厂商分开的。
PROFILES = {
    "h3c": {
        "banner": [
            "H3C Comware Software, Version 7.1.070, Alpha 7170",
            "Copyright (c) 2004-2022 New H3C Technologies Co., Ltd.",
        ],
        "user": "<H3C>",
        "priv": "[H3C]",
        "config_if": "[H3C-GigabitEthernet1/0/1]",
        "enter_priv": "system-view",
        "enter_if": "interface GigabitEthernet1/0/1",
        "paging": "screen-length disable",
        "unknown": " % Unrecognized command found at '^' position.",
        "canned": {
            "display version": "H3C Comware Software, Version 7.1.070, Alpha 7170",
            "display interface brief": (
                "Brief information on interfaces in route mode:\n"
                "Interface              Link Speed   Duplex Type  PVID\n"
                "GE1/0/1                UP   1000M   Full   --    1"),
        },
    },
    "huawei": {
        "banner": [
            "Huawei Versatile Routing Platform Software",
            "VRP (R) software, Version 8.180 (CE6865EI V200R019C10SPC800)",
            "Copyright (C) 2012-2022 Huawei Technologies Co., Ltd.",
        ],
        "user": "<HUAWEI>",
        "priv": "[HUAWEI]",
        "config_if": "[HUAWEI-GigabitEthernet0/0/1]",
        "enter_priv": "system-view",
        "enter_if": "interface GigabitEthernet0/0/1",
        "paging": "screen-length 0 temporary",
        "unknown": "Error: Unrecognized command found at '^' position.",
        "canned": {
            "display version": "VRP (R) software, Version 8.180 (CE6865EI V200R019C10SPC800)",
            "display interface brief": (
                "PHY: Physical\n"
                "Interface            PHY     Protocol InUti OutUti   inErrors  outErrors\n"
                "GE0/0/1              up      up          0%     0%          0          0"),
        },
    },
    "ruijie": {
        "banner": [
            "Ruijie Networks",
            "RGOS Version 11.4(1)B74P2, Release(10142202)",
        ],
        "user": "Ruijie>",
        "priv": "Ruijie#",
        "config_if": "Ruijie(config-if-GigabitEthernet 0/1)#",
        "enter_priv": "enable",
        "enter_if": "interface GigabitEthernet 0/1",
        "paging": "terminal length 0",
        "unknown": "% Invalid input detected at '^' marker.",
        "canned": {
            "show version": "RGOS Version 11.4(1)B74P2, Release(10142202)",
            "show interfaces status": (
                "Interface            Status   Speed  Duplex Vlan\n"
                "GigabitEthernet 0/1  up       1000M  Full   1"),
        },
    },
    "cisco": {
        "banner": [
            "Cisco IOS Software, C2960 Software (C2960-LANBASEK9-M), Version 15.0(2)SE",
            "Copyright (c) 1986-2012 by Cisco Systems, Inc.",
        ],
        "user": "Router>",
        "priv": "Router#",
        "config_if": "Router(config-if)#",
        "enter_priv": "enable",
        "enter_if": "interface GigabitEthernet0/1",
        "paging": "terminal length 0",
        "unknown": "% Invalid input detected at '^' marker.",
        "canned": {
            "show version": "Cisco IOS Software, C2960 Software, Version 15.0(2)SE",
            "show ip interface brief": (
                "Interface              IP-Address      OK? Method Status                Protocol\n"
                "GigabitEthernet0/1     unassigned      YES unset  up                    up"),
        },
    },
    "juniper": {
        "banner": [
            "Amnesiac (ttyd0)",
            "login:",
        ],
        # ⚠ JunOS 提示符**不含厂商名** —— 这是嗅探的难点。
        "user": "user@host>",
        "priv": "user@host#",
        "config_if": "user@host#",
        "enter_priv": "configure",
        "enter_if": "set interfaces ge-0/0/1",
        "paging": "set cli screen-length 0",
        "unknown": "syntax error, expecting <command>.",
        "canned": {
            "show version": "Hostname: host\nModel: vsrx\nJunos: 20.4R3-S1.3",
            "show interfaces terse": (
                "Interface               Admin Link Proto    Local                 Remote\n"
                "ge-0/0/1                up    up"),
        },
    },
}

PROMPT_SPLIT_DELAY = 0.012       # 拆帧时两半之间的间隔（模拟真机的切帧）


def send_line(sock, text: str, split: bool = False):
    """发一行。split=True 时**把提示符切成两帧**发 —— 真机 9600 波特下
    提示符被切成两块是常态，第一版嗅探只看单块，于是离线过、真机挂。"""
    data = (text + "\r\n").encode()
    if split and len(data) > 4:
        cut = len(data) // 2
        sock.sendall(data[:cut])
        time.sleep(PROMPT_SPLIT_DELAY)
        sock.sendall(data[cut:])
    else:
        sock.sendall(data)


def _strip_iac(sock, data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break
        cmd = data[i + 1]
        if cmd in (DO, DONT, WILL, WONT) and i + 2 < n:
            opt = data[i + 2]
            reply = b""
            if cmd == DO:
                reply = bytes([IAC, WONT, opt])
            elif cmd == WILL:
                reply = bytes([IAC, DONT, opt])
            if reply:
                try:
                    sock.sendall(reply)
                except OSError:
                    pass
            i += 3
        elif cmd == SB:
            j = data.find(bytes([IAC, SE]), i)
            i = n if j < 0 else j + 2
        else:
            i += 2
    return bytes(out)


def handle(sock, addr, prof, split_prompt, quiet):
    state = {"view": "user"}
    try:
        sock.sendall(bytes([IAC, WILL, 1, IAC, WILL, 3]))
        sock.settimeout(60)
        send_line(sock, "")
        for ln in prof["banner"]:
            send_line(sock, ln)
        send_line(sock, "")
        send_line(sock, prof["user"], split=split_prompt)

        buf = b""
        while True:
            data = sock.recv(1024)
            if not data:
                break
            buf += _strip_iac(sock, data)
            while b"\n" in buf or b"\r" in buf:
                if b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                elif b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                else:
                    line, buf = buf.split(b"\r", 1)
                cmd = line.decode("utf-8", errors="replace").strip()
                low = cmd.lower()

                if not cmd:
                    send_line(sock, _prompt(prof, state))
                    continue
                if low in ("quit", "exit", "logout"):
                    if state["view"] != "user":
                        state["view"] = "user"
                    else:
                        send_line(sock, "Bye!")
                        return
                elif low in ("return", "end"):
                    state["view"] = "user"
                    send_line(sock, "")
                elif low == prof["enter_priv"]:
                    state["view"] = "priv"
                    send_line(sock, "")
                elif low == prof["enter_if"]:
                    state["view"] = "config_if"
                    send_line(sock, "")
                elif low == prof["paging"] or low.startswith("screen-length"):
                    send_line(sock, "")
                elif low in prof["canned"]:
                    send_line(sock, "")
                    for ln in prof["canned"][low].split("\n"):
                        send_line(sock, ln)
                    send_line(sock, "")
                else:
                    send_line(sock, "")
                    send_line(sock, prof["unknown"])
                if not quiet:
                    print("[%s] %r -> %s" % (prof["_vendor"], cmd,
                                             _prompt(prof, state)), flush=True)
                send_line(sock, _prompt(prof, state), split=split_prompt)
    except (ConnectionResetError, OSError, TimeoutError):
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _prompt(prof, state):
    return {"user": prof["user"], "priv": prof["priv"],
            "config_if": prof["config_if"]}[state["view"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", required=True, choices=sorted(PROFILES))
    ap.add_argument("--port", type=int, default=2401)
    ap.add_argument("--split-prompt", action="store_true",
                    help="把提示符切成两帧发（复现真机 9600 波特下的分块形态）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    prof = dict(PROFILES[a.vendor])
    prof["_vendor"] = a.vendor
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.port))
    srv.listen(8)
    print("假设备 %s 在 127.0.0.1:%d  提示符 %s / %s%s"
          % (a.vendor, a.port, prof["user"], prof["priv"],
             "  [拆帧模式]" if a.split_prompt else ""), flush=True)
    while True:
        sock, addr = srv.accept()
        threading.Thread(target=handle,
                         args=(sock, addr, prof, a.split_prompt, a.quiet),
                         daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
