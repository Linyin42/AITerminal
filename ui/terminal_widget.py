"""
终端控件 - 显示设备输出
"""
import time

from PyQt6.QtWidgets import (
    QTextEdit, QVBoxLayout, QHBoxLayout, QWidget,
    QLineEdit, QPushButton, QLabel,
)
from PyQt6.QtCore import QEvent, pyqtSignal, Qt
from PyQt6.QtGui import QFont, QFontMetricsF, QGuiApplication, QTextCursor, QTextDocument

from core.terminal import TerminalBuffer, looks_like_prompt
from ui.command_hint import CommandDictionary, CommandHintPopup
from ui.output_highlighter import OutputHighlighter


class TerminalDisplay(QTextEdit):
    """终端显示区

    键盘事件全部转交给 TerminalWidget 处理。特别要在 event() 这一层截住
    Tab / Shift+Tab：Qt 的 QWidget::event() 对 Tab 的处理**优先于**
    keyPressEvent —— 只要焦点链里还有下一个可聚焦控件，Tab 就会被它吃掉
    用于跳转焦点（跳到会话标签栏 / 设备库搜索框），终端既收不到 Tab，
    之后还会失去焦点，表现为"按 Tab 光标跑到别处、接着敲字没反应"。
    """

    def __init__(self, widget):
        super().__init__()
        self._widget = widget
        # 块状插入光标的正常宽度（≈一个字符宽），拖动框选期间临时设成 0
        self._caret_width = 8

    def set_term_caret_width(self, px: int):
        """由 TerminalWidget 按当前字号设置块光标宽度"""
        self._caret_width = max(0, int(px))
        super().setCursorWidth(self._caret_width)

    def keyPressEvent(self, event):
        self._widget.handle_key_press(event)

    def wheelEvent(self, event):
        """Ctrl+滚轮 = 缩放字体（和 IDE / 浏览器一致）"""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._widget.zoom_font(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event):
        """按下鼠标：藏起插入光标；按在已有选区里就先清选区

        两件事都是为了贴近主流终端（Windows Terminal / iTerm2 / VS Code 终端）：
        1. **拖动框选时不能有插入光标跟着鼠标跑**。这块控件里的插入光标只表示
           "接下来敲的字会打到哪儿"，让它跟着拖动走，用户会以为输入位置变了
           （实测反馈："拖拽的时候光标图案也跟着移动"）。按下即藏、松开再显示。
        2. 鼠标按在**已有选区**上时先清掉选区、重新开始框选。不清的话 Qt 会把这次
           拖动理解成"把选中的文字拖出去"，跟着鼠标拖出一个半透明的幽灵图标。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            super().setCursorWidth(0)
            mods = event.modifiers()
            if not (mods & (Qt.KeyboardModifier.ShiftModifier
                            | Qt.KeyboardModifier.ControlModifier)):
                cursor = self.textCursor()
                if cursor.hasSelection():
                    pos = self.cursorForPosition(event.position().toPoint()).position()
                    if cursor.selectionStart() <= pos <= cursor.selectionEnd():
                        cursor.clearSelection()
                        cursor.setPosition(pos)
                        self.setTextCursor(cursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        """松开鼠标后，恢复光标宽度，再把插入光标拉回输入位置

        插入光标在这块控件里只有一个含义：**接下来敲的字会打到哪儿**，所以它
        必须始终停在输入位置（末尾）。QTextEdit 的默认行为是"点哪儿光标就去
        哪儿"，于是屏幕上会多出一个闪着、却和输入毫无关系的块状光标。
        正在框选设备输出时不打扰（选中要留着给复制用）。
        """
        super().mouseReleaseEvent(event)
        super().setCursorWidth(self._caret_width)
        self.pin_caret_to_input()

    def focusOutEvent(self, event):
        """焦点跑掉时（拖到窗口外松手之类）也要把光标宽度还回来，别永久消失"""
        super().focusOutEvent(event)
        super().setCursorWidth(self._caret_width)

    def pin_caret_to_input(self):
        """没在选中文本时，把插入光标拉回末尾（见 mouseReleaseEvent 注释）"""
        cursor = self.textCursor()
        if cursor.hasSelection():
            return
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

    def event(self, event):
        if (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)):
            self._widget.handle_key_press(event)
            return True          # 吃掉，别让 Qt 拿去做焦点跳转
        return super().event(event)


class TerminalWidget(QWidget):
    """
    终端显示控件

    功能:
        显示设备输出（自动剥离ANSI、处理\\r覆盖与\\b退格）
        捕获用户输入并下发，支持命令历史（上下键）、Tab 补全
        边敲边给出后续命令建议（像 IDE 的自动补全）
    """

    command_entered = pyqtSignal(str)
    interrupt_requested = pyqtSignal()
    raw_input = pyqtSignal(str)
    # 设备用回显证明了自己会回显 ⇒ 本地回显被自动撤掉（见 _drop_duplicate_echo）
    echo_auto_off = pyqtSignal()

    #: 按 Tab 后等待设备回整行的时长（秒）。设备回整行可能晚一两个数据包才到，
    #: 单靠"下一批数据"当开关会漏掉，所以用一个短窗口兜住。
    TAB_WINDOW = 1.5

    #: 终端等宽字体栈 + 默认字号（Ctrl+滚轮 / Ctrl+= 可缩放）
    #: Windows 优先 Cascadia/Consolas，Linux 优先 DejaVu/Noto/Liberation（发行版多半预装），
    #: 末尾 monospace 是 Qt 的通用兜底名，保证任何平台都有等宽字体。
    TERM_FONT_FAMILIES = ("Cascadia Mono", "JetBrains Mono", "Consolas",
                          "DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono",
                          "Menlo", "Ubuntu Mono",
                          "Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC",
                          "monospace")
    BASE_FONT_SIZE = 10

    def __init__(self, session_id: int, parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self.command_buffer = ""
        self._font_size = self.BASE_FONT_SIZE
        self.command_history = []
        self.history_index = -1
        self.screen = TerminalBuffer()
        self.local_echo = False   # 设备自身不回显时才需要本地回显
        # 本地回显过、还没得到设备确认的字符：[字符, 时刻]（见 _drop_duplicate_echo）
        self._echo_pending = []
        self.ECHO_CONFIRM_WINDOW = 0.6
        self.line_ending = "\r"   # 回车下发的行结束符，由主窗口按协议注入
        # 当前行的提示符（如 "Ruijie>" / "<H3C>"）。设备重画当前行（Tab 补全、
        # 设备端回写）时，用它从屏幕上那一行反推出用户实际输入的内容 ——
        # 输入缓冲靠这个保持与终端一致，否则按回车会执行错东西。
        self._prompt = ""
        # 上次按 Tab 的时刻：窗口期内设备回的是"整行重画"，需要吃掉开头的换行
        # 改为就地改写（否则屏上会堆出一行行重复的提示符）
        self._tab_at = 0.0
        # 命令建议：候选来自本会话执行过的命令 + 厂商常用命令
        self.dictionary = CommandDictionary()
        self.hint_popup = None
        self.init_ui()
        self.setProperty('session_id', session_id)

    def set_vendor(self, vendor: str):
        """按识别出的设备厂商换一批常用命令建议"""
        self.dictionary.set_vendor(vendor)

    def load_commands(self, commands):
        """装载按本机型号/版本得到的命令清单（建议里会优先出现）"""
        for command in commands or []:
            self.dictionary.add(command)

    def set_local_echo(self, enabled: bool):
        """
        本地回显开关。

        开启：输入直接内联显示在终端正文里（设备自己不回显时用）。
        关闭（默认）：正文等设备回显，不在本地重复显示一遍，避免重影。
        """
        self.local_echo = bool(enabled)
        if not self.local_echo:
            self._echo_pending = []

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 文本显示区
        self.text_edit = TerminalDisplay(self)
        self.text_edit.setAcceptRichText(False)
        self.text_edit.setUndoRedoEnabled(False)
        self.text_edit.setAcceptDrops(False)
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #0E1117;
                color: #E6EAF2;
                border: none;
                selection-background-color: #1C2A42;
            }
        """)
        self._apply_term_font()

        # 输出着色：把错误/UP/DOWN/IP/MAC/接口名标出来（见 output_highlighter.py）
        self.highlighter = OutputHighlighter(self.text_edit.document())

        # 查找条：默认收起，Ctrl+F 唤出（像 IDE 一样浮在编辑区顶部）
        self.find_bar = self._build_find_bar()
        layout.addWidget(self.find_bar)
        self.find_bar.hide()

        layout.addWidget(self.text_edit, 1)
        # 容器收到焦点（setFocus/键盘导航）时重定向到 text_edit，
        # 否则真实键盘事件落在容器默认 handler 上，敲键没有反应
        self.setFocusProxy(self.text_edit)

        # 命令建议浮层：终端控件的子控件，浮在文本区上方
        self.hint_popup = CommandHintPopup(self)
        self.hint_popup.accepted.connect(self._accept_hint)

    # ------------------------------------------------------------ 字号 / 查找
    def _apply_term_font(self):
        """按当前字号重建终端字体（含整块光标宽度）

        光标做成整块（像 Linux 终端那样）：默认 1px 细线在深色终端里几乎看不见。
        注意不能把显示区设成只读 —— 只读的 QTextEdit 不画插入光标，就没光标了。
        """
        font = QFont()
        font.setFamilies(list(self.TERM_FONT_FAMILIES))
        font.setPointSize(self._font_size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.text_edit.setFont(font)
        self.text_edit.set_term_caret_width(
            max(2, int(QFontMetricsF(font).horizontalAdvance("0"))))

    def zoom_font(self, delta: int):
        """缩放终端字号：Ctrl+= 放大 / Ctrl+- 缩小 / Ctrl+0 还原 / Ctrl+滚轮"""
        size = self.BASE_FONT_SIZE if delta == 0 else self._font_size + delta
        size = max(7, min(28, size))
        if size == self._font_size:
            return
        self._font_size = size
        self._apply_term_font()

    def _build_find_bar(self) -> QWidget:
        """编辑区顶部的查找条（默认收起，Ctrl+F 唤出）"""
        bar = QWidget()
        bar.setObjectName("FindBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 3, 6, 3)
        row.setSpacing(4)

        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText(
            "查找设备输出…（Enter 下一个 / Shift+Enter 上一个 / Esc 关闭）")
        self.find_input.setFixedWidth(300)
        self.find_input.textChanged.connect(self.on_find_text_changed)
        self.find_input.installEventFilter(self)
        row.addWidget(self.find_input)

        self.find_label = QLabel("")
        self.find_label.setObjectName("FindCount")
        row.addWidget(self.find_label)

        for text, tip, slot in (("↑", "上一个（Shift+Enter）", self.find_prev),
                                ("↓", "下一个（Enter）", self.find_next),
                                ("✕", "关闭（Esc）", self.hide_find_bar)):
            btn = QPushButton(text)
            btn.setObjectName("FindBarButton")
            btn.setToolTip(tip)
            btn.setFixedWidth(26)
            # 按钮不抢焦点，否则点一下就把输入框的焦点带走了
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(slot)
            row.addWidget(btn)

        row.addStretch()
        return bar

    def toggle_find_bar(self):
        """Ctrl+F：唤出/收起查找条

        用 isHidden() 而不是 isVisible()：后者在父窗口还没显示时恒为假，
        会把"已经唤出过"误判成"没唤出"，再按一次就关不掉了。
        """
        if not self.find_bar.isHidden():
            self.hide_find_bar()
            return
        # 选中了内容就直接拿来当查找词（和 IDE 一致）
        selected = self.text_edit.textCursor().selectedText().strip()
        if selected and "\n" not in selected:
            self.find_input.setText(selected)
        self.find_bar.show()
        self.find_input.setFocus()
        self.find_input.selectAll()
        self.on_find_text_changed()

    def hide_find_bar(self):
        self.find_bar.hide()
        self.text_edit.setFocus()

    def on_find_text_changed(self):
        """查找词变化：刷新命中计数，并跳到第一处"""
        text = self.find_input.text()
        if not text:
            self.find_label.setText("")
            return
        total = self.text_edit.toPlainText().count(text)
        self.find_label.setText(f"{total} 处" if total else "无匹配")
        if total:
            self._find(text, backward=False, from_start=True)

    def find_next(self):
        self._find(self.find_input.text(), backward=False)

    def find_prev(self):
        self._find(self.find_input.text(), backward=True)

    def _find(self, text: str, backward: bool = False, from_start: bool = False):
        """在输出里查找；到底了绕回另一端（IDE 那种循环查找）"""
        if not text:
            return
        flags = (QTextDocument.FindFlag.FindBackward if backward
                 else QTextDocument.FindFlag(0))
        if from_start:
            cursor = self.text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.text_edit.setTextCursor(cursor)
        if self.text_edit.find(text, flags):
            return
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End if backward
                            else QTextCursor.MoveOperation.Start)
        self.text_edit.setTextCursor(cursor)
        self.text_edit.find(text, flags)

    def eventFilter(self, obj, event):
        """查找框里：Enter 下一个 / Shift+Enter 上一个 / Esc 关闭"""
        if obj is self.find_input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.find_prev()
                else:
                    self.find_next()
                return True
            if key == Qt.Key.Key_Escape:
                self.hide_find_bar()
                return True
        return super().eventFilter(obj, event)

    def focusOutEvent(self, event):
        """失去焦点就收起建议浮层，免得留在屏幕上误导"""
        if self.hint_popup is not None:
            self.hint_popup.hide()
        super().focusOutEvent(event)

    # ------------------------------------------------------------ 按键
    def handle_key_press(self, event):
        """处理按键

        采用"逐字符实时下发"：每次按键立刻把字符发给设备，由设备维护
        自己的行缓冲与回显。这是真实终端的行为，也是 Tab 补全能工作的
        前提 —— 若只把整行攒在本地、回车才发，设备那边始终是空行，
        Tab 自然补不出任何内容。
        """
        key = event.key()
        text = event.text()
        mods = event.modifiers()

        # 显示区只能由设备输出驱动。下面这些按键会让 QTextEdit 就地改动文字，
        # 在屏上留下设备根本没收到（也没执行）的内容，一律挡掉。
        # 复制 / 全选 / 左右方向键等不改内容的按键仍交给默认处理。
        if key == Qt.Key.Key_Delete:
            return
        if mods & Qt.KeyboardModifier.ControlModifier and key in (
                Qt.Key.Key_V, Qt.Key.Key_X, Qt.Key.Key_Y, Qt.Key.Key_Z):
            return
        if mods & Qt.KeyboardModifier.ShiftModifier and key == Qt.Key.Key_Insert:
            return

        if key == Qt.Key.Key_Backtab:
            return          # Shift+Tab：不发送、也不让它跳走焦点

        if key == Qt.Key.Key_Escape:
            if not self.find_bar.isHidden():
                # 查找栏开着时，Esc 只关查找栏 —— 一次按键不产生两个副作用
                self.hide_find_bar()
                return
            # ⚠ 本文件用 PyQt6（不是 PySide6）；QCursor 不引入 —— 状态提示贴在
            #   终端控件左下角即可，测试环境（无真实 Qt）也不至于因缺符号而崩。
            from PyQt6.QtWidgets import QToolTip
            self.hint_enabled = not getattr(self, "hint_enabled", True)
            tip_pos = self.mapToGlobal(self.rect().bottomLeft())
            if self.hint_enabled:
                QToolTip.showText(tip_pos, "命令提示：已开启")
                self._refresh_hint()
            else:
                self.hint_popup.hide()
                QToolTip.showText(tip_pos, "命令提示：已关闭（Esc 恢复）")
            return

        # ---- 快捷键（与 IDE 习惯一致）----
        if key == Qt.Key.Key_F3:
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self.find_prev()
            else:
                self.find_next()
            return

        if mods & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_F:
                self.toggle_find_bar()
                return
            if key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
                self.zoom_font(1)
                return
            if key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
                self.zoom_font(-1)
                return
            if key == Qt.Key.Key_0:
                self.zoom_font(0)
                return

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # ⚠ 交互规范（2026-10-18 用户定，仿 PyCharm）：
            #   浮层正在提示时，第 1 次回车**只采纳候选**（填进输入行，不发给设备）；
            #   浮层收起后，第 2 次回车才真正发送。这样"看提示"和"执行"各一次回车，
            #   不会出现"想采纳候选却把半截命令发出去了"。
            if self.hint_popup is not None and self.hint_popup.is_active():
                cur = self.hint_popup.current_command()
                if cur:
                    self._accept_hint(cur)
                    return
            if not self.command_buffer:
                # 缓冲空了但设备那一行上还有字（探测命令的残留、或屏幕与缓冲
                # 不同步）：把这行认下来，历史/建议才和真正执行的一致
                self.command_buffer = self._device_line_input()
            if self.command_buffer:
                self.command_history.append(self.command_buffer)
                self.history_index = len(self.command_history)
                # 执行过的命令收进建议表 —— 下次敲开头就能带出来，且排在最前
                self.dictionary.mark_used(self.command_buffer)
                self.command_buffer = ""
            self.hint_popup.hide()
            # 行内容已随每次按键实时下发，回车只需补一个行结束符
            self.raw_input.emit(self.line_ending)
            # 本地回显开着时（设备不回显），回车必须自己把这一行断开：否则屏幕上
            # 那一行永远不换行，用户敲的第二条命令会接在第一条后面 —— 看起来就是
            # "按回车没反应"（用户 2026-10-17 反馈，见 main_window.check_console_echo）
            if self.local_echo:
                self._echo_text(self.line_ending)
            return

        if key == Qt.Key.Key_Backspace:
            if not self.command_buffer:
                # 同上：设备行上还有字就先认下来，否则退格永远删不掉屏上的字
                # （缓冲是空的，删的是空字符串，用户看到的就是"删不动"）
                self.command_buffer = self._device_line_input()
            if not self.command_buffer:
                # 用户这一行本来就没敲东西 ⇒ 设备那边也没有可退的字符。
                # 此时**一个字节都不往下发**：锐捷/华为这些真机会把退格原样回显，
                # 于是设备自己一个字符一个字符把刚打出来的提示符吃掉，屏幕变成
                # "Ruijie(config)#" → "Ruijie(con" → "Ruijie(co"（2026-10-19 反馈：
                # 设备回显的内容不能被删掉）。不发，就不会有这个回显。
                self._refresh_hint()
                return
            self.command_buffer = self.command_buffer[:-1]
            self._protect_current_line()        # 退格只许删到用户输入段的开头
            # 退格同样实时下发，让设备端把它的行缓冲一起退掉
            self.raw_input.emit("\b")
            # ⚠ 本地**必须**抹掉用户自己敲的那个字符，不能等设备回显：
            #   设备回显退格这事各厂商不一致 —— 锐捷会回 "\b \b"，华为 VRP 回显输入
            #   的字符却**不回显退格**（终端该自己擦）。只发不擦，华为上那个字符就
            #   永远留在屏幕上，用户看到的是"退格删不动"（2026-10-19 反馈）。
            #   设备要是也回了退格，边界（_protect_current_line）会把它吃掉，不会多删。
            self._echo_backspace()
            self._refresh_hint()
            return

        if key == Qt.Key.Key_Up:
            if self.hint_popup.is_active():
                self.hint_popup.move_selection(-1)      # 浮层里选上一条
            elif self.command_history and self.history_index > 0:
                self.history_index -= 1
                self._recall(self.command_history[self.history_index])
            return

        if key == Qt.Key.Key_Down:
            if self.hint_popup.is_active():
                self.hint_popup.move_selection(1)       # 浮层里选下一条
            elif self.history_index < len(self.command_history) - 1:
                self.history_index += 1
                self._recall(self.command_history[self.history_index])
            else:
                self.history_index = len(self.command_history)
                self._recall("")
            return

        if key == Qt.Key.Key_C and mods & Qt.KeyboardModifier.ControlModifier:
            if mods & Qt.KeyboardModifier.ShiftModifier:
                # 终端惯例（Windows Terminal / Xshell / SecureCRT 一致）：
                # Ctrl+C 是"打断设备正在跑的命令"，复制用 Ctrl+Shift+C。
                self.copy_selection()
                return
            self.interrupt_requested.emit()
            return

        if key == Qt.Key.Key_L and mods & Qt.KeyboardModifier.ControlModifier:
            self.clear()
            return

        if key == Qt.Key.Key_Tab:
            # 按住 Tab 时系统会以每秒几十次的速度重复发送，而每一次都会真的
            # 下发给设备 —— 设备就一遍遍地吐补全清单，屏幕被反复重画，看起来
            # 就是"卡住"，退格也就删不动了。只认第一次按下的那次。
            if event.isAutoRepeat():
                return
            if self.hint_popup.is_active():
                # 浮层开着时，Tab 用来选中建议（像 IDE 那样）；
                # 否则 Tab 照旧交给设备做补全
                self._accept_hint(self.hint_popup.current_command())
                return
            # 记住提示符：当前行 = 提示符 + 已输入内容
            self._learn_prompt()
            self._tab_at = time.monotonic()
            self.raw_input.emit("\t")
            return

        if text and text.isprintable():
            self.command_buffer += text
            # 逐字符实时下发：设备自己维护行缓冲，Tab 补全才有内容可补
            self.raw_input.emit(text)
            if self.local_echo:
                self._echo_text(text)
            self._refresh_hint()
            return

        # 其余按键交回默认处理（复制/全选等）
        QTextEdit.keyPressEvent(self.text_edit, event)

    # ------------------------------------------------------------ 命令建议
    def _refresh_hint(self):
        """按当前已输入内容刷新建议浮层（无候选就收起来）

        ⚠ Esc 关闭提示后（``hint_enabled=False``）**一律不弹** —— 这是"关掉提示"
        的真实语义；只 hide 不拦住重算，下一次按键又会弹出来，等于没关。
        """
        if not getattr(self, "hint_enabled", True):
            self.hint_popup.hide()
            return
        candidates = self.dictionary.suggest(self.command_buffer)
        if not candidates:
            self.hint_popup.hide()
            return
        anchor = self.text_edit.mapTo(
            self, self.text_edit.cursorRect().bottomLeft())
        self.hint_popup.show_candidates(candidates, anchor)

    def _accept_hint(self, command: str):
        """采纳一条建议：只把差的尾巴补发给设备，缓冲直接置为整条命令"""
        if not command:
            self.hint_popup.hide()
            return
        if command not in self.dictionary.suggest(self.command_buffer):
            # 浮层里的候选是按"当时"的前缀算出来的，之后命令行可能已经被
            # 设备端补全/退格改过（例如先按了 Tab 让设备补全）。这时再照旧
            # 发尾巴，补出来的就是一条错命令 —— 宁可只把候选刷成当前可用的
            self._refresh_hint()
            return
        suffix = command[len(self.command_buffer):]
        if suffix:
            self.raw_input.emit(suffix)
            if self.local_echo:
                self._echo_text(suffix)
        self.command_buffer = command
        self.hint_popup.hide()

    def _echo_text(self, text: str):
        """本地回显：把输入内容写进终端显示区（同时镜像进缓冲区）"""
        self.screen.local_write(text)
        self._append_text(text)
        # 记下"本地已经画过"的字符，等设备回显来对账（见 _drop_duplicate_echo）
        now = time.monotonic()
        for ch in text:
            if ch.isprintable():
                self._echo_pending.append((ch, now))
        if len(self._echo_pending) > 64:
            self._echo_pending = self._echo_pending[-64:]

    def _drop_duplicate_echo(self, text: str) -> str:
        """设备回显了我们刚本地画过的字符 → 撤掉本地回显，并丢掉这一遍重复

        2026-10-19 用户反馈（锐捷 S2928G-E console）："输入命令会重复，例如输入 s
        显示为 ss，自动补全也是如此"。会话日志证明设备只收到**一个** s，多出来那
        个是本地的 —— ``check_console_echo`` 靠"连上后设备有没有吐过字符"猜它回不
        回显，锐捷对唤醒回车不重打提示符，于是被误判成零回显、自动开了本地回显。

        与其猜，不如让设备自己作证：本地画过的字符若紧接着被设备原样回显回来，
        那本地这一遍就是多余的 —— 立刻关掉本地回显，并把重复的这份丢掉（用户眼
        里只有一个字）。只认"整段就是回显（可带换行）"的数据形状：设备异步吐的
        日志（如 ``i am a log line``）不以回显形状出现，真不回显的设备照样能看见
        自己敲的字。
        """
        if not (self.local_echo and self._echo_pending and text):
            return text
        now = time.monotonic()
        pending = [(c, ts) for c, ts in self._echo_pending
                   if now - ts <= self.ECHO_CONFIRM_WINDOW]
        self._echo_pending = pending
        if not pending:
            return text
        n = 0
        while n < len(pending) and n < len(text) and text[n] == pending[n][0]:
            n += 1
        if n == 0:
            return text
        rest = text[n:]
        if rest.strip("\r\n"):
            return text            # 后面还跟着别的内容 ⇒ 是设备自己的输出，不是回显
        self.set_local_echo(False)  # 设备会回显 ⇒ 本地这一遍从此刻起不要了
        self.echo_auto_off.emit()
        return rest

    def _recall(self, value: str):
        """取历史命令：先退格清掉设备当前行，再下发新内容"""
        old = self.command_buffer
        if old:
            self.raw_input.emit("\b" * len(old))
            # 同退格键：本地一律擦掉（设备不一定回显退格），边界由缓冲区守住
            for _ in range(len(old)):
                self._echo_backspace()
        if value:
            self.raw_input.emit(value)
            if self.local_echo:
                self._echo_text(value)
        self.command_buffer = value
        self._refresh_hint()

    def _display_last_line(self) -> str:
        """显示区最后一行（用来核对"还能不能再删"）"""
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine,
                            QTextCursor.MoveMode.KeepAnchor)
        return cursor.selectedText()

    def _echo_backspace(self) -> bool:
        """抹掉用户自己敲进去的最后一个字符（显示区 + 缓冲区一起）

        为什么**不**跟着 ``local_echo`` 走：那个开关只决定"输入的字符要不要本地再
        画一遍"，和"擦除要不要本地做"是两件事 —— 设备回显输入却不回显退格时
        （华为 VRP），退格必须本地擦，否则屏上那个字符删不掉（2026-10-19 反馈）。
        边界：只删得动用户输入那一段，设备回显的提示符一个字符都不动；显示区也按
        同一个边界核一遍，两边永远一致。返回是否真的删掉了。
        """
        if not self.screen.local_backspace():
            return False
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.text_edit.setTextCursor(cursor)
        if len(self._display_last_line()) > self.screen.protected_prefix_len:
            cursor.deletePreviousChar()
        return True

    # ------------------------------------------------------------ 输出
    def append_output(self, text: str):
        """添加输出（自动处理ANSI与回车覆盖）"""
        if not text:
            return
        # 设备回显裁决要在最前面：这段要是"本地回显的重复份"，直接丢掉，不喂屏幕
        text = self._drop_duplicate_echo(text)
        if not text:
            return
        # 先按"设备当前行 = 提示符 + 已输入内容"更新对提示符的认知：
        # 必须在本批数据合并进屏幕**之前**算，否则设备重画过的行会算出错误前缀
        self._learn_prompt()
        # 设备这一批数据里可能带 \b（它把用户的退格原样回显回来）：先划好
        # "哪一段是设备回显内容"，退格才不至于吃掉提示符
        self._protect_current_line()
        clean_text = self.screen.feed(text, collapse_newline=self._is_tab_redraw(text))
        # 喂完再认一次提示符：上面那次看的是"这批数据到达前"的行，认不出**刚**
        # 打出来的提示符（设备打完提示符就等人输入，往往没有下一批数据来触发
        # 再认）。认不出提示符 = 算不出"哪一段是设备回显内容"，退格就会连用户
        # 自己的输入一起保护住，表现成"退格删不动"。
        self._learn_prompt()
        self._protect_current_line()
        if self.screen.current_line_stale:
            # 设备重画/改写了当前行：屏幕要整行覆盖，输入缓冲也趁机对一下
            self._sync_line_from_screen()
            self._replace_current_line(clean_text)
            return
        # 设备把整行当普通内容追加过来（不是 \r 重写）时不会置 stale，
        # 但可能比本地缓冲多了补全内容，也要对一下
        self._sync_line_from_screen()
        if not clean_text:
            return
        self._append_text(clean_text)

    def _is_tab_redraw(self, text: str) -> bool:
        """判断本批数据是不是设备对 Tab 的响应（据此决定要不要"就地改写当前行"）

        两个条件都要满足：处在按 Tab 后的窗口期内，且这批数据以换行开头
        （设备的整行重画就是"\\r\\n + 提示符 + 整行"）。

        必须要求"以换行开头"：就地把改写会先清空当前行，窗口期内随便一批
        普通输出（比如设备持续吐的数据）被当成重画，就会把那一行擦掉。
        """
        if not (self._tab_at and time.monotonic() - self._tab_at < self.TAB_WINDOW):
            return False
        if not text.startswith(("\r\n", "\n", "\r")):
            return False
        self._tab_at = 0.0   # 重画已到，窗口关闭
        return True

    def _learn_prompt(self):
        """从"设备当前行 = 提示符 + 已输入内容"反推当前提示符

        设备可能还没回显我们刚敲的字符，所以两种情况都要认：
        · 行尾已带上输入内容 → 前缀就是提示符（提示符变化时也能自愈）
        · 行本身就是个纯提示符 → 直接记下来
          ⚠ 这条**不能**加"以前没学过才记"的条件：进了配置模式提示符会变
          （"Ruijie#" → "Ruijie(config)#"），拿旧提示符去算"哪一段是设备回显
          内容"就会算错边界，退格又能删到设备内容了（2026-10-19）。
        """
        line = self.screen.last_line()
        typed = self.command_buffer
        if typed and len(line) > len(typed) and line.endswith(typed):
            self._prompt = line[:-len(typed)]
        elif not typed and looks_like_prompt(line):
            self._prompt = line

    def _device_line_input(self) -> str:
        """屏幕上当前行里、提示符之后的内容

        这一行由设备回显驱动（本地回显关闭时），所以它代表设备那边**真实持有**的
        输入。提示符没认出来、或当前行不是提示符行时返回空串。
        """
        if not self._prompt:
            return ""
        line = self.screen.last_line()
        if not line.startswith(self._prompt):
            return ""
        return line[len(self._prompt):]

    def _protect_current_line(self):
        """登记当前行的"可退格起点"：这一行不许退到比 `提示符 + 用户输入` 更短

        前面那一段（提示符、设备补全出来的内容）是**设备回显的内容**，退格删到
        它就是屏幕在吃设备已经打出来的东西（2026-10-19 用户反馈：提示符被一个
        字符一个字符吃掉）。

        ⚠ 边界要用 `len(提示符) + len(本地输入缓冲)` 算，**不能**用"整行长度 −
        输入长度"：退格时缓冲是先减、行是后删，用减法会把正要删掉的那个字符也算
        进保护范围，屏幕上就成了"退格删不动"。
        提示符还没认出来（或这一行对不上）时按"整行都是设备内容"处理 —— 宁可
        退不动，也不删设备回显。
        """
        line = self.screen.last_line()
        prompt = self._prompt or ""
        if prompt and line.startswith(prompt):
            floor = len(prompt) + len(self.command_buffer)
        else:
            floor = len(line)
        self.screen.set_protected_prefix_len(floor)

    def input_pending(self) -> bool:
        """命令行上还有内容（用户正在输入，或设备行上还留着半截命令）

        自动发起的操作（连上后的唤醒回车、探测用的 show version）之前都要先问一句，
        否则会把内容插进用户正在敲的那一行里 —— 用户看到的是"回车不执行、
        退格删不掉、提示还不对"，根子其实是命令行被掺了探测内容。

        只看非空白字符：有些设备的提示符后面自带一个空格（如 "Ruijie# "），
        那是提示符的一部分，不是用户输入，不能因此一直判定"正在输入"。
        """
        return bool(self.command_buffer.strip()) or bool(self._device_line_input().strip())

    def _sync_line_from_screen(self):
        """让本地输入缓冲跟上屏幕上那一行（Tab 补全后缓冲区要和设备一致）

        只在"设备那一行是在我们已输入内容的基础上继续往前走"（补全、回显更完整）
        时才采纳。反过来，设备那一行比我们短（例如按 Tab 没补出东西、设备只是
        重打了一次提示符 "Ruijie>"）时**绝不能**采纳 —— 那会把用户刚敲的内容
        擦掉（屏幕上明明有字，缓冲却空了，按回车就什么也没执行）。
        """
        if not self.command_buffer:
            return
        pending = self._device_line_input()
        if not pending or not pending.startswith(self.command_buffer):
            return
        if pending == self.command_buffer:
            return
        self.command_buffer = pending
        # 命令行被设备改长了（Tab 补全等），浮层里的候选也要跟着换 ——
        # 否则它还是按旧前缀算的那批，采纳下去就补出错命令
        if self.hint_popup.is_active():
            self._refresh_hint()

    def _append_text(self, text: str):
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.text_edit.setTextCursor(cursor)
        self.text_edit.insertPlainText(text)
        self.text_edit.ensureCursorVisible()

    def _replace_current_line(self, clean_text: str):
        """用缓冲区的当前行整行覆盖屏幕上已渲染的该行"""
        # clean_text 里最后一个换行之前的内容属于已结束的行，先正常追加
        idx = clean_text.rfind('\n')
        if idx >= 0:
            self._append_text(clean_text[:idx + 1])
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine,
                            QTextCursor.MoveMode.KeepAnchor)
        self.text_edit.setTextCursor(cursor)
        self.text_edit.insertPlainText(self.screen.last_line())
        self.text_edit.ensureCursorVisible()

    def send_paste(self, text: str):
        """粘贴多行文本，逐行下发"""
        for line in text.splitlines():
            line = line.strip()
            if line:
                self.command_entered.emit(line)

    def copy_selection(self) -> bool:
        """复制当前选中的设备输出（Ctrl+Shift+C，或右键菜单里的"复制"）

        为什么不用 Ctrl+C：终端里 Ctrl+C 是"打断设备正在跑的命令"，
        这是所有人（Xshell / SecureCRT / Windows Terminal）的共同肌肉记忆，
        复制改用 Ctrl+Shift+C。
        """
        text = self.text_edit.textCursor().selectedText()
        if not text:
            return False
        # QTextCursor 用 U+2029 表示段落分隔，原样粘到记事本/工单里会变怪字符
        QGuiApplication.clipboard().setText(text.replace("\u2029", "\n"))
        return True

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
