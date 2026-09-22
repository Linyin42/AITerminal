"""
连接对话框 - 配置新连接

串口页按 Console 实际使用场景做了完整参数支持：
波特率/数据位/校验/停止位/流控/DTR/RTS/行结束符/字符间隔/自动重连/本地回显
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QComboBox, QPushButton, QSpinBox, QDoubleSpinBox,
    QLabel, QGroupBox, QMessageBox, QFileDialog, QCheckBox
)

from config.presets import (
    PRESET_CONNECTIONS, PROTOCOL_DEFAULTS, CONSOLE_PRESETS,
    BAUDRATES, PARITY_OPTIONS, STOPBIT_OPTIONS, DATABITS_OPTIONS,
    FLOW_OPTIONS, LINE_ENDING_OPTIONS, CONSOLE_HINTS,
)
from config.settings import Settings


class ConnectDialog(QDialog):
    """连接配置对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建连接")
        self.setMinimumWidth(520)
        self.settings = Settings()
        self.serial_defaults = self.settings.serial_defaults()
        self.init_ui()
        self.load_last()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)

        # 预设
        preset_group = QGroupBox("快速预设")
        preset_layout = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("— 选择预设 —")
        self.preset_combo.addItems(list(PRESET_CONNECTIONS.keys()))
        self.preset_combo.currentTextChanged.connect(self.on_preset_selected)
        preset_layout.addWidget(self.preset_combo)
        preset_group.setLayout(preset_layout)
        layout.addWidget(preset_group)

        # 基本信息
        basic_group = QGroupBox("基本信息")
        basic_layout = QFormLayout()

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("例如：核心交换机")
        basic_layout.addRow("名称:", self.name_input)

        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(["SSH", "Telnet", "Serial"])
        self.protocol_combo.currentTextChanged.connect(self.on_protocol_changed)
        basic_layout.addRow("协议:", self.protocol_combo)

        basic_group.setLayout(basic_layout)
        layout.addWidget(basic_group)

        # 连接参数（SSH / Telnet）
        self.conn_group = QGroupBox("连接参数")
        self.conn_layout = QFormLayout()

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("10.0.0.1")
        self.conn_layout.addRow("主机:", self.host_input)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(22)
        self.conn_layout.addRow("端口:", self.port_spin)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("admin")
        self.conn_layout.addRow("用户名:", self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.conn_layout.addRow("密码:", self.password_input)

        # 密钥认证
        key_row = QHBoxLayout()
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("留空则使用密码认证")
        btn_key = QPushButton("浏览…")
        btn_key.clicked.connect(self.choose_key_file)
        key_row.addWidget(self.key_input)
        key_row.addWidget(btn_key)
        self.conn_layout.addRow("密钥文件:", key_row)

        self.conn_group.setLayout(self.conn_layout)
        layout.addWidget(self.conn_group)

        # 串口参数（Console）
        self.serial_group = QGroupBox("串口参数（Console）")
        serial_layout = QFormLayout()

        # Console 预设
        self.console_preset = QComboBox()
        self.console_preset.addItem("— 选择 Console 预设 —")
        self.console_preset.addItems(list(CONSOLE_PRESETS.keys()))
        self.console_preset.currentTextChanged.connect(self.on_console_preset_selected)
        serial_layout.addRow("常用预设:", self.console_preset)

        # 端口
        port_row = QHBoxLayout()
        self.serial_port = QComboBox()
        self.serial_port.setEditable(True)
        self.serial_port.setMinimumWidth(220)
        btn_refresh = QPushButton("刷新")
        btn_refresh.setToolTip("重新枚举本机串口")
        btn_refresh.clicked.connect(self.refresh_serial_ports)
        port_row.addWidget(self.serial_port)
        port_row.addWidget(btn_refresh)
        serial_layout.addRow("串口:", port_row)

        # 波特率
        self.baudrate_combo = QComboBox()
        self.baudrate_combo.setEditable(True)
        self.baudrate_combo.addItems([str(b) for b in BAUDRATES])
        serial_layout.addRow("波特率:", self.baudrate_combo)

        # 数据位 / 校验位 / 停止位
        frame_row = QHBoxLayout()
        self.databits_combo = QComboBox()
        self.databits_combo.addItems([str(b) for b in DATABITS_OPTIONS])
        self.parity_combo = QComboBox()
        for label, value in PARITY_OPTIONS:
            self.parity_combo.addItem(label, value)
        self.stopbits_combo = QComboBox()
        for label, value in STOPBIT_OPTIONS:
            self.stopbits_combo.addItem(label, value)
        frame_row.addWidget(QLabel("数据位"))
        frame_row.addWidget(self.databits_combo)
        frame_row.addWidget(QLabel("校验"))
        frame_row.addWidget(self.parity_combo)
        frame_row.addWidget(QLabel("停止位"))
        frame_row.addWidget(self.stopbits_combo)
        frame_row.addStretch()
        serial_layout.addRow("帧格式:", frame_row)

        # 流控
        self.flow_combo = QComboBox()
        for label, value in FLOW_OPTIONS:
            self.flow_combo.addItem(label, value)
        serial_layout.addRow("流控:", self.flow_combo)

        # DTR / RTS
        signal_row = QHBoxLayout()
        self.dtr_combo = QComboBox()
        self.dtr_combo.addItem("不干预", None)
        self.dtr_combo.addItem("拉高 (High)", True)
        self.dtr_combo.addItem("拉低 (Low)", False)
        self.rts_combo = QComboBox()
        self.rts_combo.addItem("不干预", None)
        self.rts_combo.addItem("拉高 (High)", True)
        self.rts_combo.addItem("拉低 (Low)", False)
        signal_row.addWidget(QLabel("DTR"))
        signal_row.addWidget(self.dtr_combo)
        signal_row.addWidget(QLabel("RTS"))
        signal_row.addWidget(self.rts_combo)
        signal_row.addStretch()
        serial_layout.addRow("控制线:", signal_row)

        # 行结束符
        self.line_ending_combo = QComboBox()
        for label, value in LINE_ENDING_OPTIONS:
            self.line_ending_combo.addItem(label, value)
        serial_layout.addRow("行结束符:", self.line_ending_combo)

        # 字符发送间隔
        self.char_delay_spin = QSpinBox()
        self.char_delay_spin.setRange(0, 200)
        self.char_delay_spin.setSuffix(" ms")
        self.char_delay_spin.setToolTip("逐字符发送的间隔，老设备或长线缆丢字符时调大")
        serial_layout.addRow("字符间隔:", self.char_delay_spin)

        # 自动重连
        reconnect_row = QHBoxLayout()
        self.chk_auto_reconnect = QCheckBox("自动重连")
        self.chk_auto_reconnect.setToolTip("USB 转串口被拔插或设备重启后自动恢复会话")
        self.reconnect_spin = QDoubleSpinBox()
        self.reconnect_spin.setRange(0.5, 30.0)
        self.reconnect_spin.setSingleStep(0.5)
        self.reconnect_spin.setValue(2.0)
        self.reconnect_spin.setSuffix(" 秒")
        reconnect_row.addWidget(self.chk_auto_reconnect)
        reconnect_row.addWidget(QLabel("间隔"))
        reconnect_row.addWidget(self.reconnect_spin)
        reconnect_row.addStretch()
        serial_layout.addRow("断线恢复:", reconnect_row)

        # 本地回显
        self.chk_local_echo = QCheckBox("本地回显（设备不回显时勾选）")
        serial_layout.addRow("", self.chk_local_echo)

        self.serial_group.setLayout(serial_layout)
        self.serial_group.setVisible(False)
        layout.addWidget(self.serial_group)

        # Console 排查提示
        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #98a1b5; font-size: 11px;")
        self.hint_label.setText("Console 常见问题：" + CONSOLE_HINTS[0])
        self.hint_label.setVisible(False)
        layout.addWidget(self.hint_label)

        # 记住密码
        self.chk_remember = QCheckBox("记住该连接（含密码，下次自动填充）")
        self.chk_remember.setToolTip(
            "勾选后，本次填写的连接参数（包括密码）会保存到本机配置文件\n"
            "%USERPROFILE%\\.aiterminal\\settings.json 中，下次打开对话框自动填充。\n"
            "密码以明文存储，共用电脑请勿勾选。"
        )
        layout.addWidget(self.chk_remember)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("连接")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.on_accept)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

        self.refresh_serial_ports()
        self.apply_serial_defaults()

    # ------------------------------------------------------------ 串口
    def refresh_serial_ports(self):
        """枚举本机串口，显示为 COM3 — USB-SERIAL CH340"""
        current = self.serial_port.currentText()
        self.serial_port.clear()
        ports = self.list_serial_ports()
        for item in ports:
            label = item["device"]
            if item["description"]:
                label = f"{item['device']} — {item['description']}"
            self.serial_port.addItem(label, item["device"])
        if not ports:
            self.serial_port.addItem("未检测到串口", "")

        # 恢复之前选中的端口
        if current:
            for index in range(self.serial_port.count()):
                if self.serial_port.itemData(index) == current or \
                        self.serial_port.itemText(index) == current:
                    self.serial_port.setCurrentIndex(index)
                    break
        if not self.serial_port.currentText():
            self.serial_port.setCurrentText("COM3")

    @staticmethod
    def list_serial_ports():
        """枚举本机串口（带描述）"""
        try:
            from serial.tools import list_ports
            return [{
                "device": p.device,
                "description": p.description or "",
            } for p in list_ports.comports()]
        except Exception:
            return []

    def current_serial_port(self) -> str:
        """取当前选中的串口号（下拉项形如 'COM3 — USB-SERIAL CH340'）"""
        text = self.serial_port.currentText().strip()
        index = self.serial_port.findText(text)
        if index >= 0:
            data = self.serial_port.itemData(index)
            if data:
                return str(data)
        if "—" in text:
            return text.split("—")[0].strip()
        return text

    def apply_serial_defaults(self):
        """填入上次使用的串口参数"""
        d = self.serial_defaults
        self.baudrate_combo.setCurrentText(str(d.get("baudrate", 9600)))
        self.databits_combo.setCurrentText(str(d.get("bytesize", 8)))
        self.set_combo_by_data(self.parity_combo, d.get("parity", "N"))
        self.set_combo_by_data(self.stopbits_combo, d.get("stopbits", 1))
        self.set_combo_by_data(self.flow_combo, d.get("flow", "none"))
        self.set_combo_by_data(self.line_ending_combo, d.get("line_ending", "\r"))
        self.char_delay_spin.setValue(int(d.get("char_delay_ms", 0)))
        self.chk_auto_reconnect.setChecked(bool(d.get("auto_reconnect", True)))
        self.chk_local_echo.setChecked(bool(d.get("local_echo", True)))

    @staticmethod
    def set_combo_by_data(combo: QComboBox, value):
        """按 userData 选中下拉项，找不到就保持默认"""
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return
        # 数值型比较（停止位 1 vs 1.0）
        for index in range(combo.count()):
            try:
                if float(combo.itemData(index)) == float(value):
                    combo.setCurrentIndex(index)
                    return
            except (TypeError, ValueError):
                continue

    def on_console_preset_selected(self, name: str):
        """应用 Console 预设"""
        preset = CONSOLE_PRESETS.get(name)
        if not preset:
            return
        self.baudrate_combo.setCurrentText(str(preset.get("baudrate", 9600)))
        self.databits_combo.setCurrentText(str(preset.get("bytesize", 8)))
        self.set_combo_by_data(self.parity_combo, preset.get("parity", "N"))
        self.set_combo_by_data(self.stopbits_combo, preset.get("stopbits", 1))
        self.set_combo_by_data(self.flow_combo, preset.get("flow", "none"))
        self.set_combo_by_data(self.line_ending_combo, preset.get("line_ending", "\r"))
        self.char_delay_spin.setValue(int(preset.get("char_delay_ms", 0)))
        self.set_combo_by_data(self.dtr_combo, preset.get("dtr", None))
        self.set_combo_by_data(self.rts_combo, preset.get("rts", None))
        if preset.get("note"):
            self.hint_label.setText("预设说明：" + preset["note"])

    # ------------------------------------------------------------ 交互
    def choose_key_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择私钥文件", "", "所有文件 (*)"
        )
        if path:
            self.key_input.setText(path)

    def on_preset_selected(self, name: str):
        """应用连接预设"""
        preset = PRESET_CONNECTIONS.get(name)
        if not preset:
            return
        self.name_input.setText(preset.get('name', name))
        self.protocol_combo.setCurrentText(preset.get('protocol', 'SSH'))
        self.host_input.setText(preset.get('host', ''))
        self.port_spin.setValue(preset.get('port', 22))
        self.username_input.setText(preset.get('username', ''))
        if preset.get('serial_port'):
            self.serial_port.setCurrentText(preset['serial_port'])
        if preset.get('baudrate'):
            self.baudrate_combo.setCurrentText(str(preset['baudrate']))

    def on_protocol_changed(self, protocol: str):
        """协议切换"""
        is_serial = (protocol == "Serial")
        self.conn_group.setVisible(not is_serial)
        self.serial_group.setVisible(is_serial)
        self.hint_label.setVisible(is_serial)
        defaults = PROTOCOL_DEFAULTS.get(protocol, {})
        if 'port' in defaults:
            self.port_spin.setValue(defaults['port'])
        # 切到串口默认勾上本地回显 —— 串口/Console 设备多不回显你敲的字符，
        # 不开的话会以为"输入没反应"。用户可在下方手动取消。
        if is_serial and hasattr(self, "chk_local_echo"):
            self.chk_local_echo.setChecked(True)

    def load_last(self):
        """载入上次使用的连接参数（含密码，若上次勾了"记住"）"""
        last = self.settings.get_last_connection()
        if not last:
            return
        self.name_input.setText(last.get('name', ''))
        self.protocol_combo.setCurrentText(last.get('protocol', 'SSH'))
        self.host_input.setText(last.get('host', ''))
        self.username_input.setText(last.get('username', ''))
        self.port_spin.setValue(last.get('port', 22))
        if last.get('serial_port'):
            self.serial_port.setCurrentText(last['serial_port'])
        # 密码：只在"记住"过的时候才有存，有就回填
        # （之前漏了这一步 —— 勾了记住也不会自动填充，白让用户重输）
        if last.get('password'):
            self.password_input.setText(last['password'])
        key_file = last.get('key_filename', '')
        if key_file:
            self.key_input.setText(key_file)
        self.chk_remember.setChecked(bool(last.get('remember')))

    # ------------------------------------------------------------ 校验
    def on_accept(self):
        """校验后接受"""
        protocol = self.protocol_combo.currentText()
        if protocol == "Serial":
            if not self.current_serial_port():
                QMessageBox.warning(self, "参数不完整", "请选择或填写串口号（如 COM3）")
                return
        else:
            if not self.host_input.text().strip():
                QMessageBox.warning(self, "参数不完整", "请填写主机地址")
                return
        self.accept()

    def set_config(self, config: dict):
        """用一份连接配置回填整个对话框（供 Vault 的“连接/编辑”复用）"""
        if not config:
            return
        self.name_input.setText(config.get('name', ''))
        self.protocol_combo.setCurrentText(config.get('protocol', 'SSH'))
        self.on_protocol_changed(self.protocol_combo.currentText())
        self.host_input.setText(config.get('host', ''))
        self.port_spin.setValue(int(config.get('port', 22) or 22))
        self.username_input.setText(config.get('username', ''))
        key_file = config.get('key_filename', '')
        if key_file:
            self.key_input.setText(key_file)
        serial_port = config.get('serial_port') or config.get('host', '')
        if serial_port:
            self.serial_port.setCurrentText(serial_port)
        if config.get('baudrate'):
            self.baudrate_combo.setCurrentText(str(config['baudrate']))
        if config.get('bytesize'):
            self.databits_combo.setCurrentText(str(config['bytesize']))
        if config.get('parity') is not None:
            self.set_combo_by_data(self.parity_combo, config['parity'])
        if config.get('stopbits') is not None:
            self.set_combo_by_data(self.stopbits_combo, config['stopbits'])
        if config.get('flow'):
            self.set_combo_by_data(self.flow_combo, config['flow'])
        if config.get('dtr') is not None:
            self.set_combo_by_data(self.dtr_combo, config['dtr'])
        if config.get('rts') is not None:
            self.set_combo_by_data(self.rts_combo, config['rts'])
        if config.get('line_ending'):
            self.set_combo_by_data(self.line_ending_combo, config['line_ending'])
        if config.get('char_delay_ms') is not None:
            self.char_delay_spin.setValue(int(config['char_delay_ms']))
        if config.get('auto_reconnect') is not None:
            self.chk_auto_reconnect.setChecked(bool(config['auto_reconnect']))
        if config.get('reconnect_interval'):
            self.reconnect_spin.setValue(float(config['reconnect_interval']))
        if config.get('local_echo') is not None:
            self.chk_local_echo.setChecked(bool(config['local_echo']))
        # 配置里带密码就回填（设备库存的是脱敏配置，通常没有；上次连接可能有）
        if config.get('password'):
            self.password_input.setText(config['password'])
        self.chk_remember.setChecked(bool(config.get('remember', False)))

    def get_config(self) -> dict:
        """获取配置"""
        protocol = self.protocol_combo.currentText()
        config = {
            'name': self.name_input.text().strip() or "新建连接",
            'protocol': protocol,
            'remember': self.chk_remember.isChecked(),
        }

        if protocol == "Serial":
            try:
                baudrate = int(self.baudrate_combo.currentText().strip())
            except ValueError:
                baudrate = 9600
            config.update({
                'serial_port': self.current_serial_port(),
                'host': self.current_serial_port(),
                'baudrate': baudrate,
                'bytesize': int(self.databits_combo.currentText()),
                'parity': self.parity_combo.currentData(),
                'stopbits': self.stopbits_combo.currentData(),
                'flow': self.flow_combo.currentData(),
                'dtr': self.dtr_combo.currentData(),
                'rts': self.rts_combo.currentData(),
                'line_ending': self.line_ending_combo.currentData(),
                'char_delay_ms': self.char_delay_spin.value(),
                'auto_reconnect': self.chk_auto_reconnect.isChecked(),
                'reconnect_interval': self.reconnect_spin.value(),
                'local_echo': self.chk_local_echo.isChecked(),
            })
            # 记住串口参数，下次打开自动填
            self.settings.set_serial_defaults({
                'baudrate': config['baudrate'],
                'bytesize': config['bytesize'],
                'parity': config['parity'],
                'stopbits': config['stopbits'],
                'flow': config['flow'],
                'line_ending': config['line_ending'],
                'char_delay_ms': config['char_delay_ms'],
                'auto_reconnect': config['auto_reconnect'],
                'local_echo': config['local_echo'],
            })
        else:
            config['host'] = self.host_input.text().strip()
            config['port'] = self.port_spin.value()
            config['username'] = self.username_input.text().strip()
            config['password'] = self.password_input.text()
            key_file = self.key_input.text().strip()
            if key_file:
                config['key_filename'] = key_file

        if config.get('remember'):
            self.settings.set_last_connection(config)
        else:
            self.settings.set_last_connection(
                {k: v for k, v in config.items() if k != 'password'}
            )
        self.settings.save()
        return config
