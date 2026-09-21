"""
模拟网络设备 - 没有真实设备时用来验证 AITerminal 的连接/命令/分页链路

它会监听一个 TCP 端口，模拟一台 H3C 交换机的 Console/登录会话：
    · 回显命令、返回提示符 <H3C> / [H3C-GigabitEthernet1/0/1]
    · 对 display/show 类命令返回像样的输出
    · 支持分页（---- More ----，需要按空格翻页），可用 screen-length 0 temporary 关闭
    · 支持 save 的 [Y/N] 确认提示（用来验证"需人工确认"分支）
    · 未知命令返回 % Unrecognized command

用法:
    python tools/fake_device.py                 # 默认监听 127.0.0.1:2323
    python tools/fake_device.py --port 2323 --paging
    python tools/fake_device.py --vendor cisco  # 模拟 Cisco（show / Router#）

然后用另一个终端验证:
    python tools/verify_device.py --protocol telnet --host 127.0.0.1 --port 2323
"""
import argparse
import socket
import threading
import time

IAC = 255

CANNED = {
    "display version": [
        "H3C Comware Software, Version 7.1.070, Release 6126P02",
        "Copyright (c) 2004-2021 New H3C Technologies Co., Ltd.",
        "H3C S5560X-30C-EI uptime is 12 weeks, 3 days, 4 hours, 5 minutes",
        "Boot image: flash:/s5560x-cmw710-boot-r6126p02.bin",
    ],
    "display interface brief": [
        "Brief information on interfaces in route mode:",
        "Link: ADM - administratively down; Stby - standby",
        "Interface            Link Speed   Duplex Type  PVID Description",
        "GE1/0/1              UP   1G(a)   F(a)   Access 10   to-Core",
        "GE1/0/2              UP   1G(a)   F(a)   Access 20   to-AP",
        "GE1/0/24             DOWN auto    auto   Access 1    spare",
        "Vlan1                UP   --      --     --     --",
    ],
    "display cpu-usage": [
        "Slot 1 CPU usage:",
        "   5% in last 5 seconds",
        "   4% in last 1 minute",
        "   4% in last 5 minutes",
    ],
    "display memory": [
        "The statistics about memory is measured in KB:",
        "Slot 1:",
        "Total    Used    Free   Shared  Buffers  Cached",
        "524288  180224  344064        0    12456   58420",
    ],
    "display logbuffer": [
        "%Sep 21 19:58:12:336 2026 H3C IFNET/3/PHY_UPDOWN: Physical state on the interface",
        "GigabitEthernet1/0/24 changed to down.",
        "%Sep 21 19:58:30:112 2026 H3C IFNET/5/LINK_UPDOWN: Line protocol state on the interface",
        "GigabitEthernet1/0/1 changed to up.",
    ],
    "display current-configuration": [
        "#",
        " sysname H3C-Core-SW",
        "#",
        " vlan 1",
        "#",
        " vlan 10",
        "  name USERS",
        " vlan 20",
        "  name AP-MGMT",
        "#",
        " interface GigabitEthernet1/0/1",
        "  port link-type access",
        "  port default vlan 10",
        "  description to-Core",
        "#",
        " interface GigabitEthernet1/0/2",
        "  port link-type access",
        "  port default vlan 20",
        "  description to-AP",
        "#",
        " interface Vlan-interface1",
        "  ip address 10.0.0.1 255.255.255.0",
        "#",
        " snmp-agent",
        " snmp-agent sys-info version v2c",
        " snmp-agent community read monitor_ro",
        "#",
        " return",
    ],
    "show version": [
        "Cisco IOS Software, C2960 Software (C2960-LANBASEK9-M), Version 15.0(2)SE11",
        "Router uptime is 5 weeks, 2 days, 1 hour, 3 minutes",
        "System returned to ROM by power-on",
    ],
    "show ip interface brief": [
        "Interface              IP-Address      OK? Method Status                Protocol",
        "GigabitEthernet0/1     10.0.0.1        YES NVRAM  up                    up",
        "GigabitEthernet0/2     unassigned      YES NVRAM  administratively down down",
        "Vlan1                  192.168.1.1     YES NVRAM  up                    up",
    ],
    "show running-config": [
        "Building configuration...",
        "Current configuration : 2145 bytes",
        "!",
        "version 15.0",
        "hostname Router",
        "!",
        "interface GigabitEthernet0/1",
        " ip address 10.0.0.1 255.255.255.0",
        "!",
        "end",
    ],
}


class FakeDevice:
    """单连接会话处理"""

    def __init__(self, vendor: str = "h3c", paging: bool = False,
                 login: bool = True, prompt_delay: float = 0.05):
        self.vendor = vendor
        self.paging = paging
        self.login = login
        self.prompt_delay = prompt_delay

    # ------------------------------------------------------------ 提示符
    @property
    def prompt(self) -> str:
        if self.vendor == "cisco":
            return "Router#"
        return "<H3C>"

    @property
    def config_prompt(self) -> str:
        if self.vendor == "cisco":
            return "Router(config)#"
        return "[H3C]"

    # ------------------------------------------------------------ 会话
    def handle(self, conn: socket.socket, addr):
        try:
            self._serve(conn)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
        except Exception as e:
            print(f"  [会话异常] {addr}: {type(e).__name__}: {e}")
        finally:
            try:
                conn.close()
            except OSError:
                pass
            print(f"  [断开] {addr}")

    def _serve(self, conn: socket.socket):
        conn.settimeout(0.2)
        self._send(conn, f"\r\nFake {self.vendor.upper()} device (AITerminal 自测用)\r\n"
                         f"Press ENTER to get started.\r\n")
        if self.login:
            self._send(conn, "Login authentication\r\nUsername:")
            self._read_line(conn, timeout=30)
            self._send(conn, "\r\nPassword:")
            self._read_line(conn, timeout=30)
            self._send(conn, "\r\n")

        in_config = False
        buffer = b""
        while True:
            try:
                data = conn.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return

            buffer += self._strip_iac(data)
            while True:
                line, buffer, terminator = self._take_line(buffer)
                if terminator is None:
                    break
                command = line.strip()
                if not command:
                    self._send(conn, f"\r\n{self.config_prompt if in_config else self.prompt}")
                    continue

                print(f"  [收到] {command}")
                in_config, stop = self._execute(conn, command, in_config)
                if stop:
                    return

    @staticmethod
    def _strip_iac(data: bytes) -> bytes:
        """丢弃 Telnet 协商字节，只保留普通数据"""
        out = bytearray()
        index = 0
        while index < len(data):
            byte = data[index]
            if byte == IAC and index + 1 < len(data):
                nxt = data[index + 1]
                if nxt == IAC:
                    out.append(IAC)
                    index += 2
                    continue
                if nxt in (251, 252, 253, 254):
                    index += 3
                    continue
                index += 2
                continue
            out.append(byte)
            index += 1
        return bytes(out)

    @staticmethod
    def _take_line(buffer: bytes):
        """从缓冲区取出一行（兼容 \\r\\n 与 \\r）"""
        for index, byte in enumerate(buffer):
            if byte in (10, 13):
                line = buffer[:index].decode('utf-8', errors='replace')
                rest = buffer[index + 1:]
                if byte == 13 and rest[:1] == b"\n":
                    rest = rest[1:]
                return line, rest, 'cr' if byte == 13 else 'lf'
        return "", buffer, None

    def _read_line(self, conn: socket.socket, timeout: float = 10.0):
        """读取一行（用于登录输入）"""
        deadline = time.time() + timeout
        buffer = b""
        while time.time() < deadline:
            try:
                data = conn.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                return ""
            if not data:
                return ""
            buffer += self._strip_iac(data)
            line, _rest, terminator = self._take_line(buffer)
            if terminator:
                return line.strip()
        return ""

    def _read_key(self, conn: socket.socket, timeout: float = 20.0) -> str:
        """
        读取一个按键（不要求换行）

        真实设备翻页时收到单个空格就继续，不会等回车，所以这里不能按行读。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = conn.recv(64)
            except socket.timeout:
                continue
            except OSError:
                return ""
            if not data:
                return ""
            data = self._strip_iac(data)
            if data:
                return data.decode('utf-8', errors='replace')[:1]
        return ""

    def _send(self, conn: socket.socket, text: str):
        conn.sendall(text.encode('utf-8', errors='replace'))

    # ------------------------------------------------------------ 命令
    def _execute(self, conn, command: str, in_config: bool):
        """返回 (是否在配置视图, 是否结束会话)"""
        low = command.lower()

        if low in ("exit", "quit"):
            if in_config:
                self._send(conn, f"\r\n{self.prompt}")
                return False, False
            self._send(conn, "\r\n")
            return False, True

        if low == "return" or low == "end":
            self._send(conn, f"\r\n{self.prompt}")
            return False, False

        if low in ("system-view", "configure terminal", "conf t"):
            self._send(conn, f"\r\n{self.config_prompt}")
            return True, False

        # 关闭分屏
        if low in ("screen-length 0 temporary", "screen-length disable",
                   "terminal length 0", "terminal datadump"):
            self.paging = False
            self._send(conn, f"\r\n{self.config_prompt if in_config else self.prompt}")
            return in_config, False

        # 保存（带确认提示，用来验证"需人工确认"分支）
        if low in ("save", "save force", "write memory", "wr"):
            if low == "save force":
                self._send(conn, "\r\nSaving the configuration... Done.\r\n"
                                 f"{self.prompt}")
                return in_config, False
            self._send(conn, "\r\nAre you sure? [Y/N]:")
            answer = self._read_line(conn, timeout=15)
            print(f"  [确认应答] {answer!r}")
            if answer.lower().startswith('y'):
                self._send(conn, "\r\nSaving the configuration... Done.\r\n")
            else:
                self._send(conn, "\r\nSave cancelled.\r\n")
            self._send(conn, self.prompt)
            return in_config, False

        # 输出类命令
        body = self._lookup(low)
        if body is not None:
            self._send(conn, "\r\n")
            self._send_paged(conn, body)
            self._send(conn, f"\r\n{self.config_prompt if in_config else self.prompt}")
            return in_config, False

        # 配置类命令（在配置视图里一律接受）
        if in_config:
            self._send(conn, f"\r\n{self.config_prompt}")
            return True, False

        self._send(conn, "\r\n% Unrecognized command found at '^' position.\r\n"
                         f"{self.prompt}")
        return False, False

    def _lookup(self, low: str):
        """按命令查预置输出（支持前缀匹配）"""
        for key, lines in CANNED.items():
            if low == key:
                return lines
        for key, lines in CANNED.items():
            if low.startswith(key):
                return lines
        return None

    def _send_paged(self, conn, lines):
        """按分页设置输出（每 6 行翻一页，等空格继续）"""
        page_size = 6
        for index, line in enumerate(lines):
            self._send(conn, line + "\r\n")
            if self.paging and (index + 1) % page_size == 0 and index + 1 < len(lines):
                self._send(conn, "  ---- More ----")
                # 等一个按键（真实设备收到空格就继续，q 退出）
                key = self._read_key(conn, timeout=20)
                if key.strip().lower().startswith('q'):
                    return
                self._send(conn, "\r")
            else:
                time.sleep(0.01)


def serve(host: str, port: int, vendor: str, paging: bool, login: bool):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(16)

    print("=" * 62)
    print(f"模拟设备已启动: {host}:{port}  厂商={vendor}  "
          f"分页={'开' if paging else '关'}  登录={'需要' if login else '不需要'}")
    print("=" * 62)
    print("用另一个终端验证:")
    print(f"  python tools/verify_device.py --protocol telnet "
          f"--host {host} --port {port}"
          + ("" if login else " --no-login"))
    print()
    print("按 Ctrl+C 停止")
    print()

    while True:
        try:
            conn, addr = server.accept()
        except KeyboardInterrupt:
            print("\n已停止")
            return
        print(f"[连接] {addr}")
        device = FakeDevice(vendor=vendor, paging=paging, login=login)
        threading.Thread(target=device.handle, args=(conn, addr), daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description="模拟网络设备（AITerminal 自测用）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=2323, help="监听端口")
    parser.add_argument("--vendor", default="h3c", choices=["h3c", "cisco"],
                        help="模拟的厂商（影响提示符与命令语法）")
    parser.add_argument("--paging", action="store_true",
                        help="开启分页（输出 ---- More ----，需要按空格翻页）")
    parser.add_argument("--no-login", action="store_true",
                        help="不需要输入用户名密码（Console 场景）")
    args = parser.parse_args()

    try:
        serve(args.host, args.port, args.vendor, args.paging, not args.no_login)
    except OSError as e:
        print(f"启动失败: {e}\n（端口可能被占用，换一个 --port 试试）")


if __name__ == '__main__':
    main()
