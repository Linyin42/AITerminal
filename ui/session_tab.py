"""
会话标签页 - 终端 + 会话信息条
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QDialog, QPlainTextEdit, QFileDialog, QMessageBox, QApplication
)
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QFont

from ui.terminal_widget import TerminalWidget


class BatchCommandDialog(QDialog):
    """
    批量命令输入框

    不用 QInputDialog：它的尺寸不受控（每次弹出大小不一），也没法统一成
    深色主题。这里按屏幕可用区域的比例定尺寸，任何显示器上都保持合适大小。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("批量命令")
        self.setModal(True)
        self.init_ui()
        self._fit_to_screen()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        hint = QLabel("每行一条命令，将依次下发：")
        hint.setStyleSheet("color: #E6EAF2; font-size: 13px;")
        layout.addWidget(hint)

        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText(
            "例如：\nshow version\nshow ip interface brief\n\n（每行一条命令；空行会被忽略）")
        edit_font = QFont()
        edit_font.setFamilies(["Cascadia Mono", "Consolas", "Microsoft YaHei", "monospace"])
        edit_font.setPointSize(10)
        self.edit.setFont(edit_font)
        self.edit.setStyleSheet(
            "QPlainTextEdit { background-color: #0E1117; color: #E6EAF2;"
            " border: 1px solid #242931; border-radius: 6px; padding: 8px 10px;"
            " selection-background-color: #1C2A42; }"
            "QPlainTextEdit:focus { border-color: #4C8DFF; }"
        )
        layout.addWidget(self.edit, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton("发送")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)

        self.btn_ok.setStyleSheet(
            "QPushButton { background-color: #4C8DFF; color: #0E1117; border: none;"
            " border-radius: 6px; padding: 6px 20px; font-weight: 600; }"
            "QPushButton:hover { background-color: #4C8DFF; }"
            "QPushButton:pressed { background-color: #3F7DE8; }"
        )
        self.btn_cancel.setStyleSheet(
            "QPushButton { background-color: transparent; color: #E6EAF2;"
            " border: 1px solid #33373E; border-radius: 6px; padding: 6px 20px; }"
            "QPushButton:hover { background-color: #242931;"
            " color: #E6EAF2; }"
        )
        for button in (self.btn_cancel, self.btn_ok):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumWidth(88)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_ok)
        layout.addLayout(row)

    def _fit_to_screen(self):
        """按当前屏幕可用区域的固定比例定尺寸，并留出上下限"""
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        if area is None:
            self.resize(720, 520)
            return
        width = max(560, min(int(area.width() * 0.42), 1000))
        height = max(420, min(int(area.height() * 0.55), 800))
        self.resize(width, height)

    def text(self) -> str:
        """返回输入的命令文本"""
        return self.edit.toPlainText()


class SessionTab(QWidget):
    """
    会话标签页

    结构:
        顶部信息条: 会话名 / 协议 / 目标 / 连接状态 / 快捷按钮
        主体: TerminalWidget
    """

    command_entered = pyqtSignal(str)
    interrupt_requested = pyqtSignal()
    raw_input = pyqtSignal(str)
    echo_auto_off = pyqtSignal()

    def __init__(self, session_id: int, name: str, info_text: str = "", parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self.session_name = name
        # 上次的连接状态：仅在变化时才改样式表（setStyleSheet 会触发样式重算，
        # 每 50ms 无条件调用会拖慢 UI，长输出时会明显发卡）
        self._last_connected = None
        self.init_ui(name, info_text)
        self.setProperty('session_id', session_id)

    # ------------------------------------------------------------ 界面
    def init_ui(self, name: str, info_text: str):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部信息条
        bar = QWidget()
        # 条内的按钮用紧凑样式：这里是"细长信息条"，不该塞一排大按钮显得像工具栏
        bar.setStyleSheet(
            "background-color: #151A23; border-bottom: 1px solid #242931;"
            " QPushButton { padding: 3px 9px; font-size: 11px; min-height: 16px;"
            "   border-radius: 6px; background: transparent;"
            "   border: 1px solid #242931; color: #E6EAF2;"
            "   font-weight: 550; }"
            " QPushButton:hover { background: #1C2A42;"
            "   border-color: #33373E; color: #E6EAF2; }"
            " QPushButton:pressed { background: #1C2A42;"
            "   border-color: #4C8DFF; color: #4C8DFF; }"
        )
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(8, 4, 8, 4)
        bar_layout.setSpacing(6)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #4C8DFF;")
        bar_layout.addWidget(self.status_dot)

        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("color: #E6EAF2; font-weight: bold;")
        bar_layout.addWidget(self.name_label)

        self.info_label = QLabel(info_text)
        self.info_label.setStyleSheet("color: #93A0B4;")
        bar_layout.addWidget(self.info_label)
        bar_layout.addStretch()

        self.btn_paste = QPushButton("批量命令")
        self.btn_paste.setToolTip("粘贴多行命令，逐行下发")
        self.btn_paste.clicked.connect(self.on_paste_commands)
        bar_layout.addWidget(self.btn_paste)

        self.btn_interrupt = QPushButton("Ctrl+C")
        self.btn_interrupt.setToolTip("发送中断信号")
        self.btn_interrupt.clicked.connect(self.interrupt_requested.emit)
        bar_layout.addWidget(self.btn_interrupt)

        self.btn_clear = QPushButton("清屏")
        self.btn_clear.clicked.connect(self.clear)
        bar_layout.addWidget(self.btn_clear)

        self.btn_save = QPushButton("保存日志")
        self.btn_save.clicked.connect(self.save_log)
        bar_layout.addWidget(self.btn_save)

        layout.addWidget(bar)

        # 终端主体
        self.terminal = TerminalWidget(self.session_id)
        self.terminal.command_entered.connect(self.command_entered.emit)
        self.terminal.interrupt_requested.connect(self.interrupt_requested.emit)
        self.terminal.raw_input.connect(self.raw_input.emit)
        self.terminal.echo_auto_off.connect(self.echo_auto_off.emit)
        layout.addWidget(self.terminal, 1)

    # ------------------------------------------------------------ 行为
    def append_output(self, text: str):
        self.terminal.append_output(text)

    def set_status(self, connected: bool, text: str = ""):
        """更新连接状态指示灯"""
        # 只在状态真正变化时设样式表，避免每次刷新都触发样式重算
        if connected != self._last_connected:
            self._last_connected = connected
            if connected:
                self.status_dot.setStyleSheet("color: #4C8DFF;")
                self.status_dot.setToolTip("已连接")
            else:
                self.status_dot.setStyleSheet("color: #F85149;")
                self.status_dot.setToolTip("已断开")
        if text:
            self.info_label.setText(text)

    def set_ai_badge(self, enabled: bool):
        """在标题上标注 AI 状态"""
        suffix = "  [AI]" if enabled else ""
        self.name_label.setText(self.session_name + suffix)

    def clear(self):
        self.terminal.clear()

    def on_paste_commands(self):
        """弹出多行命令输入框并逐行下发"""
        dialog = BatchCommandDialog(self)
        if dialog.exec() and dialog.text().strip():
            self.terminal.send_paste(dialog.text())

    def save_log(self):
        """把当前终端内容保存为文本文件"""
        path, _ = QFileDialog.getSaveFileName(
            self, "保存会话日志",
            f"{self.session_name}.log",
            "日志文件 (*.log);;文本文件 (*.txt);;所有文件 (*)"
        )
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self.terminal.text())
            QMessageBox.information(self, "保存成功", f"日志已保存到:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
