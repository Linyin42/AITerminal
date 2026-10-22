"""
连接对话框 - 配置新连接

界面分两层：
1. ConnectDialog 主对话框（简洁）：
   - 协议只列 SSH / Telnet / Console（内部 Console 映射为 Serial）
   - Console 只暴露 串口 / 波特率 / 停止位 三个最常用项，默认 9600-8-N-1
2. SerialAdvancedDialog 高级串口对话框：
   - 数据位 / 校验 / 流控 / DTR / RTS / 行结束符 / 字符间隔 / 自动重连 / 本地回显
   - 在主对话框点"高级选项…"后，主对话框隐藏、弹出高级对话框
"""
import threading

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QComboBox, QPushButton, QSpinBox, QDoubleSpinBox,
    QLabel, QGroupBox, QMessageBox, QFileDialog, QCheckBox
)

from config.presets import (
    PROTOCOL_DEFAULTS, CONSOLE_PRESETS,
    BAUDRATES, PARITY_OPTIONS, STOPBIT_OPTIONS, DATABITS_OPTIONS,
    FLOW_OPTIONS, LINE_ENDING_OPTIONS,
)
from config.settings import Settings
from ui.window_utils import center_on_screen

# UI 显示名 ↔ 内部协议名（核心层仍用 Serial，避免改动连接器/会话代码）
PROTOCOL_INTERNAL = {"SSH": "SSH", "Telnet": "Telnet", "Console": "Serial",
                     "eNSP Pro": "eNSP"}
PROTOCOL_DISPLAY = {"SSH": "SSH", "Telnet": "Telnet", "Serial": "Console",
                    "eNSP": "eNSP Pro"}


class _QuietCombo(QComboBox):
    """下拉框：回车只当"选定"，**不要**冒泡到对话框的默认按钮。

    现场（2026-10-21 `%TEMP%\\gui_drive_1.log`）：鼠标点协议下拉 + 键盘选到
    eNSP Pro + 按回车 ⇒ QDialog 把这次回车当成"确定" ⇒ 弹「参数不完整」；
    设备下拉框上按回车又弹一次；参数齐全时则直接开始连接。
    用户的本意只是"把下拉项定下来"，不该触发提交。

    可编辑的下拉框（串口号/波特率）放行：那里回车是"确认手输的值"。

    实测（Qt6）：下拉框自己不认回车 —— 未处理的键走 `showPopup(); return;`，
    事件既不 accept 也不 stop，于是继续冒泡到 `QDialog::keyPressEvent`，
    被当成默认按钮的点击。所以这里必须**一律吞掉**。
    弹出列表打开时，按键落在弹出列表自己的焦点窗口上，到不了这里。
    """

    def keyPressEvent(self, event):                       # noqa: N802 —— Qt 的命名
        if (not self.isEditable()
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            event.accept()
            return
        super().keyPressEvent(event)


class _PrefillPasswordEdit(QLineEdit):
    """密码框：预填了上次的密码时，**第一次敲键就整段替换**，而不是接在后面。

    现场（2026-10-21 `%TEMP%\\gui_drive_1.log`）：上次"记住该连接"存了 9 位密码，
    新密码 10 位敲进去 ⇒ 框里变成 19 位（两段拼接）。密码显示成圆点，用户根本
    看不出，连上去只会回 `Error: The password is invalid.`
    —— 同类问题 2026-10-19 已经害过用户一次（见 `set_config` 里那段注释）。

    规则：
      · `setText()` 预填 ⇒ 进入"待替换"态（`clear()` 走 setText('')，自动退出）
      · 待替换态下第一次按可打印字符（或粘贴）⇒ 先全选再由父类插入 ⇒ 整段换掉
      · 先按方向键 / Home / End / 退格 ⇒ 取消待替换（用户是在改，不是重填）
    """

    _CANCEL_KEYS = frozenset({
        Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End,
        Qt.Key.Key_Backspace, Qt.Key.Key_Delete, Qt.Key.Key_Up, Qt.Key.Key_Down,
    })

    def __init__(self, parent=None):
        super().__init__(parent)
        self._replace_on_type = False

    def setText(self, text):                              # noqa: N802 —— Qt 的命名
        super().setText(text)
        self._replace_on_type = bool(text)

    def keyPressEvent(self, event):                       # noqa: N802
        if self._replace_on_type:
            if event.key() in self._CANCEL_KEYS:
                self._replace_on_type = False
            elif event.text() and event.text().isprintable():
                self._replace_on_type = False
                self.selectAll()
            elif event.matches(QKeySequence.StandardKey.Paste):
                # 从密码管理器/笔记里粘贴新密码：同样整段替换，别接在旧值后面
                self._replace_on_type = False
                self.selectAll()
        super().keyPressEvent(event)


def _set_combo_by_data(combo: QComboBox, value):
    """按 userData 选中下拉项，找不到就保持默认；兼容 1 vs 1.0 的数值比较"""
    for index in range(combo.count()):
        if combo.itemData(index) == value:
            combo.setCurrentIndex(index)
            return
    for index in range(combo.count()):
        try:
            if float(combo.itemData(index)) == float(value):
                combo.setCurrentIndex(index)
                return
        except (TypeError, ValueError):
            continue


class SerialPortScanner(QObject):
    """在后台线程枚举串口，结果存进类级缓存。

    为什么不能同步枚举：`serial.tools.list_ports` 在 Windows 上要过 setupapi，
    插着 USB 转串口时首次调用能到几百毫秒。放在 `ConnectDialog.__init__` 里同步跑，
    用户看到的就是"点开连接设备 / 高级选项要卡一下才出来"（2026-10-17 反馈）。
    现在改成：对话框读缓存立刻显示 → 后台线程刷新 → 扫完用信号回主线程重填。
    """

    done = pyqtSignal(list)

    _cache: list = []
    _busy = False

    @classmethod
    def cached(cls) -> list:
        """当前缓存的串口列表（可能为空，但绝不阻塞）"""
        return list(cls._cache)

    @classmethod
    def busy(cls) -> bool:
        """是否正有一次枚举在跑（据此决定显示"正在检测…"还是"未检测到串口"）"""
        return cls._busy

    def refresh(self):
        """触发一次后台枚举；已有枚举在跑就直接返回（不排队、不重复扫）"""
        if SerialPortScanner._busy:
            return
        SerialPortScanner._busy = True

        def work():
            try:
                items = ConnectDialog.list_serial_ports()
            except Exception:                                     # noqa: BLE001
                items = []
            SerialPortScanner._cache = items
            SerialPortScanner._busy = False
            self.done.emit(items)      # 跨线程：Qt 自动排队到主线程执行

        threading.Thread(target=work, daemon=True,
                         name="serial-port-scan").start()


class EnspDevicePuller(QObject):
    """后台拉 eNSP Pro 平台上的沙箱/设备清单。

    网络请求绝不能放 GUI 线程：平台在另一台机器上，超时能到几十秒，
    同步跑就是"点了按钮界面卡死"。与 SerialPortScanner 同一套路：
    普通线程干活，结果用信号发回主线程（Qt 自动排队）。
    """

    done = pyqtSignal(object)          # (items, error_text)

    def refresh(self, host: str, port: int):
        def work():
            items, err = None, None
            try:
                from core.ensp_client import EnspProClient, EnspProError
                cli = EnspProClient(host, port)
                cli.login()
                items = sorted(cli.devices(),
                               key=lambda t: (t[0] or 0, (t[2] or {}).get("id") or 0))
            except EnspProError as exc:
                err = str(exc)
            except Exception as exc:                              # noqa: BLE001
                err = f"拉取失败：{exc}"
            self.done.emit((items, err))

        threading.Thread(target=work, daemon=True, name="ensp-pull").start()


class SerialAdvancedDialog(QDialog):
    """串口高级参数对话框 —— 从主对话框"高级选项…"进入"""

    def __init__(self, parent=None, config: dict = None):
        super().__init__(parent)
        self.setWindowTitle("Console 高级参数")
        self.setMinimumWidth(460)
        self.config = dict(config or {})
        self.init_ui()
        self.load_values()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # Console 预设
        preset_group = QGroupBox("常用预设")
        preset_layout = QVBoxLayout()
        self.console_preset = _QuietCombo()
        self.console_preset.addItem("— 选择 Console 预设 —")
        self.console_preset.addItems(list(CONSOLE_PRESETS.keys()))
        self.console_preset.currentTextChanged.connect(self.on_preset_selected)
        preset_layout.addWidget(self.console_preset)
        self.summary_label = QLabel()
        self.summary_label.setStyleSheet("color: #93A0B4; font-size: 11px;")
        preset_layout.addWidget(self.summary_label)
        preset_group.setLayout(preset_layout)
        layout.addWidget(preset_group)

        # 帧格式
        frame_group = QGroupBox("帧格式")
        frame_layout = QHBoxLayout()
        self.databits_combo = _QuietCombo()
        self.databits_combo.addItems([str(b) for b in DATABITS_OPTIONS])
        self.parity_combo = _QuietCombo()
        for label, value in PARITY_OPTIONS:
            self.parity_combo.addItem(label, value)
        frame_layout.addWidget(QLabel("数据位"))
        frame_layout.addWidget(self.databits_combo)
        frame_layout.addWidget(QLabel("校验"))
        frame_layout.addWidget(self.parity_combo)
        frame_layout.addStretch()
        frame_group.setLayout(frame_layout)
        layout.addWidget(frame_group)

        # 流控 / 控制线 / 行结束符
        io_group = QGroupBox("流控与控制线")
        io_layout = QFormLayout()

        self.flow_combo = _QuietCombo()
        for label, value in FLOW_OPTIONS:
            self.flow_combo.addItem(label, value)
        io_layout.addRow("流控:", self.flow_combo)

        signal_row = QHBoxLayout()
        self.dtr_combo = _QuietCombo()
        self.dtr_combo.addItem("不干预", None)
        self.dtr_combo.addItem("拉高 (High)", True)
        self.dtr_combo.addItem("拉低 (Low)", False)
        self.rts_combo = _QuietCombo()
        self.rts_combo.addItem("不干预", None)
        self.rts_combo.addItem("拉高 (High)", True)
        self.rts_combo.addItem("拉低 (Low)", False)
        signal_row.addWidget(QLabel("DTR"))
        signal_row.addWidget(self.dtr_combo)
        signal_row.addWidget(QLabel("RTS"))
        signal_row.addWidget(self.rts_combo)
        signal_row.addStretch()
        io_layout.addRow("控制线:", signal_row)

        self.line_ending_combo = _QuietCombo()
        for label, value in LINE_ENDING_OPTIONS:
            self.line_ending_combo.addItem(label, value)
        io_layout.addRow("行结束符:", self.line_ending_combo)

        self.char_delay_spin = QSpinBox()
        self.char_delay_spin.setRange(0, 200)
        self.char_delay_spin.setSuffix(" ms")
        self.char_delay_spin.setToolTip("逐字符发送的间隔，老设备或长线缆丢字符时调大")
        io_layout.addRow("字符间隔:", self.char_delay_spin)

        io_group.setLayout(io_layout)
        layout.addWidget(io_group)

        # 断线恢复 / 本地回显
        recover_group = QGroupBox("断线恢复")
        recover_layout = QVBoxLayout()
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
        recover_layout.addLayout(reconnect_row)
        self.chk_local_echo = QCheckBox("本地回显（设备不回显时勾选）")
        recover_layout.addWidget(self.chk_local_echo)
        recover_group.setLayout(recover_layout)
        layout.addWidget(recover_group)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

        # 任一帧参数变化时刷新摘要
        for combo in (self.databits_combo, self.parity_combo):
            combo.currentIndexChanged.connect(self.update_summary)

    def load_values(self):
        c = self.config
        if c.get('bytesize'):
            self.databits_combo.setCurrentText(str(c['bytesize']))
        if c.get('parity') is not None:
            _set_combo_by_data(self.parity_combo, c.get('parity', 'N'))
        if c.get('flow'):
            _set_combo_by_data(self.flow_combo, c.get('flow', 'none'))
        if c.get('dtr') is not None:
            _set_combo_by_data(self.dtr_combo, c.get('dtr'))
        if c.get('rts') is not None:
            _set_combo_by_data(self.rts_combo, c.get('rts'))
        if c.get('line_ending'):
            _set_combo_by_data(self.line_ending_combo, c.get('line_ending', '\r'))
        if c.get('char_delay_ms') is not None:
            self.char_delay_spin.setValue(int(c['char_delay_ms']))
        self.chk_auto_reconnect.setChecked(bool(c.get('auto_reconnect', True)))
        if c.get('reconnect_interval'):
            self.reconnect_spin.setValue(float(c['reconnect_interval']))
        self.chk_local_echo.setChecked(bool(c.get('local_echo', False)))
        self.update_summary()

    def on_preset_selected(self, name: str):
        preset = CONSOLE_PRESETS.get(name)
        if not preset:
            return
        if preset.get('bytesize'):
            self.databits_combo.setCurrentText(str(preset['bytesize']))
        if preset.get('parity') is not None:
            _set_combo_by_data(self.parity_combo, preset.get('parity', 'N'))
        if preset.get('flow'):
            _set_combo_by_data(self.flow_combo, preset.get('flow', 'none'))
        if preset.get('dtr') is not None:
            _set_combo_by_data(self.dtr_combo, preset.get('dtr'))
        if preset.get('rts') is not None:
            _set_combo_by_data(self.rts_combo, preset.get('rts'))
        if preset.get('line_ending'):
            _set_combo_by_data(self.line_ending_combo, preset.get('line_ending', '\r'))
        if preset.get('char_delay_ms') is not None:
            self.char_delay_spin.setValue(int(preset['char_delay_ms']))
        # 波特率/停止位也随预设走，通过 config 回传给主对话框
        if preset.get('baudrate'):
            self.config['baudrate'] = preset['baudrate']
        if preset.get('stopbits') is not None:
            self.config['stopbits'] = preset['stopbits']
        self.update_summary()

    def update_summary(self):
        """显示当前 波特率-数据位-校验-停止位 摘要"""
        baud = self.config.get('baudrate', 9600)
        data = self.databits_combo.currentText() or '8'
        parity = self.parity_combo.currentData() or 'N'
        stop = self.config.get('stopbits', 1)
        self.summary_label.setText(f"当前帧格式：{baud}-{data}-{parity}-{stop}")

    def get_values(self) -> dict:
        """返回高级参数（含可能被预设修改的 baudrate / stopbits）"""
        values = dict(self.config)
        values.update({
            'bytesize': int(self.databits_combo.currentText()),
            'parity': self.parity_combo.currentData(),
            'flow': self.flow_combo.currentData(),
            'dtr': self.dtr_combo.currentData(),
            'rts': self.rts_combo.currentData(),
            'line_ending': self.line_ending_combo.currentData(),
            'char_delay_ms': self.char_delay_spin.value(),
            'auto_reconnect': self.chk_auto_reconnect.isChecked(),
            'reconnect_interval': self.reconnect_spin.value(),
            'local_echo': self.chk_local_echo.isChecked(),
        })
        return values


class ConnectDialog(QDialog):
    """连接配置对话框（简洁版）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建连接")
        self.setMinimumWidth(440)
        self.settings = Settings()
        self.serial_defaults = self.settings.serial_defaults()
        # 串口高级参数（主页只暴露三项，其余保存在这里）
        self.serial_advanced = {}
        # 是否"带着已有内容打开"（上次连接 / 设备库记录）：这类情况下不收起输入区，
        # 否则用户又得把刚保存过的主机、用户名、端口重填一遍。
        self._prefilled = False
        self.init_ui()
        self.load_last()
        self._apply_preset_state()

    # ------------------------------------------------------------ 界面
    def showEvent(self, event):        # noqa: N802 —— Qt 的命名
        """首次显示居中（只居一次，之后尊重用户挪的位置）"""
        super().showEvent(event)
        if not getattr(self, "_centered", False):
            self._centered = True
            center_on_screen(self, self.parent())

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 基本信息
        self.basic_group = QGroupBox("基本信息")
        basic_layout = QFormLayout()

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("选填，留空时自动按主机命名")
        basic_layout.addRow("名称(选填):", self.name_input)

        self.protocol_combo = _QuietCombo()
        # SSH / Telnet / Console，以及华为 eNSP Pro 仿真平台
        self.protocol_combo.addItems(["SSH", "Telnet", "Console", "eNSP Pro"])
        self.protocol_combo.currentTextChanged.connect(self.on_protocol_changed)
        basic_layout.addRow("协议:", self.protocol_combo)

        self.basic_group.setLayout(basic_layout)
        layout.addWidget(self.basic_group)

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

        self.password_input = _PrefillPasswordEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.conn_layout.addRow("密码:", self.password_input)

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

        # Console 参数（简洁：只留 串口 / 波特率 / 停止位）
        self.serial_group = QGroupBox("Console 参数")
        serial_layout = QFormLayout()

        port_row = QHBoxLayout()
        self.serial_port = QComboBox()
        self.serial_port.setEditable(True)
        self.serial_port.setMinimumWidth(240)
        btn_refresh = QPushButton("刷新")
        btn_refresh.setToolTip("重新枚举本机串口")
        # 必须包一层：clicked 会带一个 bool（checked），直接连会把 False 当 items 传进去，
        # refresh_serial_ports 里 list(False) 抛 TypeError —— 表现出来的就是"点刷新弹异常框"。
        btn_refresh.clicked.connect(lambda: self.refresh_serial_ports())
        port_row.addWidget(self.serial_port)
        port_row.addWidget(btn_refresh)
        serial_layout.addRow("串口:", port_row)

        self.baudrate_combo = QComboBox()
        self.baudrate_combo.setEditable(True)
        self.baudrate_combo.addItems([str(b) for b in BAUDRATES])
        serial_layout.addRow("波特率:", self.baudrate_combo)

        self.stopbits_combo = _QuietCombo()
        for label, value in STOPBIT_OPTIONS:
            self.stopbits_combo.addItem(label, value)
        serial_layout.addRow("停止位:", self.stopbits_combo)

        # 高级选项入口
        advanced_row = QHBoxLayout()
        advanced_row.addStretch()
        self.btn_advanced = QPushButton("高级选项…")
        self.btn_advanced.setToolTip(
            "数据位 / 校验 / 流控 / DTR / RTS / 行结束符 / 字符间隔 / 自动重连 / 本地回显")
        self.btn_advanced.clicked.connect(self.open_advanced)
        advanced_row.addWidget(self.btn_advanced)
        serial_layout.addRow("", advanced_row)

        self.serial_group.setLayout(serial_layout)
        self.serial_group.setVisible(False)
        layout.addWidget(self.serial_group)

        # eNSP Pro（华为仿真平台）：它的设备**没有 TCP 端口**，命令行全靠
        # HTTP + STOMP 中转（见 core/ensp_connector.py），所以除了平台地址/端口，
        # 还必须有沙箱 id 与设备 id —— 后者可以直接从平台拉，不用去别处抄。
        self.ensp_group = QGroupBox("eNSP Pro")
        ensp_layout = QFormLayout()

        self.ensp_sandbox_spin = QSpinBox()
        self.ensp_sandbox_spin.setRange(0, 10_000_000)
        self.ensp_sandbox_spin.setToolTip("平台里沙箱的编号（拉取设备后会自动填）")
        ensp_layout.addRow("沙箱 ID:", self.ensp_sandbox_spin)

        self.ensp_node_spin = QSpinBox()
        self.ensp_node_spin.setRange(0, 10_000_000)
        self.ensp_node_spin.setToolTip("沙箱里设备的编号（node_id，拉取后会自动填）")
        ensp_layout.addRow("设备 ID:", self.ensp_node_spin)

        pull_row = QHBoxLayout()
        self.ensp_device_combo = _QuietCombo()
        self.ensp_device_combo.setMinimumWidth(240)
        self.ensp_device_combo.addItem("（点右侧按钮从平台拉取）", None)
        self.ensp_device_combo.currentIndexChanged.connect(self.on_ensp_device_picked)
        pull_row.addWidget(self.ensp_device_combo, 1)
        self.btn_ensp_pull = QPushButton("从平台拉取设备")
        self.btn_ensp_pull.setToolTip(
            "读取 eNSP Pro 平台上的沙箱与设备，选中后自动填好沙箱 ID / 设备 ID。\n"
            "需要先在“主机”里填平台地址、“端口”填 8443。")
        self.btn_ensp_pull.clicked.connect(self.pull_ensp_devices)
        pull_row.addWidget(self.btn_ensp_pull)
        ensp_layout.addRow("设备:", pull_row)

        ensp_hint = QLabel("平台本身免密自动登录；设备登录密码填下面的“密码”。")
        ensp_hint.setWordWrap(True)
        ensp_layout.addRow("", ensp_hint)

        self.ensp_group.setLayout(ensp_layout)
        self.ensp_group.setVisible(False)
        layout.addWidget(self.ensp_group)

        # 记住密码
        self.chk_remember = QCheckBox("记住该连接（含密码，下次自动填充）")
        self.chk_remember.setToolTip(
            "勾选后，本次填写的连接参数（包括密码）会保存到本机配置文件\n"
            "%USERPROFILE%\\.aiterminal\\settings.json 中，下次打开对话框自动填充。\n"
            "密码以混淆方式存储，共用电脑请勿勾选。"
        )
        layout.addWidget(self.chk_remember)

        # 弹簧：把对话框多余的高度吸收掉。否则各分组会被等分拉伸，
        # 内部出现大片空白（切换到 Console/Telnet 时尤其明显）
        layout.addStretch(1)

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

        # 串口下拉：先让后台线程去扫（不阻塞打开）再用缓存填，扫完由信号回来重填。
        # 顺序不能反：先 refresh() 才会把 busy 置上，下拉框显示"正在检测串口…"，
        # 否则空缓存 + 未在扫会让用户以为"本机没有串口"。
        self.scanner = SerialPortScanner(self)
        self.scanner.done.connect(self.on_ports_scanned)
        self.scanner.refresh()
        self.refresh_serial_ports()
        self.apply_serial_defaults()

    # ------------------------------------------------------------ 协议
    @property
    def internal_protocol(self) -> str:
        """UI 协议名 → 内部协议名（Console → Serial）"""
        return PROTOCOL_INTERNAL.get(self.protocol_combo.currentText(), "SSH")

    def set_protocol(self, internal: str):
        """按内部协议名设置下拉框（Serial → Console）"""
        self.protocol_combo.setCurrentText(PROTOCOL_DISPLAY.get(internal, internal))

    # ------------------------------------------------------------ 串口
    def refresh_serial_ports(self, items=None):
        """把串口列表填进下拉框 —— **本函数不枚举串口**（枚举在后台，见 SerialPortScanner）

        枚举走 Windows setupapi，插着 USB 转串口时首次要几百毫秒；同步跑就是
        "点开对话框要卡一下"。这里只负责把给定（或缓存的）结果填进控件。
        """
        items = SerialPortScanner.cached() if items is None else list(items)
        current = self.serial_port.currentText()
        self.serial_port.clear()
        for item in items:
            label = item["device"]
            if item["description"]:
                label = f"{item['device']} — {item['description']}"
            self.serial_port.addItem(label, item["device"])
        if not items:
            self.serial_port.addItem(
                "正在检测串口…" if SerialPortScanner.busy() else "未检测到串口", "")

        if current:
            for index in range(self.serial_port.count()):
                if self.serial_port.itemData(index) == current or \
                        self.serial_port.itemText(index) == current:
                    self.serial_port.setCurrentIndex(index)
                    break

    def on_ports_scanned(self, items):
        """后台枚举完成：重填下拉框（用户已选中的那个保持不变）"""
        self.refresh_serial_ports(items)

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
        """填入上次使用的串口默认参数（主页三项 + 高级参数缓存）"""
        d = self.serial_defaults
        self.baudrate_combo.setCurrentText(str(d.get("baudrate", 9600)))
        _set_combo_by_data(self.stopbits_combo, d.get("stopbits", 1))
        self.serial_advanced = {
            'bytesize': d.get('bytesize', 8),
            'parity': d.get('parity', 'N'),
            'flow': d.get('flow', 'none'),
            'dtr': d.get('dtr'),
            'rts': d.get('rts'),
            'line_ending': d.get('line_ending', '\r'),
            'char_delay_ms': d.get('char_delay_ms', 0),
            'auto_reconnect': d.get('auto_reconnect', True),
            'reconnect_interval': d.get('reconnect_interval', 2.0),
            'local_echo': d.get('local_echo', False),
        }

    def open_advanced(self):
        """打开高级串口对话框

        注意：**不能**在打开前把本对话框 hide() 掉。本对话框是用 exec() 模态
        打开的，一旦 hide()，Qt 会认为这次模态会话结束、exec() 立刻返回 ——
        于是"新建连接"对话框从屏幕上消失，关掉高级框也回不来（用户得重新
        点一遍，之前填的内容全丢）。高级框作为子对话框盖在上面即可。
        """
        cfg = dict(self.serial_advanced)
        try:
            cfg['baudrate'] = int(self.baudrate_combo.currentText().strip())
        except ValueError:
            cfg['baudrate'] = 9600
        cfg['stopbits'] = self.stopbits_combo.currentData()

        dlg = SerialAdvancedDialog(self, cfg)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            values = dlg.get_values()
            # 波特率/停止位同步回主页
            if values.get('baudrate'):
                self.baudrate_combo.setCurrentText(str(values['baudrate']))
            if values.get('stopbits') is not None:
                _set_combo_by_data(self.stopbits_combo, values['stopbits'])
            # 其余高级参数缓存
            for key in ('bytesize', 'parity', 'flow', 'dtr', 'rts',
                        'line_ending', 'char_delay_ms', 'auto_reconnect',
                        'reconnect_interval', 'local_echo'):
                if key in values:
                    self.serial_advanced[key] = values[key]

    # ------------------------------------------------------------ 交互
    def choose_key_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择私钥文件", "", "所有文件 (*)"
        )
        if path:
            self.key_input.setText(path)

    # 连接参数里哪几行对当前协议没意义（行序：0 主机 / 1 端口 / 2 用户名 / 3 密码 / 4 密钥文件）
    _CONN_ROW_USERNAME = 2
    _CONN_ROW_KEY_FILE = 4

    def _set_conn_row_visible(self, row: int, visible: bool):
        """整行显隐：标签 + 字段（密钥文件那行的字段是个 HBoxLayout，得逐个藏子控件）"""
        for role in (QFormLayout.ItemRole.LabelRole, QFormLayout.ItemRole.FieldRole):
            item = self.conn_layout.itemAt(row, role)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.setVisible(visible)
                continue
            sub = item.layout()
            if sub is not None:
                for i in range(sub.count()):
                    child = sub.itemAt(i).widget()
                    if child is not None:
                        child.setVisible(visible)

    def _sync_conn_rows(self):
        """按协议收起用不上的行，给对话框腾高度

        eNSP Pro 平台**免密自动登录**，用户名/密钥文件都没意义；密钥文件只有 SSH 用得上。
        用户反馈（2026-10-17）：对话框在小屏上显示不完 —— 一行就能省一截。
        """
        internal = PROTOCOL_INTERNAL.get(self.protocol_combo.currentText(), "SSH")
        self._set_conn_row_visible(self._CONN_ROW_USERNAME, internal in ("SSH", "Telnet"))
        self._set_conn_row_visible(self._CONN_ROW_KEY_FILE, internal == "SSH")

    def _apply_preset_state(self):
        """把表单刷成"当前协议该有的样子"

        历史：这里原本是"没选预设就把输入区全收起、只留一句「选择一个连接」"。
        预设区在 2026-10-17 按用户要求删除后，表单**始终展开**，本方法只剩按协议显隐
        （连接参数 / 串口 / eNSP Pro 三块 + 参数内各行的取舍）。
        注意**不要**在这里调 `set_protocol()` 或重设端口 —— 那是 on_protocol_changed 的事。
        """
        is_console = self.protocol_combo.currentText() == "Console"
        self.basic_group.setVisible(True)
        self.conn_group.setVisible(not is_console)
        self.serial_group.setVisible(is_console)
        self.chk_remember.setVisible(True)
        self._sync_conn_rows()
        self.adjustSize()

    def on_protocol_changed(self, display_name: str):
        """协议切换（显示名：SSH/Telnet/Console/eNSP Pro）"""
        is_console = (display_name == "Console")
        internal = PROTOCOL_INTERNAL.get(display_name, "SSH")
        is_ensp = (internal == "eNSP")
        self.conn_group.setVisible(not is_console)
        self.serial_group.setVisible(is_console)
        self.ensp_group.setVisible(is_ensp)
        self._sync_conn_rows()
        defaults = PROTOCOL_DEFAULTS.get(internal, {})
        if 'port' in defaults and not is_console:
            self.port_spin.setValue(defaults['port'])
        # 分组显示/隐藏后让对话框跟着收缩或展开，否则会残留上一个协议的高度
        self.adjustSize()

    # ------------------------------------------------------------ eNSP Pro
    def pull_ensp_devices(self):
        """从 eNSP Pro 平台拉设备清单（后台线程，界面不卡）"""
        host = self.host_input.text().strip()
        port = self.port_spin.value()
        if not host:
            QMessageBox.warning(self, "缺少平台地址",
                                "请先在“主机”里填写 eNSP Pro 服务器地址（如 192.168.31.11）")
            return
        self.btn_ensp_pull.setEnabled(False)
        self.ensp_device_combo.clear()
        self.ensp_device_combo.addItem("正在拉取…", None)
        # 保引用：信号回调前对象不能被回收
        self._ensp_puller = EnspDevicePuller(self)
        self._ensp_puller.done.connect(self.on_ensp_devices_pulled)
        self._ensp_puller.refresh(host, port)

    def on_ensp_devices_pulled(self, result):
        """拉取结束：把设备填进下拉，选中即回填沙箱/设备 ID"""
        items, err = result if isinstance(result, (tuple, list)) else (None, "返回异常")
        self.btn_ensp_pull.setEnabled(True)
        self.ensp_device_combo.clear()
        if err:
            self.ensp_device_combo.addItem("（拉取失败）", None)
            QMessageBox.warning(self, "拉取设备失败", str(err))
            return
        if not items:
            self.ensp_device_combo.addItem("（平台上没有设备，请先在 WebUI 里新建并启动）", None)
            return
        self.ensp_device_combo.addItem("—— 选择设备 ——", None)
        for sandbox_id, sandbox_name, dev in items:
            dtype = (dev.get("deviceType") or {})
            type_name = dtype.get("displayName") or dtype.get("name") or "?"
            label = (f"沙箱 {sandbox_name or sandbox_id}（id={sandbox_id}） · "
                     f"设备 {dev.get('name') or '?'}（id={dev.get('id')}，{type_name}）")
            self.ensp_device_combo.addItem(
                label, (sandbox_id, dev.get("id"), dev.get("name") or ""))

    def on_ensp_device_picked(self, index: int):
        """选中下拉里的设备 → 自动填沙箱 ID / 设备 ID（用户不用抄数字）"""
        data = self.ensp_device_combo.itemData(index)
        if isinstance(data, (tuple, list)) and len(data) >= 2:
            self.ensp_sandbox_spin.setValue(int(data[0] or 0))
            self.ensp_node_spin.setValue(int(data[1] or 0))
            if len(data) > 2 and data[2] and not self.name_input.text().strip():
                self.name_input.setText(f"eNSP-{data[2]}")

    def load_last(self):
        """载入上次使用的连接参数（含密码，若上次勾了"记住"）"""
        last = self.settings.get_last_connection()
        if not last:
            return
        self._prefilled = True                  # 有上次连接：不收起输入区
        self.name_input.setText(last.get('name', ''))
        protocol = last.get('protocol', 'SSH')
        self.set_protocol(protocol)
        self.host_input.setText(last.get('host', ''))
        self.username_input.setText(last.get('username', ''))
        self.port_spin.setValue(last.get('port', 22))
        if last.get('sandbox_id') is not None:
            self.ensp_sandbox_spin.setValue(int(last.get('sandbox_id') or 0))
        if last.get('node_id') is not None:
            self.ensp_node_spin.setValue(int(last.get('node_id') or 0))
        if last.get('password'):
            password = last['password']
            if password.startswith('b64:'):
                password = self.settings.decode_password(password)
            self.password_input.setText(password)
        key_file = last.get('key_filename', '')
        if key_file:
            self.key_input.setText(key_file)
        self.chk_remember.setChecked(bool(last.get('remember')))

        # Console 参数
        if protocol == 'Serial':
            serial_port = last.get('serial_port') or last.get('host', '')
            if serial_port:
                self.serial_port.setCurrentText(serial_port)
            if last.get('baudrate'):
                self.baudrate_combo.setCurrentText(str(last['baudrate']))
            if last.get('stopbits') is not None:
                _set_combo_by_data(self.stopbits_combo, last['stopbits'])
            # 高级参数回填缓存
            for key in ('bytesize', 'parity', 'flow', 'dtr', 'rts',
                        'line_ending', 'char_delay_ms', 'auto_reconnect',
                        'reconnect_interval', 'local_echo'):
                if key in last and last[key] is not None:
                    self.serial_advanced[key] = last[key]

    # ------------------------------------------------------------ 校验
    def on_accept(self):
        """校验后接受"""
        protocol = self.internal_protocol
        if protocol == "Serial":
            if not self.current_serial_port():
                QMessageBox.warning(self, "参数不完整", "请选择或填写串口号（如 COM3）")
                return
        elif protocol == "eNSP":
            if not self.host_input.text().strip():
                QMessageBox.warning(self, "参数不完整",
                                    "请填写平台地址（eNSP Pro 服务器的 IP）")
                return
            if not self.ensp_sandbox_spin.value() or not self.ensp_node_spin.value():
                QMessageBox.warning(self, "参数不完整",
                                    "请填写沙箱 ID 与设备 ID（可点“从平台拉取设备”自动填）")
                return
        else:
            if not self.host_input.text().strip():
                QMessageBox.warning(self, "参数不完整", "请填写主机地址")
                return
        self.accept()

    def set_config(self, config: dict):
        """用一份连接配置回填整个对话框（供 Vault 的"连接/编辑"复用）"""
        if not config:
            return
        self.name_input.setText(config.get('name', ''))
        self.set_protocol(config.get('protocol', 'SSH'))
        self.host_input.setText(config.get('host', ''))
        self.port_spin.setValue(int(config.get('port', 22) or 22))
        if config.get('sandbox_id') is not None:
            self.ensp_sandbox_spin.setValue(int(config.get('sandbox_id') or 0))
        if config.get('node_id') is not None:
            self.ensp_node_spin.setValue(int(config.get('node_id') or 0))
        self.username_input.setText(config.get('username', ''))
        key_file = config.get('key_filename', '')
        if key_file:
            self.key_input.setText(key_file)

        if config.get('protocol') == 'Serial':
            serial_port = config.get('serial_port') or config.get('host', '')
            if serial_port:
                self.serial_port.setCurrentText(serial_port)
            if config.get('baudrate'):
                self.baudrate_combo.setCurrentText(str(config['baudrate']))
            if config.get('stopbits') is not None:
                _set_combo_by_data(self.stopbits_combo, config['stopbits'])
            for key in ('bytesize', 'parity', 'flow', 'dtr', 'rts',
                        'line_ending', 'char_delay_ms', 'auto_reconnect',
                        'reconnect_interval', 'local_echo'):
                if key in config and config[key] is not None:
                    self.serial_advanced[key] = config[key]

        # 密码（可能是 base64 混淆的）
        # 换设备时必须把上一台的密码清掉：以前只在"有新密码"时 setText，于是
        # 对话框打开时 load_last() 预填的旧密码会跟着跑到另一台设备上，设备直接回
        # "Error: The password is invalid."（用户 2026-10-19 的反馈：点了连接就说
        # 密码错误，其实自己什么都没输错）。
        password = config.get('password') or ''
        if isinstance(password, str) and password.startswith('b64:'):
            password = self.settings.decode_password(password)
        self.password_input.setText(password or '')
        has_password = bool(password)
        if config.get('remember') is not None:
            self.chk_remember.setChecked(bool(config.get('remember')))
        else:
            self.chk_remember.setChecked(has_password)
        self._prefilled = True                  # 带着记录打开：下面直接显示已填内容
        self._apply_preset_state()
        if not has_password:
            # 没有现成密码：光标直接停在密码框，用户敲进去就是这一台的密码
            self.password_input.setFocus()
            self.password_input.selectAll()

    def get_config(self) -> dict:
        """获取配置"""
        protocol = self.internal_protocol
        name = self.name_input.text().strip()
        if protocol == "Serial":
            auto_name = f"Console-{self.current_serial_port() or '串口'}"
        else:
            auto_name = self.host_input.text().strip() or "新建连接"
        config = {
            'name': name or auto_name,
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
                'stopbits': self.stopbits_combo.currentData(),
            })
            # 合并高级参数
            advanced = dict(self.serial_advanced)
            config.update({
                'bytesize': int(advanced.get('bytesize', 8)),
                'parity': advanced.get('parity', 'N'),
                'flow': advanced.get('flow', 'none'),
                'dtr': advanced.get('dtr'),
                'rts': advanced.get('rts'),
                'line_ending': advanced.get('line_ending', '\r'),
                'char_delay_ms': int(advanced.get('char_delay_ms', 0)),
                'auto_reconnect': bool(advanced.get('auto_reconnect', True)),
                'reconnect_interval': float(advanced.get('reconnect_interval', 2.0)),
                'local_echo': bool(advanced.get('local_echo', False)),
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
            if protocol == "eNSP":
                # eNSP Pro 还要沙箱/设备编号：连接器靠它定位 console
                # （eNSP 的设备没有 TCP 端口，命令行走 HTTP+STOMP）
                config['sandbox_id'] = self.ensp_sandbox_spin.value()
                config['node_id'] = self.ensp_node_spin.value()

        # 保存到 last_connection —— 密码统一用 base64 混淆，和 Vault 保持一致
        save_cfg = dict(config)
        if save_cfg.get('remember') and save_cfg.get('password'):
            save_cfg['password'] = self.settings.encode_password(save_cfg['password'])
        elif not save_cfg.get('remember'):
            save_cfg.pop('password', None)
        self.settings.set_last_connection(save_cfg)
        self.settings.save()
        return config
