"""
会话标签页 - 终端 + 会话信息条
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QInputDialog, QFileDialog, QMessageBox
)
from PyQt6.QtCore import pyqtSignal, Qt

from ui.terminal_widget import TerminalWidget


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

    def __init__(self, session_id: int, name: str, info_text: str = "", parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self.session_name = name
        self.init_ui(name, info_text)
        self.setProperty('session_id', session_id)

    # ------------------------------------------------------------ 界面
    def init_ui(self, name: str, info_text: str):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部信息条
        bar = QWidget()
        bar.setStyleSheet("background-color: #2d2d30;")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(8, 4, 8, 4)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #4ec9b0;")
        bar_layout.addWidget(self.status_dot)

        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("color: #d4d4d4; font-weight: bold;")
        bar_layout.addWidget(self.name_label)

        self.info_label = QLabel(info_text)
        self.info_label.setStyleSheet("color: #8a8a8a;")
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
        layout.addWidget(self.terminal, 1)

    # ------------------------------------------------------------ 行为
    def append_output(self, text: str):
        self.terminal.append_output(text)

    def set_status(self, connected: bool, text: str = ""):
        """更新连接状态指示灯"""
        if connected:
            self.status_dot.setStyleSheet("color: #4ec9b0;")
            self.status_dot.setToolTip("已连接")
        else:
            self.status_dot.setStyleSheet("color: #f14c4c;")
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
        text, ok = QInputDialog.getMultiLineText(
            self, "批量命令", "每行一条命令，将依次下发：", ""
        )
        if ok and text.strip():
            self.terminal.send_paste(text)

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
