"""
串口控制对话框 - Console 现场调试常用操作

对标 MobaXterm 在 Console 场景的实用功能：
    发送 Break（进 BootROM）、DTR/RTS 电平控制、运行时切换波特率、
    调整字符发送间隔与行结束符、手动重连。
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QComboBox, QPushButton, QSpinBox, QLabel, QCheckBox, QMessageBox
)
from PyQt6.QtCore import QTimer

from config.presets import BAUDRATES, LINE_ENDING_OPTIONS, CONSOLE_HINTS


class SerialControlDialog(QDialog):
    """
    串口控制面板

    参数:
        connector: SerialConnector 实例
    """

    def __init__(self, parent=None, connector=None, session_name: str = ""):
        super().__init__(parent)
        self.connector = connector
        self.setWindowTitle(f"串口控制 - {session_name}" if session_name else "串口控制")
        self.setMinimumWidth(460)
        self.break_timer = None
        self.break_count = 0
        self.init_ui()
        self.refresh_state()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)

        # 当前状态
        status_group = QGroupBox("当前状态")
        status_layout = QFormLayout()
        self.status_label = QLabel("-")
        self.port_label = QLabel("-")
        status_layout.addRow("连接:", self.status_label)
        status_layout.addRow("端口:", self.port_label)
        status_group.setLayout(status_layout)
        layout.addWidget(status_group)

        # Break
        break_group = QGroupBox("Break 信号（进 BootROM / 打断启动）")
        break_layout = QVBoxLayout()

        row = QHBoxLayout()
        row.addWidget(QLabel("单次时长:"))
        self.break_duration = QSpinBox()
        self.break_duration.setRange(50, 3000)
        self.break_duration.setSingleStep(50)
        self.break_duration.setValue(250)
        self.break_duration.setSuffix(" ms")
        row.addWidget(self.break_duration)
        row.addStretch()
        break_layout.addLayout(row)

        btn_row = QHBoxLayout()
        self.btn_break = QPushButton("发送 Break")
        self.btn_break.clicked.connect(self.send_break)
        btn_row.addWidget(self.btn_break)

        self.btn_break_burst = QPushButton("连续 Break（设备上电时连点）")
        self.btn_break_burst.setCheckable(True)
        self.btn_break_burst.toggled.connect(self.toggle_break_burst)
        btn_row.addWidget(self.btn_break_burst)
        break_layout.addLayout(btn_row)

        self.break_info = QLabel("提示：设备上电瞬间连点 Break 可进入 BootROM；部分设备需要先拉低 DTR。")
        self.break_info.setWordWrap(True)
        self.break_info.setStyleSheet("color: #98a1b5; font-size: 11px;")
        break_layout.addWidget(self.break_info)

        break_group.setLayout(break_layout)
        layout.addWidget(break_group)

        # 控制线
        ctrl_group = QGroupBox("控制线电平")
        ctrl_layout = QHBoxLayout()
        self.chk_dtr = QCheckBox("DTR 拉高")
        self.chk_dtr.toggled.connect(lambda v: self.apply_signal('dtr', v))
        self.chk_rts = QCheckBox("RTS 拉高")
        self.chk_rts.toggled.connect(lambda v: self.apply_signal('rts', v))
        ctrl_layout.addWidget(self.chk_dtr)
        ctrl_layout.addWidget(self.chk_rts)
        ctrl_layout.addStretch()
        ctrl_group.setLayout(ctrl_layout)
        layout.addWidget(ctrl_group)

        # 参数调整
        param_group = QGroupBox("运行中调整")
        param_layout = QFormLayout()

        self.baudrate_combo = QComboBox()
        self.baudrate_combo.setEditable(True)
        self.baudrate_combo.addItems([str(b) for b in BAUDRATES])
        param_layout.addRow("波特率:", self.baudrate_combo)

        self.line_ending_combo = QComboBox()
        for label, value in LINE_ENDING_OPTIONS:
            self.line_ending_combo.addItem(label, value)
        param_layout.addRow("行结束符:", self.line_ending_combo)

        self.char_delay_spin = QSpinBox()
        self.char_delay_spin.setRange(0, 200)
        self.char_delay_spin.setSuffix(" ms")
        self.char_delay_spin.setToolTip("老设备丢字符时调大")
        param_layout.addRow("字符间隔:", self.char_delay_spin)

        param_group.setLayout(param_layout)
        layout.addWidget(param_group)

        # 提示
        hint = QLabel("Console 排查提示：\n· " + "\n· ".join(CONSOLE_HINTS[:4]))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #98a1b5; font-size: 11px;")
        layout.addWidget(hint)

        # 底部按钮
        btn_row = QHBoxLayout()
        self.btn_reconnect = QPushButton("重新连接")
        self.btn_reconnect.clicked.connect(self.reconnect)
        btn_row.addWidget(self.btn_reconnect)
        btn_row.addStretch()

        btn_apply = QPushButton("应用参数")
        btn_apply.clicked.connect(self.apply_params)
        btn_row.addWidget(btn_apply)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        # 定时刷新状态
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_state)
        self.status_timer.start(1000)

    # ------------------------------------------------------------ 状态
    def refresh_state(self):
        """刷新连接状态与当前参数"""
        if self.connector is None:
            self.status_label.setText("无串口连接")
            self.setEnabled(False)
            return

        connected = self.connector.is_connected()
        self.status_label.setText("已连接" if connected else "已断开 / 重连中")
        self.status_label.setStyleSheet("color: #3fb950;" if connected else "color: #f85149;")

        params = self.connector.connection_params()
        self.port_label.setText(
            f"{params.get('port', '-')}  "
            f"{params.get('baudrate', '-')}-{params.get('bytesize', '-')}"
            f"{params.get('parity', '-')}{params.get('stopbits', '-')}"
        )
        self.baudrate_combo.setCurrentText(str(params.get('baudrate', 9600)))
        self.char_delay_spin.setValue(int(params.get('char_delay_ms', 0)))

    # ------------------------------------------------------------ 操作
    def send_break(self):
        """发送一次 Break"""
        if not self.connector or not self.connector.is_connected():
            QMessageBox.information(self, "未连接", "串口未连接，无法发送 Break。")
            return
        duration = self.break_duration.value() / 1000.0
        if self.connector.send_break(duration):
            self.break_count += 1
            self.break_info.setText(f"已发送 {self.break_count} 次 Break（{self.break_duration.value()} ms）")
        else:
            self.break_info.setText("发送 Break 失败，当前驱动可能不支持。")

    def toggle_break_burst(self, enabled: bool):
        """连续发送 Break（设备上电时连点）"""
        if enabled:
            if self.break_timer is None:
                self.break_timer = QTimer(self)
                self.break_timer.timeout.connect(self.send_break)
            self.break_count = 0
            self.break_timer.start(400)
            self.btn_break_burst.setText("停止连续 Break")
        else:
            if self.break_timer:
                self.break_timer.stop()
            self.btn_break_burst.setText("连续 Break（设备上电时连点）")

    def apply_signal(self, name: str, value: bool):
        """应用 DTR / RTS 电平"""
        if not self.connector:
            return
        if name == 'dtr':
            ok = self.connector.set_dtr(value)
        else:
            ok = self.connector.set_rts(value)
        if not ok:
            self.break_info.setText(f"设置 {name.upper()} 失败（驱动可能不支持）。")

    def apply_params(self):
        """应用运行中调整的参数"""
        if not self.connector:
            return
        try:
            baudrate = int(self.baudrate_combo.currentText().strip())
        except ValueError:
            QMessageBox.warning(self, "参数错误", "波特率必须是数字")
            return

        if baudrate != self.connector.connection_params().get('baudrate'):
            if self.connector.set_baudrate(baudrate):
                self.break_info.setText(f"已切换波特率到 {baudrate}")
            else:
                self.break_info.setText("切换波特率失败。")

        self.connector.set_line_ending(self.line_ending_combo.currentData())
        self.connector.set_char_delay(self.char_delay_spin.value())
        self.refresh_state()

    def reconnect(self):
        """手动重新打开串口"""
        if not self.connector:
            return
        if not hasattr(self.connector, 'reconnect_now'):
            QMessageBox.information(self, "不支持", "当前连接器不支持手动重连。")
            return
        if self.connector.reconnect_now():
            self.break_info.setText("已重新连接")
        else:
            self.break_info.setText("重新连接失败，请检查串口是否被其他程序占用。")
        self.refresh_state()

    def closeEvent(self, event):
        if self.break_timer:
            self.break_timer.stop()
        self.status_timer.stop()
        event.accept()
