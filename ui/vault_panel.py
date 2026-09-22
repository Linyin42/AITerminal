"""
设备库面板 (Vault) —— 参考 NetCatty 风格

每台设备是一张大圆角卡片，左侧有品牌色圆形图标（按厂商/协议着色），
右侧上方是设备名、下方是 user@host:port，已连接时右侧出现一个绿点。
双击连接，右键弹出菜单（连接 / 编辑 / 删除）。
顶部有搜索框与 "+ 新建" 青色按钮。
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QLineEdit, QScrollArea, QSizePolicy, QMenu
)


def _device_key(cfg: dict) -> tuple:
    host = cfg.get('host') or cfg.get('serial_port') or ''
    return (cfg.get('protocol'), host, cfg.get('port'))


def _icon_letter(cfg: dict) -> str:
    v = (cfg.get('vendor') or '').lower()
    p = (cfg.get('protocol') or '').lower()
    if v == 'h3c':
        return 'H'
    if v in ('huawei', 'vrp', 'hw'):
        return '华'
    if v == 'cisco':
        return 'C'
    if v == 'ruijie':
        return 'R'
    if v == 'h3c':
        return 'H'
    if p == 'telnet':
        return 'T'
    if p == 'serial':
        return 'S'
    return 'S'  # SSH default


def _icon_color(cfg: dict) -> str:
    v = (cfg.get('vendor') or '').lower()
    p = (cfg.get('protocol') or '').lower()
    if v == 'h3c' or v in ('huawei', 'vrp', 'hw'):
        return '#f97316'  # 橙
    if v == 'cisco':
        return '#3b82f6'  # 蓝
    if v == 'ruijie':
        return '#a855f7'  # 紫
    if p == 'telnet':
        return '#f59e0b'  # 琥珀
    if p == 'serial':
        return '#10b981'  # 翠绿
    return '#7c8cf8'      # 靛蓝（默认 SSH，与主题强调色一致）


def _subtitle(cfg: dict) -> str:
    p = cfg.get('protocol', 'SSH')
    if p == 'Serial':
        host = cfg.get('serial_port') or 'COM?'
    else:
        host = cfg.get('host') or ''
        port = cfg.get('port')
        if host and port:
            host = f"{host}:{port}"
    user = cfg.get('username') or 'root'
    if host:
        return f"{user}@{host}"
    return p


class HostCard(QFrame):
    """一张大圆角设备卡片"""
    clicked = pyqtSignal(dict)
    double_clicked = pyqtSignal(dict)
    context_requested = pyqtSignal(dict, object)  # config, global QPoint

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.config = config
        self.setObjectName("HostCard")
        self.setFixedHeight(72)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.selected = False
        self.connected = False
        self._build()
        self._apply_style()

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # 品牌色圆形图标
        self.icon = QLabel(_icon_letter(self.config))
        self.icon.setObjectName("HostCardIcon")
        self.icon.setFixedSize(44, 44)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon)

        # 文字
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        name = self.config.get('name') or self.config.get('host') or self.config.get('serial_port') or '未命名'
        self.title = QLabel(name)
        self.title.setObjectName("HostCardTitle")
        self.sub = QLabel(_subtitle(self.config))
        self.sub.setObjectName("HostCardSub")
        col.addWidget(self.title)
        col.addWidget(self.sub)
        layout.addLayout(col, 1)

        # 已连接指示点
        self.dot = QLabel("●")
        self.dot.setObjectName("HostCardDot")
        self.dot.setFixedWidth(14)
        self.dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dot.hide()
        layout.addWidget(self.dot)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context)

    def _apply_style(self):
        if self.selected:
            bg, border = '#232a45', '#7c8cf8'
        else:
            bg, border = '#1e222f', '#2b3143'
        self.setStyleSheet(
            f"QFrame#HostCard {{ background:{bg}; border:1px solid {border}; border-radius:12px; }}"
            f"QFrame#HostCard:hover {{ background:#242938; border-color:#39415a; }}"
            f"QLabel#HostCardIcon {{ background:{_icon_color(self.config)}; color:#ffffff;"
            f" border-radius:22px; font-weight:bold; font-size:16px; }}"
            f"QLabel#HostCardTitle {{ color:#e6e9f2; font-weight:bold; font-size:13px;"
            f" background:transparent; border:none; }}"
            f"QLabel#HostCardSub {{ color:#98a1b5; font-size:11px;"
            f" background:transparent; border:none; }}"
            f"QLabel#HostCardDot {{ color:#3fb950; font-size:14px;"
            f" background:transparent; border:none; }}"
        )

    def set_selected(self, selected: bool):
        self.selected = selected
        self._apply_style()

    def set_connected(self, connected: bool):
        self.connected = connected
        self.dot.setVisible(connected)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.config)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self.config)
        super().mouseDoubleClickEvent(event)

    def _on_context(self, pos):
        self.context_requested.emit(self.config, self.mapToGlobal(pos))


class VaultPanel(QWidget):
    """
    设备库面板

    信号:
        connect_requested(dict):  请求连接
        add_requested():          请求新建连接
        edit_requested(dict):     请求编辑
        delete_requested(dict):   请求删除
        selection_changed(dict):  选中设备变化
    """

    connect_requested = pyqtSignal(dict)
    add_requested = pyqtSignal()
    edit_requested = pyqtSignal(dict)
    delete_requested = pyqtSignal(dict)
    selection_changed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("VaultPanel")
        self._devices = []
        self._connected_keys = set()
        self._selected_key = None
        self._init_ui()

    # ------------------------------------------------------------ 界面
    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)

        # 顶部：标题 + 搜索 + 新建按钮
        header = QHBoxLayout()
        header.setSpacing(8)
        self.title = QLabel("设备库")
        self.title.setObjectName("VaultTitle")
        self.count = QLabel("0 台")
        self.count.setObjectName("VaultCount")
        header.addWidget(self.title)
        header.addSpacing(6)
        header.addWidget(self.count)
        header.addStretch()
        self.search = QLineEdit()
        self.search.setObjectName("VaultSearch")
        self.search.setPlaceholderText("搜索主机 / IP…")
        self.search.setFixedWidth(120)
        self.search.textChanged.connect(self._apply_filter)
        header.addWidget(self.search)
        self.btn_new = QPushButton("+ 新建")
        self.btn_new.setObjectName("VaultPrimaryBtn")
        self.btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new.clicked.connect(self.add_requested.emit)
        header.addWidget(self.btn_new)
        root.addLayout(header)

        # 分组标签
        self.section = QLabel("全部主机")
        self.section.setObjectName("VaultSection")
        root.addWidget(self.section)

        # 卡片列表
        self.scroll = QScrollArea()
        self.scroll.setObjectName("VaultScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cards_host = QWidget()
        self.cards_host.setObjectName("VaultCardsHost")
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addStretch(1)
        self.scroll.setWidget(self.cards_host)
        root.addWidget(self.scroll, 1)

        # 底部提示
        hint = QLabel("双击连接 · 右键菜单")
        hint.setObjectName("VaultHint")
        root.addWidget(hint)

    # ------------------------------------------------------------ 数据
    def set_devices(self, devices: list):
        self._devices = list(devices or [])
        self._rebuild_cards()

    def _rebuild_cards(self):
        # 清理旧卡片（保留最后的 stretch）
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for cfg in self._devices:
            card = HostCard(cfg)
            card.clicked.connect(self._on_card_clicked)
            card.double_clicked.connect(lambda c=cfg: self.connect_requested.emit(c))
            card.context_requested.connect(self._show_card_menu)
            # 插在 stretch 之前
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
        self.count.setText(f"{len(self._devices)} 台")
        self._refresh_connected()
        self._apply_filter(self.search.text())

    def _on_card_clicked(self, cfg: dict):
        self._selected_key = _device_key(cfg)
        self._refresh_selection()
        self.selection_changed.emit(cfg)

    def _refresh_selection(self):
        for i in range(self.cards_layout.count()):
            item = self.cards_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, HostCard):
                w.set_selected(_device_key(w.config) == self._selected_key)

    def _show_card_menu(self, cfg: dict, global_pos):
        menu = QMenu(self)
        menu.addAction("连接")
        menu.addSeparator()
        menu.addAction("编辑")
        menu.addAction("删除")
        act = menu.exec(global_pos)
        if act is None:
            return
        text = act.text()
        if text == "连接":
            self.connect_requested.emit(cfg)
        elif text == "编辑":
            self.edit_requested.emit(cfg)
        elif text == "删除":
            self.delete_requested.emit(cfg)

    def set_connected(self, config: dict, connected: bool = True):
        key = _device_key(config)
        if connected:
            self._connected_keys.add(key)
        else:
            self._connected_keys.discard(key)
        self._refresh_connected()

    def _refresh_connected(self):
        for i in range(self.cards_layout.count()):
            item = self.cards_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, HostCard):
                w.set_connected(_device_key(w.config) in self._connected_keys)

    def refresh(self):
        from config.settings import Settings
        self.set_devices(Settings().get_vault_devices())

    def selected_config(self) -> dict:
        for i in range(self.cards_layout.count()):
            item = self.cards_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, HostCard) and _device_key(w.config) == self._selected_key:
                return w.config
        return {}

    # ------------------------------------------------------------ 过滤
    def _apply_filter(self, text: str):
        text = (text or '').strip().lower()
        shown = 0
        for i in range(self.cards_layout.count()):
            item = self.cards_layout.itemAt(i)
            w = item.widget() if item else None
            if not isinstance(w, HostCard):
                continue
            if not text:
                w.show()
                shown += 1
                continue
            c = w.config
            hay = ' '.join([
                str(c.get('name', '')),
                str(c.get('host', '')),
                str(c.get('serial_port', '')),
                str(c.get('username', '')),
                str(c.get('vendor', '')),
            ]).lower()
            ok = text in hay
            w.setVisible(ok)
            if ok:
                shown += 1
        if text:
            self.count.setText(f"{shown} / {len(self._devices)} 台")
        else:
            self.count.setText(f"{len(self._devices)} 台")