# -*- coding: utf-8 -*-
"""假设备：一个最小的 telnet 服务器，给「接管通道」做端到端演示与测试。

用途：手边没有真机时，让 NetOpsTerminal 通过 127.0.0.1 连上来，验证
open → send → read → close 整条链路（含分页、回显、提示符判定）。

跑法：
    python tools\\dummy_telnet.py                 # 默认 127.0.0.1:23001
    python tools\\dummy_telnet.py --port 23002

说明：不实现 telnet 选项协商（对 IAC 一律回 WONT/DONT），够 NetOpsTerminal 用。
"""
import argparse
import socket
import threading
import time

PROMPT = "DummySwitch>"
BANNER = ("\r\nDummySwitch Console\r\n"
          "H3C Comware Software, Version 7.1.070, Release 3506P11\r\n"
          + PROMPT)
VERSION = ("H3C Comware Software, Version 7.1.070, Release 3506P11\r\n"
           "Copyright (c) 2004-2023 New H3C Technologies Co., Ltd.\r\n"
           "DummySwitch uptime is 0 weeks, 1 day, 2 hours, 3 minutes\r\n"
           "Boot image: flash:/dummy.bin\r\n" + PROMPT)
IFACE = ("Interface            Link Speed   Duplex Type  PVID Description\r\n"
         "GE1/0/1              UP   1G(a)   F(a)   A     1    to-PC\r\n"
         "GE1/0/2              DOWN auto    auto   A     1    --\r\n" + PROMPT)
LONG = "".join("  line %02d: dummy pagination filler\r\n" % i for i in range(60)) + PROMPT


def reply_for(command: str) -> str:
    cmd = command.strip().lower()
    if not cmd:
        return PROMPT
    if cmd.startswith("display version"):
        return "\r\n" + VERSION
    if "interface" in cmd:
        return "\r\n" + IFACE
    if cmd.startswith("display ") and "long" in cmd:
        return "\r\n" + LONG
    return "\r\n  %s: 这是假设备的回显（命令 %s）\r\n%s" % (PROMPT, command.strip(), PROMPT)


def handle(conn: socket.socket, addr, quiet: float = 0.15):
    print("  [假设备] 连上了 %s:%s" % addr, flush=True)
    try:
        conn.sendall(BANNER.encode("utf-8"))
        buf = b""
        while True:
            data = conn.recv(4096)
            if not data:
                break
            # 对 telnet 协商（IAC=255）一律回 WONT/DONT，避免对端等协商
            if 255 in data:
                out = bytearray()
                i = 0
                while i < len(data):
                    if data[i] == 255 and i + 2 < len(data):
                        out += bytes([255, 252, data[i + 2]])   # IAC WONT <opt>
                        i += 3
                    else:
                        out.append(data[i])
                        i += 1
                conn.sendall(bytes(out))
                data = bytes(b for b in data if b != 255)
            buf += data
            text = buf.decode("utf-8", "ignore")
            if "\r" in text or "\n" in text:
                for line in text.replace("\r", "\n").split("\n"):
                    if line.strip():
                        time.sleep(quiet)                      # 假装设备在跑命令
                        conn.sendall(reply_for(line).encode("utf-8"))
                buf = b""
    except OSError as exc:
        print("  [假设备] 连接断了：%s" % exc, flush=True)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=23001)
    args = ap.parse_args()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(4)
    print("假设备已就绪：telnet %s %d" % (args.host, args.port), flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
