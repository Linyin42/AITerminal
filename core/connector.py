"""
连接基类 - 定义所有连接器的通用接口
"""
from abc import ABC, abstractmethod
from PyQt6.QtCore import QObject, pyqtSignal


class BaseConnector(QObject):
    """
    连接基类

    信号:
        output_received(str): 收到设备输出时发射
        connection_lost(): 连接断开时发射
        error_occurred(str): 发生错误时发射
    """
    output_received = pyqtSignal(str)
    connection_lost = pyqtSignal()
    error_occurred = pyqtSignal(str)

    #: 发送命令时追加的行结束符（SSH/Telnet 用 \n，Console 通常用 \r）
    line_ending = '\n'

    def __init__(self):
        super().__init__()
        # 非 Qt 的输出监听器：供批量下发/巡检判断"命令是否执行完"
        # 不走信号是因为信号在子线程里是排队投递的，没有事件循环就收不到
        self._output_listeners = []

    # ---------------------- 输出监听（线程安全，不消耗缓冲区） ----------------------
    def add_output_listener(self, callback):
        """注册输出监听器（在读线程中被同步调用）"""
        if callback not in self._output_listeners:
            self._output_listeners.append(callback)

    def remove_output_listener(self, callback):
        """注销输出监听器"""
        try:
            self._output_listeners.remove(callback)
        except ValueError:
            pass

    def _notify_output(self, data: str):
        """子类在读线程里调用：通知所有监听器"""
        if not data or not self._output_listeners:
            return
        for callback in list(self._output_listeners):
            try:
                callback(data)
            except Exception:
                pass

    @abstractmethod
    def connect(self, **kwargs) -> bool:
        """建立连接"""
        pass

    @abstractmethod
    def send(self, command: str):
        """发送命令到设备"""
        pass

    @abstractmethod
    def disconnect(self):
        """断开连接"""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """检查连接状态"""
        pass

    @abstractmethod
    def get_output(self) -> str:
        """获取当前输出缓冲区内容"""
        pass

    @abstractmethod
    def get_output_block(self, timeout: float = 2.0) -> str:
        """阻塞获取输出，等待超时无新数据后返回"""
        pass

    # ------------------------------------------------------------------
    # 以下为可选能力的默认实现，子类可按需覆盖
    # ------------------------------------------------------------------
    def send_raw(self, data: str):
        """发送原始数据（不追加换行、不等待）"""
        self.send(data)

    def resize_terminal(self, width: int, height: int):
        """调整远端伪终端大小（仅 SSH 有效）"""
        return None

    def info(self) -> str:
        """返回连接的一句话描述，用于状态栏/标签页提示"""
        return self.__class__.__name__

    # ---------------------- 串口/Console 专用能力（默认不支持） ----------------------
    def send_break(self, duration: float = 0.25) -> bool:
        """
        发送 Break 信号

        Console 场景常用：打断设备启动进入 BootROM/ROMMON。
        """
        return False

    def set_dtr(self, value: bool) -> bool:
        """控制 DTR 电平（部分设备需要 DTR 拉低才能进 BootROM）"""
        return False

    def set_rts(self, value: bool) -> bool:
        """控制 RTS 电平"""
        return False

    def set_baudrate(self, baudrate: int) -> bool:
        """运行时切换波特率（设备启动后 console 速度可能变化）"""
        return False

    def supports_serial_control(self) -> bool:
        """是否支持串口控制能力（DTR/RTS/Break/改波特率）"""
        return False

    def set_line_ending(self, ending: str):
        """设置命令结尾符，如 '\\r' / '\\n' / '\\r\\n'；空字符串表示不追加"""
        if ending is None:
            return
        self.line_ending = ending

    def connection_params(self) -> dict:
        """返回当前连接参数（供界面显示/重新连接）"""
        return dict(getattr(self, '_params', {}) or {})
