"""
SSH连接器 - 基于Paramiko实现SSH连接
"""
import time
import threading
import paramiko
from .connector import BaseConnector


class SSHConnector(BaseConnector):
    """
    SSH连接器

    支持密码认证和密钥认证
    支持华三/华为/H3C/Cisco等厂商设备
    """

    def __init__(self):
        super().__init__()
        self.client: paramiko.SSHClient = None
        self.channel = None
        self._connected = False
        self._running = False
        self._read_thread = None
        self._buffer = ""
        self._lock = threading.Lock()
        self._host = ""
        self._params: dict = {}

    def connect(self, host: str, username: str, password: str = "",
                port: int = 22, key_filename: str = None,
                timeout: int = 10, **kwargs) -> bool:
        """
        建立SSH连接

        参数:
            host: 设备IP地址
            username: 用户名
            password: 密码
            port: 端口号，默认22
            key_filename: 密钥文件路径，可选
            timeout: 连接超时时间，默认10秒
        返回:
            连接成功返回True，失败返回False
        """
        try:
            self.client = paramiko.SSHClient()
            # 自动接受未知主机密钥
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            # 连接参数
            connect_params = {
                'hostname': host,
                'port': port,
                'username': username,
                'timeout': timeout,
                'banner_timeout': timeout,
                'auth_timeout': timeout,
                'allow_agent': False,
                'look_for_keys': False,
            }
            if key_filename:
                connect_params['key_filename'] = key_filename
            else:
                connect_params['password'] = password

            self.client.connect(**connect_params)

            # 开启 SSH 保活，避免设备侧空闲超时把会话踢掉
            try:
                transport = self.client.get_transport()
                if transport is not None:
                    transport.set_keepalive(30)
            except Exception:
                pass

            # 创建交互式Shell
            self.channel = self.client.invoke_shell(
                term='vt100',
                width=200,
                height=50
            )
            self._host = host
            self._params = dict(connect_params)
            self._params.pop('password', None)
            self._connected = True
            self._running = True

            # 启动后台读取线程
            self._read_thread = threading.Thread(
                target=self._read_loop,
                name=f"ssh-read-{host}",
                daemon=True
            )
            self._read_thread.start()

            time.sleep(1)  # 等待初始输出
            return True

        except paramiko.AuthenticationException:
            self.error_occurred.emit("认证失败：用户名或密码错误")
            return False
        except paramiko.SSHException as e:
            self.error_occurred.emit(f"SSH错误: {str(e)}")
            return False
        except Exception as e:
            self.error_occurred.emit(f"连接失败: {str(e)}")
            return False

    def _read_loop(self):
        """后台读取设备输出"""
        while self._running and self.channel:
            try:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096).decode('utf-8', errors='replace')
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
        """
        发送命令到设备

        参数:
            command: 要发送的命令字符串
        """
        if self.channel and self._connected:
            self.channel.send(command + (self.line_ending or ''))
            time.sleep(0.3)  # 等待命令执行

    def send_raw(self, data: str):
        """发送原始数据（不追加换行），用于交互式确认等场景"""
        if self.channel and self._connected:
            self.channel.send(data)

    def disconnect(self):
        """断开连接"""
        was_connected = self._connected
        self._running = False
        self._connected = False
        if self.channel:
            try:
                self.channel.close()
            except Exception:
                pass
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        if was_connected:
            self.connection_lost.emit()

    def is_connected(self) -> bool:
        """检查连接状态"""
        return (self._connected and
                self.channel is not None and
                self.channel.active)

    def get_output(self) -> str:
        """获取当前缓冲区内容并清空"""
        with self._lock:
            output = self._buffer
            self._buffer = ""
            return output

    def get_output_block(self, timeout: float = 2.0) -> str:
        """
        阻塞获取输出

        持续读取直到timeout秒内没有新数据

        参数:
            timeout: 超时时间（秒）
        返回:
            读取到的所有输出内容
        """
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

    def resize_terminal(self, width: int, height: int):
        """调整终端窗口大小"""
        if self.channel:
            self.channel.resize_pty(width=width, height=height)

    def info(self) -> str:
        return f"SSH {self._host}"
