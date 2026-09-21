"""
设备库面板 (Vault) —— 对标 NetCatty 左侧的 Vault

显示已保存的设备列表，点击即可连接，也支持新建 / 编辑 / 删除 / 刷新。
每台设备是一张卡片：名称、主机:端口、协议徽标、连接状态点。
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QFrame, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QColor, QBrush, QPalette


def _device_key(config: dict) -> tuple:
    host = config.get('host') or config.get('serial_port') or ''
    return (config.get('protocol'), host, config.get('port'))


def _device_title(config: dict) -> str:
    name = config.get('name') or config.get('host') or config.get('serial_port') or '未命名设备'
    return name


def _device_subtitle(config: dict) -> str:
    protocol = config.get('protocol', 'SSH')
    if protocol == 'Serial':
        host = config.get('serial_port') or 'COM?'
    else:
        host = config.get('host') or ''
        port = config.get('port')
        if host and port:
            host = f"{host}:{port}"
    vendor = config.get('vendor')
    sub = f"{protocol}  {host}" if host else protocol
    if vendor and vendor != 'unknown':
        sub += f"  ·  {vendor.upper()}"
    return sub


class DeviceCard(QFrame):
    """设备库里的一张卡片（名称 + 主机信息 + 状态点）"""

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.config = config
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setLineWidth(0)
        self.selected = False
        self.connected = False
        self._build()

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        # 状态点
        self.dot = QLabel("●")
        self.dot.setFixedSize(10, 10)
        self._paint_dot()
        layout.addWidget(self.dot)

        # 文本区
        text = QVBoxLayout()
        text.setSpacing(1)
        title = QLabel(_device_title(self.config))
        title.setStyleSheet("color: #e8e8e8; font-weight: bold; font-size: 13px;")
        sub = QLabel(_device_subtitle(self.config))
        sub.setStyleSheet("color: #8a8a8a; font-size: 11px;")
        text.addWidget(title)
        text.addWidget(sub)
        layout.addLayout(text, 1)

        # 协议徽标
        badge = QLabel(self.config.get('protocol', 'SSH'))
        badge.setStyleSheet(
            "color: #4fc1ff; border: 1px solid #3c3c3c; border-radius: 3px;"
            " padding: 1px 6px; font-size: 10px;"
        )
        layout.addWidget(badge)

    def _paint_dot(self):
        color = "#3fb950" if self.connected else "#555555"
        self.dot.setStyleSheet(
            f"color: {color}; font-size: 12px;"
        )

    def set_connected(self, connected: bool):
        self.connected = connected
        self._paint_dot()

    def set_selected(self, selected: bool):
        self.selected = selected
        if selected:
            self.setStyleSheet(
                "DeviceCard { background-color: #094771; border: 1px solid #0e639c;"
                " border-radius: 6px; }")
        else:
            self.setStyleSheet(
                "DeviceCard { background-color: #252526; border: 1px solid #3c3c3c;"
                " border-radius: 6px; }")
        # 重建文本样式（setStyleSheet 会覆盖子控件？不会，但保险起见重设）
        self._reapply_text_style()

    def _reapply_text_style(self):
        for label in self.findChildren(QLabel):
            if label is self.dot:
                continue
            if label.text() in ('SSH', 'Telnet', 'Serial'):
                label.setStyleSheet(
                    "color: #4fc1ff; border: 1px solid #3c3c3c; border-radius: 3px;"
                    " padding: 1px 6px; font-size: 10px;")
            elif label.font().bold():
                label.setStyleSheet("color: #e8e8e8; font-weight: bold; font-size: 13px;")
            else:
                label.setStyleSheet("color: #8a8a8a; font-size: 11px;")


class VaultPanel(QWidget):
    """
    设备库 (Vault) 面板

    信号:
        connect_requested(dict):  请求连接某台设备（参数为连接配置）
        add_requested():          请求新建连接
        edit_requested(dict):     请求编辑某台设备
        delete_requested(dict):   请求删除某台设备
        selection_changed(dict):  选中设备变化（参数为设备配置，或 None）
    """

    connect_requested = pyqtSignal(dict)
    add_requested = pyqtSignal()
    edit_requested = pyqtSignal(dict)
    delete_requested = pyqtSignal(dict)
    selection_changed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._devices = []
        self._connected_keys = set()
        self._selected_key = None
        self.init_ui()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # 标题
        title_row = QHBoxLayout()
        title = QLabel("设备库  Vault")
        title.setStyleSheet("color: #9cdcfe; font-weight: bold; font-size: 13px;")
        title_row.addWidget(title)
        title_row.addStretch()
        self.count_label = QLabel("0 台")
        self.count_label.setStyleSheet("color: #8a8a8a; font-size: 11px;")
        title_row.addWidget(self.count_label)
        layout.addLayout(title_row)

        # 设备列表
        self.list_widget = QListWidget()
        self.list_widget.setSpacing(4)
        self.list_widget.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        layout.addWidget(self.list_widget, 1)

        # 按钮
        row1 = QHBoxLayout()
        self.btn_connect = QPushButton("连接")
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_add = QPushButton("新建")
        self.btn_add.clicked.connect(lambda: self.add_requested.emit())
        row1.addWidget(self.btn_connect)
        row1.addWidget(self.btn_add)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_edit = QPushButton("编辑")
        self.btn_edit.clicked.connect(self.on_edit)
        self.btn_delete = QPushButton("删除")
        self.btn_delete.clicked.connect(self.on_delete)
        row2.addWidget(self.btn_edit)
        row2.addWidget(self.btn_delete)
        layout.addLayout(row2)

        hint = QLabel("双击设备即可连接")
        hint.setStyleSheet("color: #6a6a6a; font-size: 10px;")
        layout.addWidget(hint)

        self._update_buttons()

    # ------------------------------------------------------------ 数据
    def set_devices(self, devices: list):
        """用新的设备列表刷新界面"""
        self._devices = list(devices or [])
        self.list_widget.clear()
        for config in self._devices:
            self._append_card(config)
        self.count_label.setText(f"{len(self._devices)} 台")
        self._refresh_status_dots()
        self._update_buttons()

    def _append_card(self, config: dict):
        card = DeviceCard(config)
        item = QListWidgetItem(self.list_widget)
        item.setData(Qt.ItemDataRole.UserRole, config)
        item.setSizeHint(QSize(0, 46))
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(item, card)
        card.set_connected(_device_key(config) in self._connected_keys)

    def selected_config(self) -> dict:
        item = self.list_widget.currentItem()
        if item is None:
            return {}
        return item.data(Qt.ItemDataRole.UserRole) or {}

    def set_connected(self, config: dict, connected: bool = True):
        """标记某台设备为已连接 / 已断开，并刷新状态点"""
        key = _device_key(config)
        if connected:
            self._connected_keys.add(key)
        else:
            self._connected_keys.discard(key)
        self._refresh_status_dots()

    def _refresh_status_dots(self):
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            config = item.data(Qt.ItemDataRole.UserRole)
            widget = self.list_widget.itemWidget(item)
            if widget:
                widget.set_connected(_device_key(config) in self._connected_keys)

    def refresh(self):
        """重新拉取设置里的设备库（外部数据变化时调用）"""
        from config.settings import Settings
        self.set_devices(Settings().get_vault_devices())

    # ------------------------------------------------------------ 交互
    def on_item_clicked(self, item: QListWidgetItem):
        config = item.data(Qt.ItemDataRole.UserRole) or {}
        self._selected_key = _device_key(config)
        self._highlight_selection()
        self.selection_changed.emit(config)

    def on_item_double_clicked(self, item: QListWidgetItem):
        config = item.data(Qt.ItemDataRole.UserRole) or {}
        if config:
            self.connect_requested.emit(config)

    def on_connect(self):
        config = self.selected_config()
        if config:
            self.connect_requested.emit(config)

    def on_edit(self):
        config = self.selected_config()
        if config:
            self.edit_requested.emit(config)

    def on_delete(self):
        config = self.selected_config()
        if config:
            self.delete_requested.emit(config)

    def _highlight_selection(self):
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            config = item.data(Qt.ItemDataRole.UserRole)
            widget = self.list_widget.itemWidget(item)
            if widget:
                widget.set_selected(_device_key(config) == self._selected_key)

    def _update_buttons(self):
        has = (self.list_widget.currentItem() is not None)
        self.btn_connect.setEnabled(has)
        self.btn_edit.setEnabled(has)
        self.btn_delete.setEnabled(has)
