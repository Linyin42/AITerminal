"""
巡检设备编辑对话框 —— 手动填写一台待巡检设备的连接参数

为什么单独做一个：巡检面板原来只能"从当前会话添加/添加最近连接"，
用户想巡检一台没打开会话的设备（比如另一台还没登过的交换机）就没处填 IP
和账号密码（2026-10-19 用户明确要求"多设备巡检应该有一个编辑页面"）。

用法:
    dialog = DeviceEditDialog(parent, device=None)          # 新增
    if dialog.exec():
        device = dialog.get_device()

    dialog = DeviceEditDialog(parent, device=old_device)    # 编辑
"""
from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QVBoxLayout, QHBoxLayout, QLineEdit, QComboBox,
    QSpinBox, QPushButton, QLabel, QMessageBox, QCheckBox
)
from PyQt6.QtCore import Qt

#: 协议 -> 默认端口（选协议时自动带出来，用户可改）
DEFAULT_PORTS = {"SSH": 22, "Telnet": 23}


class DeviceEditDialog(QDialog):
    """填写/修改一台设备的连接参数"""

    def __init__(self, parent=None, device: dict = None, title: str = ""):
        super().__init__(parent)
        self._device = dict(device or {})
        self.setWindowTitle(title or ("编辑巡检设备" if device else "添加巡检设备"))
        self.setMinimumWidth(460)
        self.init_ui()
        self.load_device(self._device)

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("留空就用 IP 当名字")
        form.addRow("设备名称:", self.name_input)

        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(["SSH", "Telnet"])
        self.protocol_combo.currentTextChanged.connect(self.on_protocol_changed)
        form.addRow("协议:", self.protocol_combo)

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("如 10.2.0.1")
        form.addRow("IP 地址:", self.host_input)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(22)
        form.addRow("端口:", self.port_spin)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("如 admin（华为只用密码时留空）")
        form.addRow("用户名:", self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("巡检时用这个密码登录设备")
        form.addRow("密码:", self.password_input)

        self.chk_show = QCheckBox("显示密码")
        self.chk_show.toggled.connect(
            lambda on: self.password_input.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        form.addRow("", self.chk_show)

        layout.addLayout(form)

        hint = QLabel("密码只留在本次运行的记忆里，不会写进设置文件；"
                      "下次打开巡检面板需要重新填写。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #93A0B4; font-size: 11px;")
        layout.addWidget(hint)

        row = QHBoxLayout()
        row.addStretch()
        btn_ok = QPushButton("确定")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.on_accept)
        row.addWidget(btn_ok)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        layout.addLayout(row)

    # ------------------------------------------------------------ 数据
    def on_protocol_changed(self, protocol: str):
        """切协议时把端口改成该协议的默认端口（用户改过的端口不覆盖）"""
        current = self.port_spin.value()
        defaults = set(DEFAULT_PORTS.values())
        if current in defaults or current <= 0:
            self.port_spin.setValue(DEFAULT_PORTS.get(protocol, 22))

    def load_device(self, device: dict):
        device = device or {}
        protocol = device.get('protocol') or 'SSH'
        index = self.protocol_combo.findText(protocol)
        if index >= 0:
            self.protocol_combo.setCurrentIndex(index)
        self.name_input.setText(device.get('name') or '')
        self.host_input.setText(device.get('host') or '')
        self.port_spin.setValue(int(device.get('port') or DEFAULT_PORTS.get(protocol, 22)))
        self.username_input.setText(device.get('username') or '')
        self.password_input.setText(device.get('password') or '')
        # 新增设备时把光标放在 IP 上，编辑时放在密码上（密码最容易漏填）
        if device.get('host'):
            self.password_input.setFocus()
            self.password_input.selectAll()
        else:
            self.host_input.setFocus()

    def on_accept(self):
        host = self.host_input.text().strip()
        if not host:
            QMessageBox.warning(self, "参数不完整", "请填写设备 IP 地址")
            return
        if not self.password_input.text():
            answer = QMessageBox.question(
                self, "没填密码",
                "没有填写密码，这台设备巡检时会登录失败（只能看到连接异常）。\n"
                "仍然添加吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def get_device(self) -> dict:
        protocol = self.protocol_combo.currentText()
        host = self.host_input.text().strip()
        name = self.name_input.text().strip() or host or "未命名设备"
        device = dict(self._device)
        device.update({
            'name': name,
            'protocol': protocol,
            'host': host,
            'port': self.port_spin.value(),
            'username': self.username_input.text().strip(),
            'password': self.password_input.text(),
            'source': '手动填写',
        })
        return device
