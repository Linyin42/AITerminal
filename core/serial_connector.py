"""
串口连接器 - Console 串口连接（对标 MobaXterm 的兼容性）

相比基础实现，这里补齐了 Console 场景真正会用到的能力：

    1. 完整串口参数：波特率(含自定义)、数据位、校验位、停止位
    2. 流控：无 / RTS-CTS 硬件流控 / XON-XOFF 软件流控 / DSR-DTR
    3. DTR / RTS 电平控制（部分设备需要 DTR 拉低才能进 BootROM）
    4. Break 信号（打断设备启动，进 BootROM / ROMMON）
    5. 运行时切换波特率（设备启动过程中 console 速率可能变化）
    6. 行结束符可选（Console 默认 \\r，很多设备只认 CR）
    7. 字符发送间隔（慢速设备 / 老设备兼容）
    8. 自动重连（USB 转串口被拔插、设备重启导致端口消失后自动恢复）
    9. 端口枚举带描述，端口被占用等常见错误给出可读提示
"""
import time
import threading
from typing import Optional

import serial
from serial.tools import list_ports
from PyQt6.QtCore import pyqtSignal

from .connector import BaseConnector

# 常用波特率（含设备 Console 常见档位）
COMMON_BAUDRATES = [
    1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600,
    115200, 128000, 230400, 460800, 921600,
]

PARITY_MAP = {
    'N': serial.PARITY_NONE,
    'E': serial.PARITY_EVEN,
    'O': serial.PARITY_ODD,
    'M': serial.PARITY_MARK,
    'S': serial.PARITY_SPACE,
}

STOPBITS_MAP = {
    1: serial.STOPBITS_ONE,
    1.5: serial.STOPBITS_ONE_POINT_FIVE,
    2: serial.STOPBITS_TWO,
}

# 流控方式
FLOW_NONE = 'none'
FLOW_RTSCTS = 'rtscts'
FLOW_XONXOFF = 'xonxoff'
FLOW_DSRDTR = 'dsrdtr'

LINE_ENDINGS = {
    'CR (\\r)': '\r',
    'LF (\\n)': '\n',
    'CRLF (\\r\\n)': '\r\n',
    '无': '',
}


class SerialConnector(BaseConnector):
    """
    串口连接器

    信号补充:
        reconnecting(int): 第 N 次尝试自动重连
        reconnected():     重连成功
        reconnect_failed(str): 重连失败并放弃
    """

    reconnecting = pyqtSignal(int)
    reconnected = pyqtSignal()
    reconnect_failed = pyqtSignal(str)

    #: Console 默认用 CR 结尾（多数网络设备只认 0x0D）
    line_ending = '\r'

    def __init__(self):
        super().__init__()
        self.ser: Optional[serial.Serial] = None
        self._connected = False
        self._running = False
        self._read_thread = None
        self._reconnect_thread = None
        self._buffer = ""
        self._lock = threading.Lock()
        self._port = ""
        self._params: dict = {}
        self._char_delay = 0.0

    # ------------------------------------------------------------ 连接
    def connect(self, port: str, baudrate: int = 9600,
                bytesize: int = 8, parity: str = 'N',
                stopbits: float = 1, timeout: float = 0.1,
                flow: str = FLOW_NONE,
                dtr: Optional[bool] = None, rts: Optional[bool] = None,
                line_ending: str = '\r', char_delay_ms: int = 0,
                auto_reconnect: bool = True, reconnect_interval: float = 2.0,
                max_reconnect: int = 0, **kwargs) -> bool:
        """
        建立串口连接

        参数:
            port: 串口号，如 'COM3' 或 '/dev/ttyUSB0'
            baudrate: 波特率，默认9600
            bytesize: 数据位，默认8
            parity: 校验位，N/E/O/M/S，默认'N'无校验
            stopbits: 停止位，1 / 1.5 / 2
            timeout: 读取超时，默认0.1秒
            flow: 流控，none / rtscts / xonxoff / dsrdtr
            dtr: 打开后 DTR 电平，None 表示不干预
            rts: 打开后 RTS 电平，None 表示不干预
            line_ending: 命令结尾符，Console 默认 '\\r'
            char_delay_ms: 逐字符发送间隔（慢设备兼容）
            auto_reconnect: 端口消失后是否自动重连
            reconnect_interval: 重连间隔秒数
            max_reconnect: 最大重连次数，0 表示一直尝试
        """
        self._params = {
            'port': port, 'baudrate': int(baudrate), 'bytesize': int(bytesize),
            'parity': parity, 'stopbits': stopbits, 'timeout': timeout,
            'flow': flow, 'dtr': dtr, 'rts': rts,
            'line_ending': line_ending, 'char_delay_ms': int(char_delay_ms),
            'auto_reconnect': bool(auto_reconnect),
            'reconnect_interval': float(reconnect_interval),
            'max_reconnect': int(max_reconnect),
        }
        self.line_ending = line_ending
        self._char_delay = max(0, int(char_delay_ms)) / 1000.0
        self._port = port

        if not self._open():
            return False

        self._connected = True
        self._running = True
        self._start_read_loop()
        time.sleep(0.3)
        return True

    def _open(self) -> bool:
        """按当前参数打开串口"""
        params = self._params
        flow = params.get('flow', FLOW_NONE)
        try:
            self.ser = serial.Serial(
                port=params['port'],
                baudrate=params['baudrate'],
                bytesize=params['bytesize'],
                parity=PARITY_MAP.get(params['parity'], serial.PARITY_NONE),
                stopbits=STOPBITS_MAP.get(params['stopbits'], serial.STOPBITS_ONE),
                timeout=params.get('timeout', 0.1),
                write_timeout=2,
                rtscts=(flow == FLOW_RTSCTS),
                xonxoff=(flow == FLOW_XONXOFF),
                dsrdtr=(flow == FLOW_DSRDTR),
            )
            # 打开瞬间清一次缓冲，避免把上次残留数据当成本次输出
            try:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
            except Exception:
                pass

            # 初始 DTR / RTS 电平
            if params.get('dtr') is not None:
                self.set_dtr(bool(params['dtr']))
            if params.get('rts') is not None:
                self.set_rts(bool(params['rts']))
            return True

        except serial.SerialException as e:
            self.error_occurred.emit(self._explain_error(e))
            return False
        except Exception as e:
            self.error_occurred.emit(f"串口打开失败: {e}")
            return False

    @staticmethod
    def _explain_error(exc: Exception) -> str:
        """把 pyserial 的报错翻译成人话"""
        text = str(exc)
        low = text.lower()
        if 'access is denied' in low or 'permission' in low or '被占用' in text:
            return (f"串口被占用或没有权限：{text}\n"
                    "请检查是否还有其他终端软件（MobaXterm / SecureCRT / putty）打开了同一个串口。")
        if 'could not open port' in low and 'filenotfound' in low.replace(' ', ''):
            return f"串口不存在：{text}\n请确认 USB 转串口线已插好，并在端口列表里刷新。"
        if 'file not found' in low or 'no such file' in low:
            return f"串口不存在：{text}"
        return f"串口打开失败: {text}"

    # ------------------------------------------------------------ 读线程
    def _start_read_loop(self):
        self._read_thread = threading.Thread(
            target=self._read_loop,
            name=f"serial-read-{self._port}",
            daemon=True
        )
        self._read_thread.start()

    def _read_loop(self):
        """后台读取串口输出；端口异常时触发自动重连"""
        while self._running:
            try:
                if self.ser is None or not self.ser.is_open:
                    raise serial.SerialException("端口已关闭")
                waiting = self.ser.in_waiting
                if waiting:
                    data = self.ser.read(waiting).decode('utf-8', errors='replace')
                    if data:
                        with self._lock:
                            self._buffer += data
                        self.output_received.emit(data)
                        self._notify_output(data)
                else:
                    time.sleep(0.02)
            except Exception as e:
                # 拔线 / 设备重启 / 端口被系统回收都会走到这里
                if self._running:
                    self._on_serial_error(e)
                return

        if self._connected:
            self._connected = False
            self.connection_lost.emit()

    def _on_serial_error(self, exc: Exception):
        """串口异常处理：能重连就重连，否则上报断开"""
        was_connected = self._connected
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass

        if self._params.get('auto_reconnect'):
            self.error_occurred.emit(f"串口中断: {exc}，开始自动重连…")
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop,
                name=f"serial-reconnect-{self._port}",
                daemon=True
            )
            self._reconnect_thread.start()
            return

        self._connected = False
        if was_connected:
            self.connection_lost.emit()

    def _reconnect_loop(self):
        """按间隔不断尝试重新打开串口"""
        interval = self._params.get('reconnect_interval', 2.0)
        max_tries = self._params.get('max_reconnect', 0)
        attempt = 0

        while self._running:
            attempt += 1
            if max_tries and attempt > max_tries:
                self._connected = False
                self.reconnect_failed.emit(f"已尝试 {max_tries} 次仍无法重连 {self._port}")
                self.connection_lost.emit()
                return

            self.reconnecting.emit(attempt)
            time.sleep(interval)

            if not self._running:
                return
            if self._open():
                self._connected = True
                self.reconnected.emit()
                self._start_read_loop()
                return

    # ------------------------------------------------------------ 发送
    def send(self, command: str):
        """发送命令（自动追加行结束符，支持逐字符节奏）"""
        if not (self.ser and self.ser.is_open and self._connected):
            return
        data = command + (self.line_ending or '')
        self._write(data)

    def send_raw(self, data: str):
        """发送原始数据（Ctrl+C、Tab、Enter 等），不追加行结束符"""
        if self.ser and self.ser.is_open and self._connected:
            self._write(data)

    def _write(self, data: str):
        payload = data.encode('utf-8', errors='replace')
        try:
            if self._char_delay > 0:
                for byte in payload:
                    self.ser.write(bytes([byte]))
                    time.sleep(self._char_delay)
            else:
                self.ser.write(payload)
            self.ser.flush()
        except Exception as e:
            self._on_serial_error(e)
            return
        time.sleep(0.05)

    def send_break(self, duration: float = 0.25) -> bool:
        """发送 Break 信号（打断设备启动，进 BootROM/ROMMON）"""
        if not (self.ser and self.ser.is_open):
            return False
        try:
            self.ser.send_break(duration)
            return True
        except Exception:
            # 部分驱动不支持 send_break，退化为手动拉低 TX
            try:
                self.ser.break_condition = True
                time.sleep(duration)
                self.ser.break_condition = False
                return True
            except Exception as e:
                self.error_occurred.emit(f"发送 Break 失败: {e}")
                return False

    # ------------------------------------------------------------ 串口控制
    def set_dtr(self, value: bool) -> bool:
        if not (self.ser and self.ser.is_open):
            return False
        try:
            self.ser.dtr = bool(value)
            return True
        except Exception:
            try:
                self.ser.setDTR(bool(value))
                return True
            except Exception:
                return False

    def set_rts(self, value: bool) -> bool:
        if not (self.ser and self.ser.is_open):
            return False
        try:
            self.ser.rts = bool(value)
            return True
        except Exception:
            try:
                self.ser.setRTS(bool(value))
                return True
            except Exception:
                return False

    def set_baudrate(self, baudrate: int) -> bool:
        """运行时切换波特率（设备启动后 console 速度可能变化）"""
        if not (self.ser and self.ser.is_open):
            return False
        try:
            self.ser.baudrate = int(baudrate)
            self._params['baudrate'] = int(baudrate)
            return True
        except Exception as e:
            self.error_occurred.emit(f"切换波特率失败: {e}")
            return False

    def set_line_ending(self, ending: str):
        super().set_line_ending(ending)
        self._params['line_ending'] = ending

    def set_char_delay(self, delay_ms: int):
        """设置逐字符发送间隔"""
        self._char_delay = max(0, int(delay_ms)) / 1000.0
        self._params['char_delay_ms'] = int(delay_ms)

    def supports_serial_control(self) -> bool:
        return True

    def reconnect_now(self) -> bool:
        """手动触发一次重连（界面"重新连接"按钮）"""
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self._running = False
        time.sleep(0.1)
        self._running = True
        if self._open():
            self._connected = True
            self._start_read_loop()
            return True
        return False

    # ------------------------------------------------------------ 状态
    def disconnect(self):
        """断开连接"""
        was_connected = self._connected
        self._running = False
        self._connected = False
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        if was_connected:
            self.connection_lost.emit()

    def is_connected(self) -> bool:
        """检查连接状态"""
        return bool(self._connected and self.ser is not None and self.ser.is_open)

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
        port = self._port or self._params.get('port', '')
        baud = self._params.get('baudrate', '')
        return f"Serial {port} @ {baud}"

    # ------------------------------------------------------------ 端口枚举
    @staticmethod
    def list_ports_detailed():
        """
        枚举本机串口（带描述）

        返回: [{"device": "COM3", "description": "USB-SERIAL CH340", "hwid": "USB VID:PID=1A86:7523"}]
        """
        result = []
        try:
            for port in list_ports.comports():
                result.append({
                    "device": port.device,
                    "description": port.description or "",
                    "hwid": port.hwid or "",
                    "manufacturer": getattr(port, "manufacturer", "") or "",
                })
        except Exception:
            pass
        return result

    @staticmethod
    def list_ports():
        """列出本机可用串口号"""
        return [p["device"] for p in SerialConnector.list_ports_detailed()]
