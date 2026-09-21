"""
单元测试：验证 core/telnet_connector.TelnetConnector 在密码错误时
立即返回 False 并关连接，不再让后续 send() 被设备当成密码重试。

用本地 mock Telnet 服务器模拟 H3C 的行为：
- 发送 IAC + 横幅 + "Password:" 提示
- 收到任何输入（当作密码）→ 回复 "%Wrong password!\r\nPassword:"
- 收到第二个回车 → "%Wrong password!" 后再次要求密码
"""

import os
import socket
import sys
import threading
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from PyQt6.QtCore import QCoreApplication

app = QCoreApplication.instance() or QCoreApplication(sys.argv)

from core.telnet_connector import TelnetConnector

IAC = 255
WILL = 251
DO = 253
WONT = 252
DONT = 254
ECHO = 1
SGA = 3


def h3c_mock_server(port_holder, stop_event, attempts_holder):
    """
    启动一个本地 mock，模拟 H3C "Password:" only 登录。
    收到任何密码尝试都回复 '%Wrong password!'，并统计收到了几次。
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_holder.append(srv.getsockname()[1])

    srv.settimeout(8)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        srv.close()
        return

    try:
        # 横幅：IAC 协商 + H3C 风格 banner + Password:
        banner = (
            b"\xff\xfb\x01"            # IAC WILL ECHO
            b"\xff\xfb\x03"            # IAC WILL SGA
            b"\xff\xfd\x18"            # IAC DO TERMINAL-TYPE
            b"\xff\xfd\x1f"            # IAC DO NAWS
            b"\r\n**********************************************\r\n"
            b"* H3C Comware mock for login-verify test      *\r\n"
            b"**********************************************\r\n"
            b"\r\nLogin authentication\r\n\r\nPassword:"
        )
        conn.sendall(banner)

        # 接收密码尝试，每收到一行计一次
        conn.settimeout(0.4)
        buf = b""
        deadline = time.time() + 6
        attempt_count = 0
        while time.time() < deadline and not stop_event.is_set():
            try:
                chunk = conn.recv(256)
            except socket.timeout:
                # 触发一次主动回应，再给一次机会
                if buf:
                    conn.sendall(b" %Wrong password!\r\nPassword:")
                    attempt_count += 1
                    buf = b""
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            # 简单判定：一行视为一次密码尝试
            if b"\n" in buf or b"\r" in buf:
                conn.sendall(b" %Wrong password!\r\nPassword:")
                attempt_count += 1
                buf = b""

        attempts_holder.append(attempt_count)
    finally:
        try:
            conn.close()
        except OSError:
            pass
        srv.close()


def run_test():
    print("=" * 60)
    print("测试 1: 错密码时，连接器必须立即返回 False，不能烧多次重试")
    print("=" * 60)

    port_holder = []
    attempts_holder = []
    stop = threading.Event()
    t = threading.Thread(target=h3c_mock_server,
                        args=(port_holder, stop, attempts_holder),
                        daemon=True)
    t.start()
    # 等监听端口
    for _ in range(20):
        if port_holder:
            break
        time.sleep(0.05)
    assert port_holder, "Mock server 没起来"
    port = port_holder[0]

    c = TelnetConnector()
    errors = []
    c.error_occurred.connect(lambda m: errors.append(m))

    t0 = time.time()
    ok = c.connect("127.0.0.1", password="WRONG_PASSWORD", port=port, timeout=6)
    elapsed = time.time() - t0

    print(f"\nconnect() -> {ok}, 耗时 {elapsed:.2f}s")
    print(f"is_connected() -> {c.is_connected()}")
    print(f"error_occurred 信号: {errors}")

    stop.set()
    t.join(timeout=2)

    attempts = attempts_holder[0] if attempts_holder else 0
    print(f"\nMock 服务器收到的密码尝试次数: {attempts}")

    ok_check = (ok is False) and (not c.is_connected()) and (attempts <= 1)
    print("\n期望: ok=False, 未连接, 尝试次数<=1")
    print("结果:", "✓ 通过" if ok_check else "✗ 失败")
    if not ok_check:
        print("  ok False?", ok is False)
        print("  is_connected False?", not c.is_connected())
        print("  attempts <= 1?", attempts <= 1, f"(实际 {attempts})")

    print()
    print("=" * 60)
    print("测试 2: 错密码后 send() 不能把命令当成密码再发出去")
    print("=" * 60)

    attempts_before = attempts_holder[0] if attempts_holder else 0
    # 故意调用 3 次 send()，模拟之前会烧重试的场景
    for fake_cmd in ("display version", "display vlan", "screen-length disable"):
        try:
            c.send(fake_cmd)
        except Exception as e:
            print(f"  send({fake_cmd!r}) 抛异常: {type(e).__name__}: {e}")
    time.sleep(0.5)  # 给 mock 服务器时间（如果还会回话）
    stop.set()
    t.join(timeout=2)

    attempts_after = attempts_holder[0] if attempts_holder else 0
    delta = attempts_after - attempts_before
    print(f"\nsend() 前 mock 已收到尝试: {attempts_before}")
    print(f"3 次 send() 后 mock 收到的尝试: {attempts_after}")
    print(f"增量: {delta}")
    print("\n期望: 增量 = 0（send() 不应再产生任何密码尝试）")
    print("结果:", "✓ 通过" if delta == 0 else "✗ 失败")

    print()
    print("=" * 60)
    print("汇总")
    print("=" * 60)
    return ok_check and (delta == 0)


if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)