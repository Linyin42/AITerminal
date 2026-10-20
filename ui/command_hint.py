"""
命令提示浮层 —— 像 IDE 那样边敲边列出后续可用的命令

数据来源（越靠前越优先）:
    1. 本次会话里已经执行过的命令（回车时自动收录，用得越多越靠前）
    2. 资料库「命令速查」里的命令（导入手册整理出的、按型号生成的）
    3. 设备厂商的常用命令表（config/presets.py）

浮层是终端控件的**子控件**而非独立窗口，并且不接收焦点 ——
否则会抢走键盘输入，终端就打不了字了。
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QListWidget, QListWidgetItem

from config.presets import get_vendor_commands

MAX_ITEMS = 8       # 浮层最多列几条
MIN_WIDTH = 180
MAX_WIDTH = 420

#: 合法的"单词命令"。从手册/文档里抽命令时，单靠"至少两个词"会把它们误杀，
#: 所以显式列一份。没有列进来的单个词一律不收 —— 文档里那些参数碎片
#: （state、description、alias、text…）就是这么被挡掉的。
SINGLE_WORD_COMMANDS = {
    "show", "display", "enable", "disable", "reload", "reboot", "write", "save",
    "copy", "quit", "exit", "logout", "ping", "traceroute", "tracert", "clear",
    "reset", "configure", "terminal", "debug", "undebug", "undo", "no", "dir",
    "more", "rename", "delete", "mkdir", "rmdir", "pwd", "clock", "history",
    "help", "alias", "system-view", "commit", "rollback", "shutdown", "switchport",
}


def looks_like_command(command: str) -> bool:
    """是不是一条"能直接敲"的命令（挡掉文档里抽出来的参数碎片）

    文档正文里的行并不都是命令：PDF 抽出来的参数表会散落成一堆单个词
    （state / description / alias / text…），它们进了提示表就变成
    "提示的命令是错的"。判据只有两条：
        · 两个词以上 → 基本可以当命令（show version / no shutdown）
        · 单个词 → 必须在已知的单词命令表里
    """
    tokens = (command or "").split()
    if not tokens:
        return False
    if len(tokens) >= 2:
        return True
    return tokens[0].lower() in SINGLE_WORD_COMMANDS


class CommandDictionary:
    """候选命令表：按已输入前缀给出建议

    候选来源（越靠前越优先）:
        1. 本次会话执行过的命令（回车时收录，保证排最前）
        2. 资料库里整理好的命令（导入手册整理出的清单、按型号生成的清单）
        3. 设备厂商的常用命令表（config/presets.py）

    命令表完全由程序自己维护 —— 不向设备敲 ? 去问，避免把探测内容
    插进用户正在敲的那一行里。
    """

    def __init__(self, vendor: str = ""):
        self._commands = []          # 保持顺序，用于稳定排序
        self._seen = set()
        self._used = set()           # 本次会话真正执行过的
        self.vendor = ""
        self.set_vendor(vendor)

    def set_vendor(self, vendor: str):
        """按设备厂商装一批常用命令（识别不出厂商时用 h3c 的）"""
        self.vendor = (vendor or "").lower()
        for command in get_vendor_commands(self.vendor).values():
            self.add(command)

    def add(self, command: str, validate: bool = True):
        """收录一条命令（去重；空白折叠成单空格）

        validate 打开时会挡掉"不像命令"的条目（文档里抽出来的参数碎片），
        用户亲手敲过的命令则不走这个判断 —— 那是设备认过的。
        """
        command = " ".join((command or "").split())
        if not command or command in self._seen:
            return
        if validate and not looks_like_command(command):
            return
        self._seen.add(command)
        self._commands.append(command)

    def mark_used(self, command: str):
        """标记某条命令被执行过 —— 之后它排在最前面"""
        command = " ".join((command or "").split())
        if command:
            self.add(command, validate=False)
            self._used.add(command)

    def suggest(self, prefix: str, limit: int = MAX_ITEMS):
        """给出后续命令建议（命令行要以已输入内容开头）"""
        prefix = (prefix or "").strip()
        if not prefix:
            return []
        low = prefix.lower()
        hits = []
        for command in self._commands:
            if command == prefix or not command.lower().startswith(low):
                continue
            hits.append(command)
        # 执行过的优先，其次短的优先（更可能是用户想要的完整命令）
        hits.sort(key=lambda c: (c not in self._used, len(c), c.lower()))
        return hits[:limit]

    def all_commands(self):
        return list(self._commands)


class CommandHintPopup(QListWidget):
    """命令建议浮层"""

    accepted = pyqtSignal(str)

    def __init__(self, parent):
        super().__init__(parent)
        # 不能抢焦点：否则键盘输入会落到浮层上，终端就打不了字了
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setStyleSheet(
            "QListWidget { background-color: #151A23; color: #E6EAF2;"
            " border: 1px solid #242931; border-radius: 8px; padding: 3px;"
            " outline: none; }"
            "QListWidget::item { padding: 4px 8px; border-radius: 4px; }"
            "QListWidget::item:selected { background-color: #242931;"
            " color: #E6EAF2; }"
        )
        self.itemClicked.connect(self._on_item_clicked)
        self.hide()

    # ------------------------------------------------------------ 状态
    def is_active(self) -> bool:
        """浮层当前是否在提示（有候选且没被隐藏）"""
        return not self.isHidden() and self.count() > 0

    def current_command(self) -> str:
        item = self.currentItem()
        return item.text() if item else ""

    def move_selection(self, delta: int):
        """上/下移动选中项"""
        if not self.count():
            return
        row = (self.currentRow() + delta) % self.count()
        self.setCurrentRow(row)

    # ------------------------------------------------------------ 展示
    def show_candidates(self, candidates, anchor):
        """填充候选项并贴到光标处（anchor 为父控件坐标系下的锚点）"""
        self.clear()
        for text in candidates:
            self.addItem(QListWidgetItem(text))
        self.setCurrentRow(0)

        row_height = self.sizeHintForRow(0) or 22
        width = max(MIN_WIDTH, min(
            MAX_WIDTH, max(self.fontMetrics().horizontalAdvance(c)
                           for c in candidates) + 28))
        height = row_height * len(candidates) + 8

        parent = self.parentWidget()
        x, y = anchor.x(), anchor.y() + 2
        if parent is not None:
            if y + height > parent.height():
                y = max(0, anchor.y() - height - 18)   # 下面放不下就翻到光标上方
            x = max(0, min(x, parent.width() - width))
        self.resize(width, height)
        self.move(x, y)
        self.show()
        self.raise_()

    def _on_item_clicked(self, item):
        self.accepted.emit(item.text())
