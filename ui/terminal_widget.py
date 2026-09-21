"""
终端控件 - 显示设备输出
"""
from PyQt6.QtWidgets import QTextEdit, QVBoxLayout, QWidget, QLabel, QHBoxLayout
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QFont, QTextCursor

from core.terminal import TerminalBuffer
from ui.output_highlighter import OutputHighlighter


class TerminalWidget(QWidget):
    """
    终端显示控件

    功能:
        显示设备输出（自动剥离ANSI、处理\\r覆盖与\\b退格）
        捕获用户输入并发送命令
        支持命令历史（上下键）
        底部输入回显条，实时显示当前正在输入的命令
    """

    command_entered = pyqtSignal(str)
    interrupt_requested = pyqtSignal()
    raw_input = pyqtSignal(str)

    def __init__(self, session_id: int, parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self.command_buffer = ""
        self.command_history = []
        self.history_index = -1
        self.screen = TerminalBuffer()
        self.local_echo = False   # Console 设备常不回显，需要本地回显
        self.init_ui()
        self.setProperty('session_id', session_id)

    def set_local_echo(self, enabled: bool):
        """本地回显开关（串口 Console 建议开启）"""
        self.local_echo = bool(enabled)
        self._refresh_input_bar()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 文本显示区
        self.text_edit = QTextEdit()
        # 等宽字体栈：优先现代编程字体，回退 Consolas（Windows 必有）与雅黑（中文）
        term_font = QFont()
        term_font.setFamilies(
            ["Cascadia Mono", "JetBrains Mono", "Consolas",
             "Microsoft YaHei", "monospace"]
        )
        term_font.setPointSize(10)
        term_font.setStyleHint(QFont.StyleHint.Monospace)
        self.text_edit.setFont(term_font)
        self.text_edit.setReadOnly(False)
        self.text_edit.setAcceptRichText(False)
        self.text_edit.setUndoRedoEnabled(False)
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #1b2027;
                color: #c9d1d9;
                border: none;
                selection-background-color: rgba(34, 211, 238, 0.28);
            }
        """)

        # 输出着色：把错误/UP/DOWN/IP/MAC/接口名标出来（见 output_highlighter.py）
        self.highlighter = OutputHighlighter(self.text_edit.document())

        # 重写按键处理
        self.text_edit.keyPressEvent = self.handle_key_press
        layout.addWidget(self.text_edit, 1)

        # 输入回显条
        bar = QWidget()
        bar.setStyleSheet("background-color: #21262d; border-top: 1px solid #3a4049;")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(6, 2, 6, 2)
        self.input_label = QLabel("")
        self.input_label.setFont(QFont("Consolas", 10))
        self.input_label.setStyleSheet("color: #67e8f9;")
        bar_layout.addWidget(self.input_label)
        bar_layout.addStretch()
        self.hint_label = QLabel("Enter 发送 · ↑↓ 历史 · Ctrl+C 中断 · Ctrl+L 清屏")
        self.hint_label.setStyleSheet("color: #6b7480; font-size: 11px;")
        bar_layout.addWidget(self.hint_label)
        layout.addWidget(bar)

        self._refresh_input_bar()

    # ------------------------------------------------------------ 按键
    def handle_key_press(self, event):
        """处理按键"""
        key = event.key()
        text = event.text()
        mods = event.modifiers()

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.command_buffer:
                self.command_history.append(self.command_buffer)
                self.history_index = len(self.command_history)
                if self.local_echo:
                    self._echo_text("\n")
                self.command_entered.emit(self.command_buffer)
                self.command_buffer = ""
            else:
                if self.local_echo:
                    self._echo_text("\n")
                self.raw_input.emit("\n")
            self._refresh_input_bar()
            return

        if key == Qt.Key.Key_Backspace:
            if self.command_buffer:
                self.command_buffer = self.command_buffer[:-1]
                if self.local_echo:
                    self._echo_backspace()
            self._refresh_input_bar()
            return

        if key == Qt.Key.Key_Up:
            if self.command_history and self.history_index > 0:
                self.history_index -= 1
                self.command_buffer = self.command_history[self.history_index]
            self._refresh_input_bar()
            return

        if key == Qt.Key.Key_Down:
            if self.history_index < len(self.command_history) - 1:
                self.history_index += 1
                self.command_buffer = self.command_history[self.history_index]
            else:
                self.history_index = len(self.command_history)
                self.command_buffer = ""
            self._refresh_input_bar()
            return

        if key == Qt.Key.Key_C and mods & Qt.KeyboardModifier.ControlModifier:
            self.interrupt_requested.emit()
            return

        if key == Qt.Key.Key_L and mods & Qt.KeyboardModifier.ControlModifier:
            self.clear()
            return

        if key == Qt.Key.Key_Tab:
            self.raw_input.emit("\t")
            return

        if text and text.isprintable():
            self.command_buffer += text
            if self.local_echo:
                self._echo_text(text)
            self._refresh_input_bar()
            return

        # 其余按键交回默认处理（复制/全选等）
        QTextEdit.keyPressEvent(self.text_edit, event)

    def _echo_text(self, text: str):
        """本地回显：把输入内容写进终端显示区"""
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.text_edit.setTextCursor(cursor)
        self.text_edit.insertPlainText(text)
        self.text_edit.ensureCursorVisible()

    def _echo_backspace(self):
        """本地回显：退格时删掉显示区最后一个字符"""
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.text_edit.setTextCursor(cursor)
        if self.text_edit.toPlainText():
            cursor.deletePreviousChar()

    def _refresh_input_bar(self):
        """刷新底部输入回显"""
        self.input_label.setText("❯ " + self.command_buffer + "▌")

    # ------------------------------------------------------------ 输出
    def append_output(self, text: str):
        """添加输出（自动处理ANSI与回车覆盖）"""
        if not text:
            return
        clean_text = self.screen.feed(text)
        if not clean_text:
            return
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.text_edit.setTextCursor(cursor)
        self.text_edit.insertPlainText(clean_text)
        self.text_edit.ensureCursorVisible()

    def send_paste(self, text: str):
        """粘贴多行文本，逐行下发"""
        for line in text.splitlines():
            line = line.strip()
            if line:
                self.command_entered.emit(line)

    def clear(self):
        """清空终端"""
        self.text_edit.clear()
        self.screen.clear()

    def text(self) -> str:
        """返回当前屏幕文本（用于保存日志）"""
        return self.text_edit.toPlainText()

    def set_session_id(self, session_id: int):
        self.session_id = session_id
        self.setProperty('session_id', session_id)
