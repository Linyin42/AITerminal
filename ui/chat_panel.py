"""
对话式 AI 面板 - 对标 Netcatty 的 Catty Agent

交互方式:
    用户在输入框里用自然语言说需求 → AI 回复 → 回复里的命令渲染成
    "命令计划卡片"（可点执行 / 复制 / 跳过）→ 执行前仍走配置命令确认闸门。

与旧版"按钮 + 日志"的区别:
    · 有真正的对话上下文（气泡形式）
    · 命令以卡片呈现，带操作按钮，而不是埋在日志里
    · 顶部固定显示当前设备，AI 的回答始终针对这台设备
"""
import html as html_mod

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextBrowser, QPlainTextEdit,
    QPushButton, QLabel, QToolButton
)
from PyQt6.QtCore import Qt, pyqtSignal, QUrl

MAX_MESSAGES = 200


class ChatInput(QPlainTextEdit):
    """输入框：Enter 发送，Shift+Enter 换行"""

    submitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText(
            "向 Catty 发送消息 — 例如：检查所有接口状态，找出 down 的端口"
        )
        self.setFixedHeight(76)
        # 边框与圆角由外层「输入容器」统一负责，这里保持透明无边框
        self.setStyleSheet(
            "QPlainTextEdit { background: transparent; color: #E6EAF2;"
            " border: none; padding: 2px 2px; }"
        )

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)      # Shift+Enter 换行
            else:
                self.submitted.emit()
            return
        super().keyPressEvent(event)


class ChatView(QWidget):
    """
    对话式 AI 视图

    信号:
        message_sent(str):     用户发出一条消息
        execute_requested(list): 请求执行某组命令（命令计划卡片上的"执行"）
        explain_requested():   点"解释输出"
        research_requested(str): 点"查资料"或直接在对话里问知识类问题
        clear_requested():     清空对话
    """

    message_sent = pyqtSignal(str)
    execute_requested = pyqtSignal(list)
    explain_requested = pyqtSignal()
    research_requested = pyqtSignal(str)
    clear_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.messages = []          # [{role, text, commands, plan_id, notes}]
        self._plan_seq = 0
        self._device_text = "未连接设备"
        self._model_text = ""
        self.init_ui()
        # 渲染初始欢迎语：不调用的话聊天区是一片空白，看起来像功能坏了
        self._render()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # 顶部：当前设备
        self.device_bar = QLabel("")
        self.device_bar.setWordWrap(True)
        self.device_bar.setStyleSheet(
            "background-color: #151A23; color: #4C8DFF; padding: 6px 10px;"
            " border: 1px solid #242931; border-radius: 8px; font-size: 12px;"
        )
        layout.addWidget(self.device_bar)

        # 对话区
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self.on_anchor_clicked)
        self.view.setStyleSheet(
            "QTextBrowser { background-color: #0E1117; border: 1px solid #242931;"
            " border-radius: 8px; padding: 10px; }"
        )
        layout.addWidget(self.view, 1)

        # 输入区：整块圆角容器（参考 OpenCode / Netcatty 的输入框样式），
        # 上方是输入框，下方一行左侧提示、右侧圆形发送按钮
        composer = QWidget()
        composer.setObjectName("ChatComposer")
        composer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        composer.setStyleSheet(
            "#ChatComposer { background-color: #151A23; border: 1px solid #242931;"
            " border-radius: 14px; }")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(10, 8, 10, 8)
        composer_layout.setSpacing(6)

        self.input = ChatInput()
        self.input.submitted.connect(self.on_send)
        composer_layout.addWidget(self.input)

        compose_row = QHBoxLayout()
        compose_row.setSpacing(8)
        hint = QLabel("Enter 发送 · Shift+Enter 换行")
        hint.setStyleSheet("color: #6B7684; font-size: 11px;")
        compose_row.addWidget(hint)
        compose_row.addStretch()

        self.btn_send = QPushButton()
        self.btn_send.setFixedSize(32, 32)
        self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_send.setToolTip("发送（Enter）")
        # 自绘箭头图标：字符 "↑" 依赖字体字形，不同机器上会渲染成方块/怪符号
        self.btn_send.setIcon(self._send_icon())
        from PyQt6.QtCore import QSize
        self.btn_send.setIconSize(QSize(18, 18))
        self.btn_send.setStyleSheet(
            "QPushButton { background-color: #4C8DFF; border: none;"
            " border-radius: 16px; }"
            "QPushButton:hover { background-color: #4C8DFF; }"
            "QPushButton:pressed { background-color: #3F7DE8; }"
            "QPushButton:disabled { background-color: #151A23; }"
        )
        self.btn_send.clicked.connect(self.on_send)
        compose_row.addWidget(self.btn_send)
        composer_layout.addLayout(compose_row)
        layout.addWidget(composer)

        # 辅助操作
        row = QHBoxLayout()
        row.setSpacing(4)

        for text, tip, handler in (
            ("解释输出", "让 AI 用中文解释当前终端里的内容", self.explain_requested.emit),
            ("查资料", "把输入框内容当作问题去查资料", self.on_research),
            ("清空", "清空对话", self.clear_requested.emit),
        ):
            button = QToolButton()
            button.setText(text)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            button.setStyleSheet(
                "QToolButton { color: #93A0B4; background-color: transparent;"
                " border: none; padding: 3px 8px; font-size: 11px; }"
                "QToolButton:hover { color: #4C8DFF; }"
            )
            row.addWidget(button)
        row.addStretch()

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #6B7684; font-size: 11px;")
        row.addWidget(self.status_label)
        layout.addLayout(row)

    # ------------------------------------------------------------ 设备信息
    @staticmethod
    def _send_icon():
        """自绘「向上箭头」图标：不依赖字体字形，任何机器渲染一致"""
        from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QIcon
        from PyQt6.QtCore import QPointF
        size = 20
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#0E1117"))
        pen.setWidthF(2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        cx = size / 2.0
        painter.drawLine(QPointF(cx, size - 4), QPointF(cx, 4.5))      # 竖线
        painter.drawLine(QPointF(cx, 4.5), QPointF(cx - 4.5, 9))       # 左斜
        painter.drawLine(QPointF(cx, 4.5), QPointF(cx + 4.5, 9))       # 右斜
        painter.end()
        return QIcon(pixmap)

    def set_device(self, name: str, protocol: str = "", host: str = "",
                   connected: bool = False, vendor: str = "unknown"):
        """更新顶部设备信息条"""
        if not name:
            self._device_text = "未连接设备"
        else:
            parts = [f"<b>{html_mod.escape(name)}</b>"]
            if protocol or host:
                parts.append(f"{html_mod.escape(protocol)} {html_mod.escape(host)}")
            if vendor and vendor != "unknown":
                parts.append(f"厂商 {vendor.upper()}")
            if connected:
                parts.append("<span style='color:#3FB950;'>● 已连接</span>")
            self._device_text = "　".join(parts)
        self.device_bar.setText(f"当前设备：{self._device_text}")

    def set_model(self, text: str):
        self._model_text = text or ""
        self._render()

    def set_busy(self, busy: bool, hint: str = "AI 正在思考…"):
        # 发送键是 32x32 的纯图标按钮，不能再 setText：
        # 图标与文字会挤在同一个圆里，渲染出残缺字形。
        self.btn_send.setEnabled(not busy)
        self.btn_send.setToolTip("思考中…" if busy else "发送（Enter）")
        self.status_label.setText(hint if busy else "")

    # ------------------------------------------------------------ 消息
    def add_user(self, text: str):
        self._append({"role": "user", "text": text})

    def add_ai(self, text: str, commands=None, notes: str = ""):
        message = {"role": "ai", "text": text,
                   "commands": list(commands or []), "notes": notes}
        # 有命令时才分配 plan_id，供命令计划卡片上的执行/复制/跳过按钮定位
        if message["commands"]:
            self._plan_seq += 1
            message["plan_id"] = self._plan_seq
        self._append(message)

    def add_system(self, text: str):
        self._append({"role": "system", "text": text})

    def add_error(self, text: str):
        self._append({"role": "error", "text": text})

    def _append(self, message: dict):
        self.messages.append(message)
        if len(self.messages) > MAX_MESSAGES:
            self.messages = self.messages[-MAX_MESSAGES:]
        self._render()

    def clear(self):
        self.messages = []
        self._render()

    # ------------------------------------------------------------ 交互
    def on_send(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self.message_sent.emit(text)

    def on_research(self):
        text = self.input.toPlainText().strip()
        self.research_requested.emit(text)

    def on_anchor_clicked(self, url: QUrl):
        """处理命令计划卡片上的按钮"""
        target = url.toString()
        if not target.startswith("#"):
            return
        action, _, raw_id = target[1:].partition(":")
        try:
            plan_id = int(raw_id)
        except ValueError:
            return

        message = next((m for m in self.messages
                        if m.get("plan_id") == plan_id), None)
        if message is None:
            return

        commands = list(message.get("commands") or [])
        if action == "exec":
            self.add_user(f"执行这 {len(commands)} 条命令")
            self.execute_requested.emit(commands)
        elif action == "skip":
            message["skipped"] = True
            self.add_system(f"已跳过 {len(commands)} 条命令")
        elif action == "copy":
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText("\n".join(commands))
            self.add_system("命令已复制到剪贴板")

    # ------------------------------------------------------------ 渲染
    def _render(self):
        html = [self._style_block()]
        if not self.messages:
            html.append(self._empty_state_html())
        else:
            for message in self.messages:
                role = message.get("role")
                if role == "user":
                    html.append(self._user_bubble(message["text"]))
                elif role == "ai":
                    html.append(self._ai_bubble(message))
                elif role == "system":
                    html.append(self._system_line(message["text"], "#93A0B4"))
                elif role == "error":
                    html.append(self._system_line("⚠ " + message["text"], "#F85149"))
        self.view.setHtml("".join(html))
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    @staticmethod
    def _style_block() -> str:
        return ("<style>"
                "body { color: #E6EAF2; font-family: 'Microsoft YaHei', sans-serif;"
                " font-size: 13px; }"
                "a { color: #4C8DFF; text-decoration: none; }"
                "a:hover { text-decoration: underline; }"
                "code { background-color: #151A23; color: #d19a66; padding: 1px 4px;"
                " border-radius: 4px; }"
                "</style>")

    @staticmethod
    def _user_bubble(text: str) -> str:
        safe = html_mod.escape(text).replace('\n', '<br/>')
        return (
            '<table width="100%" cellspacing="0" cellpadding="0"><tr>'
            '<td align="right">'
            '<table cellspacing="0" cellpadding="8" bgcolor="#242931" width="86%" '
            'style="border:1px solid #4C8DFF;border-radius: 8px;">'
            f'<tr><td><span style="color:#E6EAF2;font-size:13px;">{safe}</span></td></tr>'
            '</table></td></tr></table><div style="height:6px;"></div>'
        )

    def _ai_bubble(self, message: dict) -> str:
        text = self._render_text(message.get("text", ""))
        commands = message.get("commands") or []
        plan = self._plan_card(commands, message.get("plan_id"),
                               skipped=message.get("skipped", False)) if commands else ""
        notes = message.get("notes", "")
        notes_html = (f'<div style="color:#D29922;font-size:11px;margin-top:4px;">'
                      f'{html_mod.escape(notes)}</div>') if notes else ""
        model = (f'<span style="color:#6B7684;font-size:10px;">{html_mod.escape(self._model_text)}</span>'
                 if self._model_text else "")
        return (
            '<table width="100%" cellspacing="0" cellpadding="0"><tr><td align="left">'
            '<table cellspacing="0" cellpadding="10" bgcolor="#151A23" '
            'style="border:1px solid #242931;border-radius: 8px;" width="96%">'
            f'<tr><td><div style="color:#4C8DFF;font-size:11px;font-weight:bold;">AI 助手　{model}</div>'
            f'<div style="color:#E6EAF2;">{text}</div>{notes_html}{plan}</td></tr>'
            '</table></td></tr></table><div style="height:6px;"></div>'
        )

    def _plan_card(self, commands, plan_id, skipped: bool = False) -> str:
        rows = []
        for index, command in enumerate(commands, start=1):
            safe = html_mod.escape(str(command))
            rows.append(
                '<tr>'
                f'<td width="24" align="right" style="color:#6B7684;">{index}.</td>'
                f'<td><span style="color:#d19a66;font-family:Consolas,monospace;">{safe}</span></td>'
                '</tr>'
            )
        if skipped:
            actions = '<span style="color:#6B7684;">（已跳过）</span>'
        else:
            actions = (
                f'<a href="#exec:{plan_id}">▶ 执行</a>　'
                f'<a href="#copy:{plan_id}">⧉ 复制</a>　'
                f'<a href="#skip:{plan_id}">✕ 跳过</a>'
            )
        return (
            '<table width="100%" cellspacing="0" cellpadding="8" bgcolor="#0E1117" '
            'style="border:1px solid #242931;border-radius: 6px;margin-top:8px;">'
            '<tr><td>'
            f'<div style="color:#D29922;font-size:11px;font-weight:bold;">命令计划（{len(commands)} 条）'
            '　<span style="color:#6B7684;font-weight:normal;">配置类命令会先弹确认框</span></div>'
            f'<table cellspacing="0" cellpadding="3">{"".join(rows)}</table>'
            f'<div style="margin-top:6px;">{actions}</div>'
            '</td></tr></table>'
        )

    @staticmethod
    def _empty_state_html() -> str:
        return (
            '<div style="height:190px;"></div>'
            '<div style="text-align:center;color:#93A0B4;font-size:13px;">'
            '询问服务器相关问题、执行命令或获取配置帮助。'
            '</div>'
        )

    @staticmethod
    def _system_line(text: str, color: str) -> str:
        return (f'<div style="color:{color};font-size:11px;margin:4px 0;">'
                f'{html_mod.escape(text)}</div>')

    @staticmethod
    def _render_text(text: str) -> str:
        """轻量渲染：代码块、行内代码、粗体、换行"""
        text = text or ""
        blocks = []
        parts = text.split("```")
        for index, part in enumerate(parts):
            if index % 2 == 1:
                body = part.strip('\n')
                if body.startswith(('bash', 'text', 'plain')):
                    body = body.split('\n', 1)[-1]
                blocks.append(
                    '<table width="100%" cellspacing="0" cellpadding="6" '
                    'bgcolor="#0E1117"><tr><td>'
                    '<span style="color:#d19a66;font-family:Consolas,monospace;'
                    'font-size:12px;">'
                    + html_mod.escape(body).replace('\n', '<br/>')
                    + '</span></td></tr></table>')
            else:
                safe = html_mod.escape(part).replace('\n', '<br/>')
                safe = safe.replace('&amp;nbsp;', '&nbsp;')
                blocks.append(safe)
        rendered = "".join(blocks)
        # 行内代码 `xxx`
        import re
        rendered = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', rendered)
        rendered = re.sub(r'\*\*([^*\n]+)\*\*', r'<b>\1</b>', rendered)
        return rendered
