"""
Telnet连接器 - 实现Telnet协议连接

注意:
    Python 3.13 起标准库已移除 telnetlib。
    本模块优先使用 telnetlib（Python <= 3.12），
    在不可用时自动回退到内置的纯 socket Telnet 实现（_MiniTelnet），
    对外接口与 telnetlib.Telnet 保持兼容（connect/read_until/write/read_very_eager/close）。
"""
import time
import socket
import threading
from .connector import BaseConnector

try:  # Python <= 3.12
    import telnetlib  # type: ignore
    HAS_TELNETLIB = True
except ImportError:  # Python >= 3.13
    telnetlib = None  # type: ignore
    HAS_TELNETLIB = False


# ---------------------------------------------------------------- Telnet 常量
IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240
_OPT_ECHO = 1
_OPT_SGA = 3


class _MiniTelnet:
    """
    纯 socket 实现的极简 Telnet 客户端

    仅实现终端交互所需的最小功能：
      - IAC 选项协商（默认拒绝，回显/抑制GA 接受）
      - 子协商（SB ... SE）跳过
      - read_until / read_very_eager / write / close
    """

    def __init__(self, host: str, port: int = 23, timeout: int = 10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._pending = bytearray()

    # ------------------------------------------------------------ 内部处理
    def _negotiate(self, data: bytearray) -> bytes:
        """剥离并回应 IAC 协商序列，返回可见数据"""
        out = bytearray()
        i = 0
        n = len(data)
        while i < n:
            byte = data[i]
            if byte != IAC:
                out.append(byte)
                i += 1
                continue

            if i + 1 >= n:
                break
            cmd = data[i + 1]

            if cmd == IAC:                      # 转义的 0xFF
                out.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):  # 3 字节选项协商
                if i + 2 >= n:
                    break
                opt = data[i + 2]
                self._reply(cmd, opt)
                i += 3
            elif cmd == SB:                      # 子协商，跳到 IAC SE
                j = i + 2
                while j + 1 < n and not (data[j] == IAC and data[j + 1] == SE):
                    j += 1
                i = j + 2 if j + 1 < n else n
            else:
                i += 2
        return bytes(out)

    def _reply(self, cmd: int, opt: int):
        """对协商请求给出回应"""
        try:
            if cmd == DO:
                # 只接受 ECHO / SGA，其余一律 WONT
                resp = WILL if opt in (_OPT_ECHO, _OPT_SGA) else WONT
                self.sock.sendall(bytes([IAC, resp, opt]))
            elif cmd == WILL:
                resp = DO if opt in (_OPT_ECHO, _OPT_SGA) else DONT
                self.sock.sendall(bytes([IAC, resp, opt]))
        except OSError:
            pass

    # ------------------------------------------------------------ 对外接口
    def write(self, data: bytes):
        self.sock.sendall(data)

    def read_until(self, expected: bytes, timeout: float = 5):
        """读取直到遇到 expected（或超时），返回已读数据"""
        deadline = time.time() + timeout
        buf = bytearray()
        while time.time() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            self._pending.extend(chunk)
            data = self._negotiate(self._pending)
            self._pending.clear()
            buf.extend(data)
            if expected in bytes(buf):
                break
        return bytes(buf)

    def read_very_eager(self) -> bytes:
        """
        非阻塞读取当前已到达的数据

        注意：必须临时把 socket 设为非阻塞，否则 recv 会一直阻塞到超时，
        导致上层每 10 秒才拿到一批输出（会话看起来像卡住）。
        """
        chunks = bytearray()
        original_timeout = self.sock.gettimeout()
        self.sock.settimeout(0)
        try:
            while True:
                try:
                    chunk = self.sock.recv(4096)
                except (socket.timeout, BlockingIOError):
                    break
                except OSError:
                    break
                if not chunk:
                    break
                self._pending.extend(chunk)
                data = self._negotiate(self._pending)
                self._pending.clear()
                chunks.extend(data)
        finally:
            try:
                self.sock.settimeout(original_timeout)
            except OSError:
                pass
        return bytes(chunks)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# 登录提示语：不同厂商写法差别很大，全部兼容（不区分大小写）
USERNAME_PROMPTS = (
    b"login:", b"login :", b"username:", b"user name:", b"login as:", b"user:",
)
PASSWORD_PROMPTS = (
    b"password:", b"passwd:", b"passcode:", b"password :",
)
# 登录成功后常见的提示（用来确认已进入设备）
PROMPT_TAILS = (b">", b"#", b"]", b"$")
# 登录失败的标识（H3C / 华为 / Cisco 常见写法）
FAIL_PATTERNS = (
    b"wrong password",
    b"login failed",
    b"authentication failed",
    b"invalid password",
    b"% login",                # 部分设备前缀带 %
    b"login incorrect",
    b"access denied",
    b"password: ",             # 失败后再次要求密码
)


#: 线路上的 CR NUL 修饰（RFC 854：CR 后面跟 NUL 表示"这一行只回车不换行"）。
#: 实测 H3C Comware 7 每打一行都是 "\r\x00\r\n"：先 CR NUL 回到行首，再 CR LF 换行。
#: 旧代码把中间那个裸 \r 当成"进度条重写当前行"，**每一行刚打出来就被自己抹掉** ——
#: 用户看到的就是"设备明明有数据，界面只有一行 ---- More ----"（2026-10-19 反馈）。
_CR_NUL_CRLF = "\r\x00\r\n"
_CR_NUL = "\r\x00"


def clean_telnet_text(text: str) -> str:
    """把 telnet 线路上的 CR NUL 修饰清掉，交给界面的是正常的 \\r\\n 行尾

    顺序不能反：先合并"CR NUL + CR LF"这一个完整行尾，再把剩下的 CR NUL 还原成
    裸 CR（那是真的"回到行首重写"，比如进度条），最后丢掉孤立的 NUL。
    """
    if not text:
        return text
    if _CR_NUL_CRLF in text:
        text = text.replace(_CR_NUL_CRLF, "\r\n")
    if _CR_NUL in text:
        text = text.replace(_CR_NUL, "\r")
    if "\x00" in text:
        text = text.replace("\x00", "")
    return text


class TelnetConnector(BaseConnector):
    """Telnet连接器"""

    def __init__(self):
        super().__init__()
        self.tn = None
        self._connected = False
        self._running = False
        self._read_thread = None
        self._buffer = ""
        self._lock = threading.Lock()
        self._host = ""
        self._params: dict = {}
        self._login_buffer = b""   # 登录阶段读到的原始数据（登录完成后回填会话）

    # 兼容两种底层实现
    @staticmethod
    def _open_socket(host: str, port: int, timeout: int):
        if HAS_TELNETLIB:
            return telnetlib.Telnet(host, port, timeout=timeout)
        return _MiniTelnet(host, port, timeout=timeout)

    def _login_read_until_any(self, patterns, timeout: float = 4.0):
        """
        登录阶段读取并匹配提示语（不区分大小写），返回命中的模式，超时返回 None

        读到的数据保留在 self._login_buffer 里，不会被丢弃 —— 否则先匹配到
        用户名提示后，第二次再等同一个提示就会白等一个超时。
        收到数据后若 0.4 秒内不再有新数据且仍无匹配，就提前返回，
        避免免登录设备白等满超时。
        """
        deadline = time.time() + timeout
        last_data_time = 0.0
        while True:
            low = self._login_buffer.lower()
            for pattern in patterns:
                if pattern in low:
                    return pattern
            now = time.time()
            if now >= deadline:
                return None
            # 已经有数据但不再变化 → 说明没有登录提示，提前收工
            if last_data_time and now - last_data_time > 0.4:
                return None
            try:
                data = self.tn.read_very_eager()
            except Exception:
                return None
            if data:
                self._login_buffer += data
                last_data_time = time.time()
                if len(self._login_buffer) > 16384:
                    self._login_buffer = self._login_buffer[-4096:]
            else:
                time.sleep(0.05)

    def connect(self, host: str, username: str = "", password: str = "",
                port: int = 23, timeout: int = 10, **kwargs) -> bool:
        """
        建立Telnet连接

        登录流程：
            1. 唤醒设备（部分设备/Telnet 转 Console 需要先敲一个回车）
            2. 按常见提示语匹配用户名提示 → 立刻输入（不再固定干等 5 秒）
            3. 匹配密码提示 → 输入
            4. 登录阶段读到的内容回填到会话缓冲区，避免丢数据
        匹配不到登录提示时按"免登录设备"处理，直接进入会话。
        """
        try:
            self.tn = self._open_socket(host, port, timeout)
            self._login_buffer = b""
            all_prompts = PROMPT_TAILS + USERNAME_PROMPTS + PASSWORD_PROMPTS
            short = min(2.5, max(1.0, float(timeout) / 3))

            # 1) 唤醒
            matched = self._login_read_until_any(all_prompts, timeout=short)
            if matched is None:
                try:
                    self.tn.write(b"\r")
                except Exception:
                    pass
                matched = self._login_read_until_any(all_prompts, timeout=short)

            # 2) 用户名
            password_attempted = False
            if username:
                if matched in USERNAME_PROMPTS or self._login_read_until_any(
                        USERNAME_PROMPTS, timeout=min(3.0, float(timeout))):
                    self.tn.write(username.encode() + b"\n")
                    self._login_buffer = b""
                    # 3) 密码
                    if password and self._login_read_until_any(
                            PASSWORD_PROMPTS, timeout=min(4.0, float(timeout))):
                        self.tn.write(password.encode() + b"\n")
                        self._login_buffer = b""
                        password_attempted = True
                elif password and self._login_read_until_any(PASSWORD_PROMPTS, timeout=1.5):
                    # 只要求密码的设备
                    self.tn.write(password.encode() + b"\n")
                    self._login_buffer = b""
                    password_attempted = True
            elif password:
                if matched in PASSWORD_PROMPTS or self._login_read_until_any(
                        PASSWORD_PROMPTS, timeout=min(3.0, float(timeout))):
                    self.tn.write(password.encode() + b"\n")
                    self._login_buffer = b""
                    password_attempted = True

            # 4) 登录后验证：等待成功提示符 (> # 等) 或失败标识。
            #    若设备是"静默登录"（不吐任何东西就进入命令模式），到点就当成功。
            #    失败时立刻关连接，否则后续 send() 会被设备当成密码重试，
            #    几次就触发 H3C 默认的 5 分钟锁定。
            if password_attempted:
                verify = self._login_read_until_any(
                    PROMPT_TAILS + FAIL_PATTERNS,
                    timeout=min(3.0, float(timeout)),
                )
                if verify in FAIL_PATTERNS:
                    try:
                        self.tn.close()
                    except Exception:
                        pass
                    msg = verify.decode("utf-8", errors="replace").strip()
                    if verify in (b"password: ", b"passwd:", b"password:"):
                        # 设备什么都没说，只是又要求输一次密码 —— 这就是口令被拒
                        reason = "设备又要求输入密码（说明上一口令被拒绝）"
                    elif verify == b"% login":
                        reason = "设备拒绝了这次登录（% Login invalid）"
                    else:
                        reason = f"设备返回 \"{msg}\""
                    self.error_occurred.emit(
                        f"Telnet登录失败：{reason}。请确认密码/用户名是否正确；"
                        f"连续多次失败部分设备会临时锁定账号 5 分钟")
                    return False

            self._host = host
            self._params = {'host': host, 'port': port, 'username': username}
            self._connected = True
            self._running = True

            self._read_thread = threading.Thread(
                target=self._read_loop,
                name=f"telnet-read-{host}",
                daemon=True
            )
            self._read_thread.start()

            # 4) 登录阶段读到的内容交给会话，避免开头输出丢失
            if self._login_buffer:
                leftover = clean_telnet_text(
                    self._login_buffer.decode('utf-8', errors='replace'))
                self._login_buffer = b""
                with self._lock:
                    self._buffer += leftover
                self.output_received.emit(leftover)
                self._notify_output(leftover)

            time.sleep(0.4)
            return True

        except Exception as e:
            self.error_occurred.emit(f"Telnet连接失败: {str(e)}")
            return False

    def _read_loop(self):
        """后台读取输出"""
        while self._running and self.tn:
            try:
                data = self.tn.read_very_eager().decode('utf-8', errors='replace')
                # CR NUL 必须在这里就清掉：它进了界面会被当成"整行重写"，
                # 把设备刚打的每一行抹掉（见 clean_telnet_text 的注释）
                data = clean_telnet_text(data)
                if data:
                    with self._lock:
                        self._buffer += data
                    self.output_received.emit(data)
                    self._notify_output(data)
                else:
                    time.sleep(0.05)
            except Exception:
                break

        if self._connected:
            self._connected = False
            self.connection_lost.emit()

    def send(self, command: str):
        """发送命令"""
        if self.tn and self._connected:
            self.tn.write(command.encode() + (self.line_ending or '').encode())
            time.sleep(0.3)

    def send_raw(self, data: str):
        """发送原始数据，不追加换行"""
        if self.tn and self._connected:
            self.tn.write(data.encode())

    def send_break(self, duration: float = 0.25) -> bool:
        """发送 Telnet BREAK（IAC BRK），用于打断设备启动"""
        if not (self.tn and self._connected):
            return False
        try:
            self.tn.write(bytes([IAC, 243]))  # IAC BRK
            return True
        except Exception as e:
            self.error_occurred.emit(f"发送 Break 失败: {e}")
            return False

    def disconnect(self):
        """断开连接"""
        was_connected = self._connected
        self._running = False
        self._connected = False
        if self.tn:
            try:
                self.tn.close()
            except Exception:
                pass
        if was_connected:
            self.connection_lost.emit()

    def is_connected(self) -> bool:
        """检查连接状态"""
        return self._connected and self.tn is not None

    def get_output(self) -> str:
        """获取缓冲区内容"""
        with self._lock:
            output = self._buffer
            self._buffer = ""
            return output

    def get_output_block(self, timeout: float = 2.0) -> str:
        """阻塞获取输出"""
        result = ""
        last_data_time = time.time()

        while time.time() - last_data_time < timeout:
            with self._lock:
                if self._buffer:
                    result += self._buffer
                    self._buffer = ""
                    last_data_time = time.time()
            time.sleep(0.1)

        return result

    def info(self) -> str:
        return f"Telnet {self._host}"
