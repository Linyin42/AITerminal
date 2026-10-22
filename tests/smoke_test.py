"""
NetOpsTerminal 冒烟测试 - 不依赖真实设备，验证核心逻辑与界面可正常构建

运行:
    python tests/smoke_test.py
"""
import os
import sys
import time

# 无显示环境下运行 Qt
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from PyQt6.QtWidgets import QApplication  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402

from core.connector import BaseConnector  # noqa: E402
from core.terminal import (  # noqa: E402
    TerminalBuffer, detect_vendor, clean_prompt_tail, looks_like_prompt)
from core.ai_agent import AIAgent  # noqa: E402
from core.session import SessionManager, SessionInfo  # noqa: E402
from core import telnet_connector as telnet_mod  # noqa: E402
from core.diff import diff_lines, summarize, to_unified, has_changes  # noqa: E402
from core.logger import SessionLogger, list_logs  # noqa: E402
from core.batch import BatchRunner, CommandWaiter, disable_paging  # noqa: E402
from core import safety as safety_mod  # noqa: E402
from core.xlsx_writer import write_xlsx  # noqa: E402
from core import manual as manual_mod  # noqa: E402
from core import patrol as patrol_mod  # noqa: E402
from core.patrol import PatrolRunner, export_patrol_excel, export_patrol_csv  # noqa: E402
from core import web_search as ws  # noqa: E402
from core.web_search import WebSearchClient  # noqa: E402
from core.serial_connector import SerialConnector  # noqa: E402
from config.settings import Settings  # noqa: E402
from config.presets import (  # noqa: E402
    PRESET_CONNECTIONS, get_vendor_commands, CONSOLE_PRESETS,
    BAUDRATES, PARITY_OPTIONS, FLOW_OPTIONS, LINE_ENDING_OPTIONS
)
from config.templates import (  # noqa: E402
    COMMAND_TEMPLATES, extract_variables, render_template,
    template_commands, save_config_commands
)
from plugins.base import PluginManager  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.diff_dialog import DiffDialog  # noqa: E402
from ui.template_dialog import TemplateDialog  # noqa: E402
from ui.prompt_dialog import PromptDialog  # noqa: E402

RESULTS = []
_APP = None  # 保持 QApplication 引用，避免被回收


def _settings_snapshot():
    """备份配置，避免测试写脏用户真实配置"""
    settings = Settings()
    return settings, dict(settings.data)


def _settings_restore(settings, snapshot):
    settings._data = snapshot
    settings.save()


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    flag = "PASS" if condition else "FAIL"
    print(f"[{flag}] {name}" + (f"  -> {detail}" if detail and not condition else ""))


class DummyConnector(BaseConnector):
    """用于测试的假连接器（会模拟设备回显与提示符返回）"""

    def __init__(self, simulate_prompt: bool = True):
        super().__init__()
        self._buffer = ""
        self._connected = True
        self.sent = []
        self.simulate_prompt = simulate_prompt

    def connect(self, **kwargs) -> bool:
        return True

    def send(self, command: str):
        self.sent.append(command)
        if self.simulate_prompt:
            # 模拟设备：回显命令 + 输出 + 提示符，供"等提示符"逻辑判定
            self._notify_output(f"{command}\r\nOK\r\nswitch#")

    def send_raw(self, data: str):
        self.sent.append(data)
        if self.simulate_prompt:
            self._notify_output("switch#")

    def disconnect(self):
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_output(self) -> str:
        out, self._buffer = self._buffer, ""
        return out

    def get_output_block(self, timeout: float = 2.0) -> str:
        return self.get_output()


def test_terminal_buffer():
    buf = TerminalBuffer()
    buf.feed("hello\r\nworld")
    check("终端缓冲: 换行", buf.text() == "hello\nworld", repr(buf.text()))

    buf2 = TerminalBuffer()
    out = buf2.feed("progress 10%\rprogress 99%")
    check("终端缓冲: \\r 行内覆盖", out == "progress 99%" and buf2.text() == "progress 99%",
          repr((out, buf2.text())))

    buf3 = TerminalBuffer()
    buf3.feed("\x1b[31mERROR\x1b[0m ok")
    check("终端缓冲: 剥离ANSI", buf3.text() == "ERROR ok", repr(buf3.text()))

    buf4 = TerminalBuffer()
    buf4.feed("ab\b\bcd")
    check("终端缓冲: 退格", buf4.text() == "cd", repr(buf4.text()))

    check("厂商识别: H3C", detect_vendor("<H3C>display version\nComware Software") == "h3c")
    check("厂商识别: 华为", detect_vendor("Huawei Versatile Routing Platform VRP") == "huawei")
    check("厂商识别: Cisco", detect_vendor("Cisco IOS Software, C2960") == "cisco")

    tail = clean_prompt_tail("display version\nVRP V800\n<H3C>")
    check("去除尾部提示符", tail == "display version\nVRP V800", repr(tail))

    check("提示符识别: Ruijie>", looks_like_prompt("Ruijie>") is True)
    check("提示符识别: <H3C>", looks_like_prompt("<H3C>") is True)
    check("提示符识别: 输出行不算", looks_like_prompt("GigabitEthernet0/1 is up") is False)
    check("提示符识别: 空行不算", looks_like_prompt("") is False)


def test_terminal_input_tracking():
    """输入缓冲要跟着终端当前行同步（Tab 补全、设备重画），且不被设备擦掉"""
    from PyQt6.QtGui import QKeyEvent
    from ui.terminal_widget import TerminalWidget

    def press(widget, key, text="", mods=Qt.KeyboardModifier.NoModifier):
        event = QKeyEvent(QKeyEvent.Type.KeyPress, key, mods, text)
        widget.handle_key_press(event)

    widget = TerminalWidget(1)
    # 光标要像 Linux 终端那样看得见：只读的 QTextEdit 不画插入光标，所以不能只读
    check("显示区: 非只读（否则没有插入光标）",
          widget.text_edit.isReadOnly() is False)
    check("显示区: 光标为整块", widget.text_edit.cursorWidth() > 1,
          str(widget.text_edit.cursorWidth()))
    check("终端: 底部回显栏已移除", not hasattr(widget, "input_bar"))
    widget.append_output("Ruijie#")          # 设备打出提示符
    for ch in "en":                          # 逐字符输入
        press(widget, Qt.Key.Key_E, ch)
    check("输入缓冲: 输入时记录", widget.command_buffer == "en",
          repr(widget.command_buffer))

    # 粘贴/删除不能往显示区里塞字，否则屏上会出现设备没收到的"幽灵文本"
    from PyQt6.QtWidgets import QApplication
    QApplication.clipboard().setText("幽灵命令")
    before = widget.text()
    press(widget, Qt.Key.Key_V, "", Qt.KeyboardModifier.ControlModifier)
    check("显示区: 粘贴被挡掉", widget.text() == before, repr(widget.text()))
    press(widget, Qt.Key.Key_Delete)
    check("显示区: Delete 不改内容", widget.text() == before, repr(widget.text()))

    # 关键回归：设备还没回显我们敲的字符就按了 Tab，
    # 补全后设备整行重画 —— 缓冲必须跟到"enable"（以前会停在"en"）
    press(widget, Qt.Key.Key_Tab)
    widget.append_output("\nRuijie#enable")
    check("输入缓冲: Tab 补全后就地同步", widget.command_buffer == "enable",
          repr(widget.command_buffer))

    # 回车：缓冲清空并进历史
    press(widget, Qt.Key.Key_Return)
    check("输入缓冲: 回车后清空", widget.command_buffer == "",
          repr(widget.command_buffer))
    check("输入缓冲: 历史已记录", widget.command_history == ["enable"],
          repr(widget.command_history))
    widget.close()

    # 设备端补全（整行当普通内容追加过来，不触发 \r 重写）：缓冲也要跟上
    widget2 = TerminalWidget(2)
    widget2.append_output("Ruijie#")
    for ch in "sh":
        press(widget2, Qt.Key.Key_S, ch)
        widget2.append_output(ch)            # 设备逐字符回显
    widget2.append_output("ow version")       # 设备补全了后半段
    check("输入缓冲: 设备端追加补全也同步",
          widget2.command_buffer == "show version", repr(widget2.command_buffer))
    widget2.close()

    # 按 Tab 后的窗口期内，普通输出（不以换行开头）不能被当成"整行重画"
    # 而把当前行擦掉 —— 连打 Tab 时窗口一直开着，擦一次屏就花了
    widget3 = TerminalWidget(3)
    widget3.append_output("Ruijie#")
    press(widget3, Qt.Key.Key_Tab)
    widget3.append_output(" show")
    check("Tab 窗口: 普通输出不清空当前行",
          widget3.screen.last_line() == "Ruijie# show",
          repr(widget3.screen.last_line()))
    check("Tab 窗口: 普通输出仍显示在屏上",
          widget3.text().endswith("Ruijie# show"), repr(widget3.text()))
    widget3.close()

    # 关键回归（真实设备行为）：按 Tab 没补出东西，设备只重打了提示符，
    # 把当前行改写成"只有提示符" —— 绝不能因此把用户已输入的内容擦掉，
    # 否则屏幕上明明有字、缓冲却空了，按回车等于什么都没执行
    widget4 = TerminalWidget(4)
    widget4.append_output("Ruijie#")
    for ch in "en":
        press(widget4, Qt.Key.Key_E, ch)
        widget4.append_output(ch)              # 设备回显
    press(widget4, Qt.Key.Key_Tab)
    widget4.append_output("\rRuijie#")         # 设备整行重写，内容只剩提示符
    check("输入缓冲: 设备重打提示符不擦掉已输入",
          widget4.command_buffer == "en", repr(widget4.command_buffer))
    widget4.close()

    # 点终端任意位置：插入光标只该待在输入位置（末尾）。QTextEdit 默认"点哪
    # 光标就去哪"，屏幕上于是多出一个和输入毫无关系、还一直闪的块状光标。
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent, QTextCursor
    widget5 = TerminalWidget(5)
    widget5.append_output("Ruijie#show version")

    def mouse(kind, x=6, y=3):
        pos = QPointF(x, y)
        return QMouseEvent(kind, pos, pos, Qt.MouseButton.LeftButton,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)

    widget5.text_edit.mousePressEvent(mouse(QEvent.Type.MouseButtonPress))
    clicked_at = widget5.text_edit.textCursor().position()   # 基类会把光标挪到点击处
    widget5.text_edit.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease))
    tail_pos = len(widget5.text_edit.toPlainText())
    check("显示区: 单击任意位置后光标回到输入位置",
          widget5.text_edit.textCursor().position() == tail_pos,
          f"按下后被挪到 {clicked_at}（点哪去哪就是原始缺陷），"
          f"松开后应回到 {tail_pos}，实际 {widget5.text_edit.textCursor().position()}")

    cursor = widget5.text_edit.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(6, QTextCursor.MoveMode.KeepAnchor)
    widget5.text_edit.setTextCursor(cursor)
    widget5.text_edit.pin_caret_to_input()
    check("显示区: 正在选中输出时不抢光标（保留可复制）",
          widget5.text_edit.textCursor().hasSelection())

    widget5.text_edit.mouseDoubleClickEvent(mouse(QEvent.Type.MouseButtonDblClick))
    widget5.text_edit.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease))
    check("显示区: 双击选词没被「拉回末尾」破坏",
          widget5.text_edit.textCursor().hasSelection(),
          repr(widget5.text_edit.textCursor().selectedText()))
    widget5.close()


def test_terminal_backspace_keeps_device_echo():
    """退格只删得动用户自己敲的那一段，设备回显的内容一个字符都不能少

    2026-10-19 用户反馈：在提示符上连按退格，屏上的提示符被一个字符一个字符
    吃掉 —— "Ruijie(config)#" → "Ruijie(con" → "Ruijie(co"。
    真机（锐捷 / 华为）会把退格原样回显成 "\\b \\b"，本地照着"\\b 删一个字符"
    执行，就把设备**已经打出来**的提示符退掉了。
    """
    from PyQt6.QtGui import QKeyEvent
    from ui.terminal_widget import TerminalWidget

    def press(widget, key, text="", mods=Qt.KeyboardModifier.NoModifier):
        event = QKeyEvent(QKeyEvent.Type.KeyPress, key, mods, text)
        widget.handle_key_press(event)

    # ① 设备回显路径（本地回显关，真机就是这样）：设备把自己回显的退格发回来
    widget = TerminalWidget(11)
    widget.append_output("Ruijie#conf terminal\r\n")
    widget.append_output("Ruijie(config)#")
    prompt_line = widget.screen.last_line()
    for _ in range(5):
        widget.append_output("\b \b")           # 真机对退格的回显
    check("退格: 设备回显的退格删不动提示符",
          widget.screen.last_line() == prompt_line,
          f"期望 {prompt_line!r}，实际 {widget.screen.last_line()!r}")
    check("退格: 屏幕上提示符一个字符没少",
          widget.text().rstrip("\n").endswith(prompt_line),
          repr(widget.text()[-30:]))

    # ② 用户自己敲的字，退格仍然要删得掉（别保护过头 → "退格删不动"）
    for ch in "abc":
        press(widget, Qt.Key.Key_A, ch)
        widget.append_output(ch)                # 设备回显
    for _ in range(2):
        press(widget, Qt.Key.Key_Backspace)
        widget.append_output("\b \b")           # 设备回显退格
    check("退格: 用户输入删得掉（没保护过头）",
          widget.screen.last_line() == prompt_line + "a",
          repr(widget.screen.last_line()))
    check("退格: 输入缓冲跟着退", widget.command_buffer == "a",
          repr(widget.command_buffer))
    widget.close()

    # ③ 输入行本来就是空的：一个字节都别往下发。真机会把退格照原样回显，
    #    发出去就等于让设备去退它自己打的提示符
    widget2 = TerminalWidget(12)
    widget2.append_output("Ruijie(config)#")
    sent = []
    widget2.raw_input.connect(sent.append)
    before = widget2.text()
    for _ in range(3):
        press(widget2, Qt.Key.Key_Backspace)
    check("退格: 输入行空时不往设备发退格", sent == [], repr(sent))
    check("退格: 输入行空时屏幕不动", widget2.text() == before, repr(before[-20:]))
    widget2.close()

    # ④ 本地回显路径（设备不回显）：显示区也不能被退进提示符里
    widget3 = TerminalWidget(13)
    widget3.set_local_echo(True)
    widget3.append_output("Ruijie(config)#")
    for ch in "ab":
        press(widget3, Qt.Key.Key_A, ch)
    for _ in range(5):                          # 比输进去的字符多按 3 次
        press(widget3, Qt.Key.Key_Backspace)
    check("退格: 本地回显也不删设备回显的提示符",
          widget3.screen.last_line() == "Ruijie(config)#",
          repr(widget3.screen.last_line()))
    check("退格: 本地回显下显示区提示符完整",
          widget3.text().endswith("Ruijie(config)#"), repr(widget3.text()[-24:]))
    check("退格: 本地回显下输入已清空",
          widget3.command_buffer == "", repr(widget3.command_buffer))
    widget3.close()


def test_terminal_backspace_without_device_echo():
    """设备**不回显退格**时，退格必须本地把用户敲的字符抹掉

    2026-10-19 用户反馈（华为 S12708，截图里一屏 "<JianKong_Core>sy"）："无法删除
    输入的内容"。华为 VRP 这类设备会回显输入的字符、却**不回显退格**（终端该自己
    擦）；只有当设备既不回显输入、也不回显退格时（local_echo=on）才做了本地擦除，
    于是这种"只回显输入"的设备上，退格发下去了、屏幕上那个字符永远留着。
    """
    from PyQt6.QtGui import QKeyEvent
    from ui.terminal_widget import TerminalWidget

    def press(widget, key, text="", mods=Qt.KeyboardModifier.NoModifier):
        event = QKeyEvent(QKeyEvent.Type.KeyPress, key, mods, text)
        widget.handle_key_press(event)

    widget = TerminalWidget(21)
    widget.append_output("<JianKong_Core>")              # 设备回显提示符
    for ch in "sy":                                       # 设备逐个回显输入的字符
        press(widget, Qt.Key.Key_S, ch)
        widget.append_output(ch)
    check("退格: 设备回显输入后缓冲同步", widget.command_buffer == "sy",
          repr(widget.command_buffer))

    press(widget, Qt.Key.Key_Backspace)                   # 设备**不回显**这次退格
    check("退格: 设备不回显退格时也要删掉屏上的字符",
          widget.screen.last_line() == "<JianKong_Core>s",
          f"实际 {widget.screen.last_line()!r}（'y' 没被抹掉就是用户说的"
          f"『无法删除输入的内容』）")
    check("退格: 显示区同步删掉（不是只改缓冲）",
          widget.text().endswith("<JianKong_Core>s"), repr(widget.text()[-24:]))
    check("退格: 缓冲同步", widget.command_buffer == "s", repr(widget.command_buffer))
    widget.close()

    # 一路退到空：提示符一个字符都不能少
    widget2 = TerminalWidget(22)
    widget2.append_output("<JianKong_Core>")
    for ch in "sy":
        press(widget2, Qt.Key.Key_S, ch)
        widget2.append_output(ch)
    for _ in range(4):                                    # 比输入的多按 2 次
        press(widget2, Qt.Key.Key_Backspace)
    check("退格: 退到空行后提示符完整",
          widget2.screen.last_line() == "<JianKong_Core>"
          and widget2.text().endswith("<JianKong_Core>"),
          repr(widget2.screen.last_line()))
    check("退格: 退到空行后缓冲为空", widget2.command_buffer == "",
          repr(widget2.command_buffer))
    widget2.close()


def test_terminal_local_echo_no_duplicate():
    """设备本身会回显时，本地回显绝不能和它叠加成双字

    2026-10-19 用户反馈（锐捷 S2928G-E console）："输入命令会重复，例如输入 s
    会显示为 ss，自动补全也是如此"。会话日志证明设备只收到**一个** s —— 多出来
    那个是本地的：``check_console_echo`` 在连上 1.7s 后用"设备有没有吐过字符"猜
    它回不回显，锐捷对唤醒回车**不重打提示符**，于是被误判成"设备零回显"、自动
    打开了本地回显，之后每个字符都被画两遍（设备一遍 + 本地一遍）。

    根因是**靠猜**：猜错一次，整个会话的双字就改不回来。正确做法是让设备自己
    作证 —— 本地回显过的字符，若在很短时间内被设备原样回显回来，就说明设备会
    回显：立刻撤掉本地回显，并把这一遍重复的字符丢掉（用户看到的就是一个字）。
    """
    from PyQt6.QtGui import QKeyEvent
    from ui.terminal_widget import TerminalWidget

    def press(widget, key, text="", mods=Qt.KeyboardModifier.NoModifier):
        event = QKeyEvent(QKeyEvent.Type.KeyPress, key, mods, text)
        widget.handle_key_press(event)

    widget = TerminalWidget(23)
    widget.append_output("Ruijie(config)#")     # 设备提示符
    widget.set_local_echo(True)                  # 被误判成"设备不回显"后的状态

    press(widget, Qt.Key.Key_S, "s")
    check("双字: 本地回显先画出用户敲的字", widget.screen.last_line().endswith("s"),
          repr(widget.screen.last_line()))

    widget.append_output("s")                     # 设备也把它回显回来
    check("双字: 设备回显同一字符时不能变成 'ss'",
          widget.screen.last_line() == "Ruijie(config)#s",
          f"实际 {widget.screen.last_line()!r}（'ss' 就是用户说的『输入命令会重复』）")
    check("双字: 显示区也不能多出那个字符",
          widget.text().endswith("Ruijie(config)#s"), repr(widget.text()[-20:]))
    check("双字: 一旦设备证明会回显，就自动撤掉本地回显",
          widget.local_echo is False, f"local_echo={widget.local_echo}")
    check("双字: 撤掉后缓冲不多不少", widget.command_buffer == "s",
          repr(widget.command_buffer))

    # 之后继续敲：设备回显照常,屏幕只出现一份
    press(widget, Qt.Key.Key_H, "h")
    widget.append_output("h")
    check("双字: 后续字符同样只显示一份",
          widget.screen.last_line() == "Ruijie(config)#sh",
          repr(widget.screen.last_line()))
    check("双字: 后续字符缓冲正确", widget.command_buffer == "sh",
          repr(widget.command_buffer))
    widget.close()

    # 真·不回显的设备（本地回显是它唯一能看见字的原因）不能被误判成"会回显"
    widget2 = TerminalWidget(24)
    widget2.append_output("<HUAWEI>")
    widget2.set_local_echo(True)
    press(widget2, Qt.Key.Key_D, "d")
    press(widget2, Qt.Key.Key_I, "i")              # 设备一个字都不回
    check("双字: 设备不回显时本地回显必须留着",
          widget2.local_echo is True and widget2.text().endswith("<HUAWEI>di"),
          f"local_echo={widget2.local_echo} text={widget2.text()[-12:]!r}")
    # 设备晚一点吐了个**以刚敲的字开头**的异步消息（不是回显），不能据此关掉本地回显
    widget2.append_output("i am a log line\r\n")
    check("双字: 异步日志整段不是回显，不能据此关掉本地回显",
          widget2.local_echo is True, f"local_echo={widget2.local_echo}")
    widget2.close()


def test_terminal_cr_nul_not_swallowed():
    """设备用 \\r\\x00\\r\\n 收行时，屏上必须留下正文

    2026-10-19 用户反馈："回显内容有问题 没有数据 但是设备是有数据的"。
    真机上设备给了 999 字节，控件里只剩 41 个字符（就一行 ---- More ----）：
    H3C 每打一行都是 `文本 + \\r\\x00\\r\\n`，缓冲层把中间那个裸 \\r 当成
    "进度条重写当前行"，于是**每一行刚打出来就被自己抹掉**。
    """
    from core.telnet_connector import clean_telnet_text
    from ui.terminal_widget import TerminalWidget

    comware = ("display version\r\x00\r\n"
               "H3C Comware Software, Version 7.1.070, Release 7639P03\r\x00\r\n"
               "Copyright (c) 2004-2023 New H3C Technologies Co., Ltd.\r\x00\r\n"
               "H3C S7503X uptime is 8 weeks\r\x00\r\n"
               "---- More ----")
    # ① 读线程出口清洗后的样子：就是普通 \r\n
    cleaned = clean_telnet_text(comware)
    check("回显: 清洗后没有 NUL / CR NUL", "\x00" not in cleaned and "\r\x00" not in cleaned,
          repr(cleaned[:40]))

    widget = TerminalWidget(7)
    widget.append_output(cleaned)
    shown = widget.text()
    check("回显: 屏上有设备正文（不是只剩分页标记）",
          "H3C Comware" in shown and "Copyright" in shown,
          f"实际 {len(shown)} 字符：{shown[:60]!r}")
    check("回显: 屏上行数对得上（5 行内容都在）",
          len([ln for ln in shown.splitlines() if ln.strip()]) >= 5,
          f"{len([ln for ln in shown.splitlines() if ln.strip()])} 行")
    check("回显: 分页标记仍然显示（用户在等分页时看得见）", "More" in shown)

    # ② 没清洗就喂进来（别的接入方式直连控件）时，缓冲层也要自己扛住
    raw_widget = TerminalWidget(8)
    raw_widget.append_output(comware)
    check("回显: 未经清洗的 CR NUL 也不会抹掉正文（缓冲层兜底）",
          "H3C Comware" in raw_widget.text(),
          f"实际 {len(raw_widget.text())} 字符")

    # ③ 不能修过头：进度条那种"裸 CR + 可打印字符"仍只留最后一次
    progress = TerminalWidget(9)
    progress.append_output("下载 10%\r下载 90%")
    check("回显: 进度条式重写仍只留最后一次",
          progress.text().strip().endswith("下载 90%") and "10%" not in progress.text(),
          repr(progress.text()))


def test_console_echo_no_doubling_any_shape():
    """console 上敲一整条命令：屏幕、缓冲、自动提示三处都不许重字

    2026-10-19 用户截图（锐捷 S2928G-E console）：
        Ruijie(config)#sshhooww
        浮层提示：sshhooww rruunnn
        % Incomplete command
    即敲 "show run" 每个字符被画两遍，而且**自动提示是按错的缓冲算的**（浮层里
    也跟着是重字）。上面的 test_terminal_local_echo_no_duplicate 只演了单个字符，
    这里把整条命令、三种设备回显形态（逐字 / 整段 / 带换行）都过一遍，并直接测量
    "提示浮层这次是按什么前缀算的" —— 光看屏幕对了不算数，缓冲错了提示就是错的。
    """
    from PyQt6.QtGui import QKeyEvent
    from ui.terminal_widget import TerminalWidget

    def key_for(ch):
        return Qt.Key.Key_Space if ch == " " else getattr(Qt.Key, f"Key_{ch.upper()}")

    def replay(shape, command):
        widget = TerminalWidget(1201)
        widget.append_output("Ruijie(config)#")   # 设备提示符
        widget.set_local_echo(True)               # 旧代码误判"设备不回显"后的状态

        prefixes = []
        original = widget.dictionary.suggest
        widget.dictionary.suggest = lambda p: (prefixes.append(p), original(p))[1]

        for ch in command:
            widget.handle_key_press(QKeyEvent(
                QKeyEvent.Type.KeyPress, key_for(ch),
                Qt.KeyboardModifier.NoModifier, ch))
            if shape == "per_char":
                widget.append_output(ch)          # 设备逐字回显（真实 console 节奏）
        if shape == "whole":
            widget.append_output(command)
        elif shape == "with_crlf":
            widget.append_output(command + "\r\n")

        shown = [ln for ln in widget.text().splitlines() if ln.strip()]
        last = shown[-1] if shown else ""
        buffer = widget.command_buffer
        widget.close()
        return last, buffer, prefixes

    for shape, title in (("per_char", "逐字回显"), ("whole", "整段回显"),
                         ("with_crlf", "整段回显带换行")):
        last, buffer, prefixes = replay(shape, "show run")
        check(f"console重字[{title}]: 屏幕上是一条 show run（不是 sshhooww）",
              "sshhooww" not in last and last.endswith("show run"), repr(last))
        check(f"console重字[{title}]: 输入缓冲是 show run",
              buffer == "show run", repr(buffer))
        check(f"console重字[{title}]: 自动提示按原文算前缀",
              bool(prefixes) and prefixes[-1] == "show run"
              and all(p == "show run"[:len(p)] for p in prefixes),
              f"最后一次前缀 {prefixes[-1] if prefixes else None!r}")


def test_patrol_device_edit():
    """巡检设备必须能手填 IP/协议/账号/密码

    2026-10-19 用户要求："多设备巡检应该有一个编辑页面 里面填写设备 IP 选择协议
    填写账号和密码"。原来只能"从当前会话添加/添加最近连接"，一台还没登过的设备
    就没处填凭据 —— 结果巡检一跑全失败，用户以为巡检坏了。
    """
    from ui.device_edit_dialog import DeviceEditDialog
    from ui.patrol_dialog import PatrolDialog

    edit = DeviceEditDialog(None, device={"protocol": "Telnet", "host": "10.2.0.1",
                                          "port": 23, "username": "admin",
                                          "password": "p@ss"})
    device = edit.get_device()
    check("巡检设备: 编辑框带回 IP/协议/账号/密码",
          device["host"] == "10.2.0.1" and device["protocol"] == "Telnet"
          and device["username"] == "admin" and device["password"] == "p@ss",
          f"{device.get('host')}/{device.get('protocol')}/{device.get('username')}")
    check("巡检设备: 没填名字时用 IP 当名字", device["name"] == "10.2.0.1",
          device["name"])

    empty = DeviceEditDialog(None)
    check("巡检设备: 新增默认 SSH:22",
          empty.get_device()["protocol"] == "SSH" and empty.get_device()["port"] == 22,
          f"{empty.get_device()['protocol']}:{empty.get_device()['port']}")
    empty.host_input.setText("192.168.1.9")
    empty.protocol_combo.setCurrentText("Telnet")
    check("巡检设备: 切到 Telnet 端口自动变 23", empty.get_device()["port"] == 23,
          str(empty.get_device()["port"]))

    dialog = PatrolDialog(None, candidates=[], sessions=[])
    headers = [dialog.table.horizontalHeaderItem(i).text()
               for i in range(dialog.table.columnCount())]
    check("巡检设备: 列表有密码列", headers == ["选择", "名称", "协议", "地址", "密码", "来源"],
          f"实际 {headers}")

    dialog.add_device_row({"name": "有密码", "protocol": "Telnet", "host": "10.2.0.1",
                           "port": 23, "username": "admin", "password": "x"})
    dialog.add_device_row({"name": "缺密码", "protocol": "Telnet", "host": "10.9.9.9",
                           "port": 23, "username": "admin", "password": ""})
    check("巡检设备: 密码列区分已填/未填",
          dialog.table.item(0, 4).text() == "已填"
          and dialog.table.item(1, 4).text() == "未填",
          f"{dialog.table.item(0, 4).text()}/{dialog.table.item(1, 4).text()}")
    check("巡检设备: 缺密码那行有提示", bool(dialog.table.item(1, 4).toolTip()),
          "没有提示用户就不知道该怎么补")

    dialog.replace_device(("Telnet", "10.2.0.1", None, "admin"),
                          {"name": "改过名", "protocol": "Telnet", "host": "10.2.0.1",
                           "port": 23, "username": "admin", "password": "new"})
    check("巡检设备: 替换同地址设备原地更新",
          dialog.table.item(0, 1).text() == "改过名", dialog.table.item(0, 1).text())
    check("巡检设备: 去重键按协议/地址/用户名",
          ("Telnet", "10.2.0.1", None, "admin") in dialog.existing_keys())

    saved = {}
    dialog.settings.set_patrol_devices = lambda devices: saved.__setitem__("d", devices)
    dialog.settings.save = lambda: None
    try:
        dialog.save_settings()
    finally:
        # ⚠ 必须还原：Settings 是**共享单例**，这里装上去的替身会一直留在实例上，
        #   后面 test_settings() 拿到的还是这个对象，`save()` 成了空函数 ⇒
        #   它那句 "配置保存" 找不到文件而失败（与终端改动无关，纯测试顺序污染）。
        for _name in ("set_patrol_devices", "save"):
            dialog.settings.__dict__.pop(_name, None)
    entries = saved.get("d") or []
    check("巡检设备: 设备列表会存下来（下次不用重填）", len(entries) == 2,
          f"{len(entries)} 台")
    check("巡检设备: 存盘时剥掉密码", all("password" not in entry for entry in entries),
          f"字段 {[sorted(entry) for entry in entries][:1]}")


def test_session_connection_lost_visible():
    """设备掉线必须在屏幕上显示出来

    2026-10-19 用户反馈："设备回显的掉线 要显示出来"。之前连接器早就发出了
    ``connection_lost``，但界面**一个地方都没接** —— 掉线后终端就此静止，
    用户分不清是设备没反应、命令还在跑，还是已经断了。
    """
    from ui.main_window import MainWindow
    from ui.session_tab import SessionTab

    class FakeConnector:
        def is_connected(self):
            return False

        def info(self):
            return "COM5 @9600"

    class FakeSession:
        def __init__(self):
            self.connector = FakeConnector()
            self.protocol = "Serial"
            self.config = {'auto_reconnect': True}
            self.name = "假 Console"

        def close_log(self):            # 收尾时 closeEvent 会挨个关会话
            pass

    window = MainWindow()
    window.update_status = lambda: None          # 只验提示，不验状态重算
    sid = 91
    window.session_manager._sessions[sid] = FakeSession()
    window.tabs[sid] = SessionTab(sid, "假 Console", "COM5 @9600")

    emitted = []
    window.session_manager.connection_lost.connect(
        lambda s, r: emitted.append((s, r)))
    window.session_manager._on_connection_lost(sid)
    window.session_manager._on_connection_lost(sid)      # 读线程 + 写线程各报一次
    check("掉线: 连接器重复报只提示一次", len(emitted) == 1, f"{emitted}")
    check("掉线: 原因说得清楚（串口 + 会重连）",
          bool(emitted) and "串口" in emitted[0][1] and "重连" in emitted[0][1],
          f"{emitted}")

    window.on_connection_lost(sid, emitted[0][1])
    text = window.tabs[sid].terminal.text()
    check("掉线: 终端里落一行『连接已断开 + 时间』", "[连接已断开" in text,
          f"实际末尾 {text[-80:]!r}")
    check("掉线: 状态条写明已断开",
          "已断开" in window.tabs[sid].info_label.text(),
          window.tabs[sid].info_label.text())

    # 掉线后继续敲键盘：按键既没发出去、屏幕上也没反应，等于"键盘坏了"
    window.on_raw_input(sid, "s")
    window.on_raw_input(sid, "h")
    text2 = window.tabs[sid].terminal.text()
    check("掉线: 输入被丢弃时提示，且只提示一次",
          text2.count("[未发送]") == 1, f"出现 {text2.count('[未发送]')} 次")

    # 重连成功：状态灯要绿回来、标记要清掉，下一次掉线还能再报
    window.on_connection_info(sid, "串口已自动重连成功")
    check("掉线: 重连成功不能把状态灯按成红色",
          window.tabs[sid]._last_connected is True,
          f"_last_connected={window.tabs[sid]._last_connected}")
    window.session_manager._on_reconnected(sid)
    window.session_manager._on_connection_lost(sid)
    check("掉线: 重连之后能再次上报下一条", len(emitted) == 2, f"{emitted}")
    window.close()


def test_terminal_tab_reaches_device():
    """按 Tab 必须真的发给设备，且不能把焦点跳走

    Qt 的 QWidget::event() 对 Tab 的处理优先于 keyPressEvent：只要焦点链里
    还有下一个可聚焦控件，Tab 就会被它吃掉去跳转焦点（跳到会话标签栏 /
    设备库搜索框），终端既收不到 Tab、之后还会失去焦点。
    """
    from PyQt6.QtTest import QTest
    from core.session import SessionInfo

    settings, snapshot = _settings_snapshot()
    try:
        settings.set("restore_sessions", False)
        window = MainWindow()
        window.show()
        QApplication.setActiveWindow(window)
        _APP.processEvents()

        manager = window.session_manager
        session = SessionInfo(1, "Console", "Serial")
        session.connector = DummyConnector()
        session.config = {"host": "COM5", "protocol": "Serial"}
        manager._sessions[1] = session
        manager._next_id = 2
        window.on_session_created(1, "Console")
        _APP.processEvents()

        terminal = window.tabs[1].terminal
        terminal.text_edit.setFocus()
        _APP.processEvents()

        QTest.keyClick(terminal.text_edit, Qt.Key.Key_Tab)
        _APP.processEvents()
        check("终端: Tab 真的发给设备了",
              "\t" in session.connector.sent, repr(session.connector.sent[-3:]))
        focused = _APP.focusWidget()
        check("终端: 按 Tab 后焦点没跑掉",
              focused is terminal.text_edit,
              type(focused).__name__ if focused else "None")

        QTest.keyClick(terminal.text_edit, Qt.Key.Key_E,
                       Qt.KeyboardModifier.NoModifier)
        _APP.processEvents()
        check("终端: 按键仍进终端", "e" in session.connector.sent,
              repr(session.connector.sent[-3:]))
        window.close()
    finally:
        _settings_restore(settings, snapshot)


def test_nav_rail():
    """左侧工具窗口条：点图标开关面板，勾选态跟着面板可见性走"""
    settings, snapshot = _settings_snapshot()
    try:
        settings.set("restore_sessions", False)
        window = MainWindow()
        window.show()
        QApplication.setActiveWindow(window)
        _APP.processEvents()

        rail = window.rail
        check("工具窗口条: 三个入口都在",
              all(rail.is_checked(key) in (True, False)
                  for key in ("devices", "ai")), repr(rail._actions.keys()))
        check("工具窗口条: 默认设备库选中、AI 未选中",
              rail.is_checked("devices") is True and rail.is_checked("ai") is False,
              f"devices={rail.is_checked('devices')} ai={rail.is_checked('ai')}")
        check("三栏分隔条: 热区加宽到 6px（4px 抓不住）",
              window.side_splitter.handleWidth() >= 6,
              str(window.side_splitter.handleWidth()))
        check("三栏分隔条: 三栏都挂在 splitter 上",
              window.side_splitter.count() == 3,
              str(window.side_splitter.count()))

        before = window.side_splitter.sizes()[0]
        window.on_rail_item("devices")
        check("工具窗口条: 点图标收起设备库",
              window.vault_panel.isHidden() is True,
              str(window.side_splitter.sizes()))
        check("工具窗口条: 勾选态跟着面板变", rail.is_checked("devices") is False)

        # 收起来再打开，宽度要还原（至少不低于最小宽度，不然面板会缩成一条缝）
        window.on_rail_item("devices")
        _APP.processEvents()
        check("工具窗口条: 重新打开时宽度还原",
              window.side_splitter.sizes()[0] >= 170,
              f"{before} -> {window.side_splitter.sizes()}")

        window.on_rail_item("ai")
        check("工具窗口条: 点图标打开 AI 面板",
              window.side_splitter.sizes()[1] > 0 and rail.is_checked("ai") is True,
              f"sizes={window.side_splitter.sizes()} checked={rail.is_checked('ai')}")
        check("AI 面板: 打开后有实际宽度",
              window.side_splitter.sizes()[1] > 200,
              str(window.side_splitter.sizes()))
        check("AI 面板: 打开后页签栏可见",
              window.right_tabs.tabBar().isVisible() is True)

        # 从别处（视图菜单 / 面板上的 ✕）关掉时，图标也要跟着取消勾选
        window.set_ai_panel_visible(False)
        _APP.processEvents()
        check("工具窗口条: 别处关面板也同步", rail.is_checked("ai") is False,
              str(rail.is_checked("ai")))
        window.close()
    finally:
        _settings_restore(settings, snapshot)


def test_terminal_find_and_zoom():
    """终端查找（Ctrl+F）与字号缩放（Ctrl+= / Ctrl+- / Ctrl+0 / Ctrl+滚轮）"""
    from ui.terminal_widget import TerminalWidget

    widget = TerminalWidget(1)
    widget.append_output("Ruijie#show version\n")
    widget.append_output("System software version : RGOS 10.4(2b12)\n")
    widget.append_output("Ruijie#show interfaces status\n")

    check("查找: 默认收起", widget.find_bar.isHidden() is True)
    widget.toggle_find_bar()
    check("查找: Ctrl+F 唤出", widget.find_bar.isHidden() is False)
    widget.toggle_find_bar()
    check("查找: 再按 Ctrl+F 收起", widget.find_bar.isHidden() is True)
    widget.toggle_find_bar()

    widget.find_input.setText("show")
    check("查找: 命中计数", widget.find_label.text() == "2 处",
          repr(widget.find_label.text()))
    check("查找: 已选中第一处",
          widget.text_edit.textCursor().selectedText() == "show",
          repr(widget.text_edit.textCursor().selectedText()))

    widget.find_next()
    check("查找: 下一个不报错", widget.text_edit.textCursor().hasSelection() is True)

    widget.find_input.setText("不存在的命令")
    check("查找: 无匹配时给出提示", widget.find_label.text() == "无匹配",
          repr(widget.find_label.text()))

    # 查找框里的 Esc 要能直接关掉（窗口没显示时 hasFocus 不可靠，这里直接走
    # 事件过滤器，跟真实按键走的是同一条路）
    from PyQt6.QtGui import QKeyEvent
    escape = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                       Qt.KeyboardModifier.NoModifier)
    widget.eventFilter(widget.find_input, escape)
    check("查找: Esc 关闭查找条", widget.find_bar.isHidden() is True)

    base = widget._font_size
    widget.zoom_font(1)
    check("字号: 放大", widget._font_size == base + 1, str(widget._font_size))
    widget.zoom_font(-1)
    check("字号: 缩小", widget._font_size == base, str(widget._font_size))
    widget.zoom_font(5)
    widget.zoom_font(0)
    check("字号: Ctrl+0 还原", widget._font_size == base, str(widget._font_size))
    # 光标宽度要跟着字号走，否则放大后整块光标会跟字对不上
    widget.zoom_font(4)
    check("字号: 放大后光标变宽", widget.text_edit.cursorWidth() > 10,
          str(widget.text_edit.cursorWidth()))
    widget.close()


def test_terminal_tab_repeat():
    """按住 Tab：重复按键不再一次次发给设备；过期的候选不能拿去补全

    按住 Tab 时系统会以每秒几十次重复发送，每次都真的下发 → 设备一遍遍吐
    补全清单、屏幕被反复重画（表现为"卡住"、退格也删不动）。
    """
    from PyQt6.QtGui import QKeyEvent
    from ui.terminal_widget import TerminalWidget

    def key_event(auto_repeat=False):
        return QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Tab,
                         Qt.KeyboardModifier.NoModifier, "", auto_repeat)

    widget = TerminalWidget(1)
    widget.set_vendor("ruijie")
    sent = []
    widget.raw_input.connect(sent.append)

    widget.handle_key_press(key_event(False))      # 第一次按下
    widget.handle_key_press(key_event(True))       # 之后的按住重复
    widget.handle_key_press(key_event(True))
    check("按住 Tab: 重复按键不重复下发", sent.count("\t") == 1, repr(sent))

    # 候选必须还对得上当前命令行，否则补出来的就是一条错命令
    def press(ch):
        widget.handle_key_press(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_S,
                                          Qt.KeyboardModifier.NoModifier, ch))

    widget.append_output("Ruijie#")
    for ch in "sh":
        press(ch)
        widget.append_output(ch)                   # 设备逐字符回显
    check("按住 Tab: 用例前置条件（按 sh 有候选）",
          widget.hint_popup.is_active() is True, str(widget.hint_popup.count()))

    widget.append_output("ow cpu")                 # 设备把这一行补成了 show cpu
    check("按住 Tab: 设备补全后缓冲跟着走", widget.command_buffer == "show cpu",
          repr(widget.command_buffer))
    sent.clear()
    widget._accept_hint("show arp")                # 拿着按 "sh" 算出来的旧候选来采纳
    check("按住 Tab: 过期候选不往设备发尾巴", sent == [], repr(sent))
    check("按住 Tab: 过期候选不改动命令行",
          widget.command_buffer == "show cpu", repr(widget.command_buffer))
    widget.close()


def test_command_hint():
    """命令建议：候选来源、前缀匹配、执行过的排前面"""
    from ui.command_hint import CommandDictionary

    dictionary = CommandDictionary("ruijie")
    hits = dictionary.suggest("sh")
    check("命令建议: 按前缀给出候选", "show version" in hits, repr(hits))
    check("命令建议: 空输入不给候选", dictionary.suggest("") == [])
    check("命令建议: 大小写不敏感", dictionary.suggest("SHOW VER") == ["show version"],
          repr(dictionary.suggest("SHOW VER")))
    check("命令建议: 只给前缀匹配的",
          all(h.lower().startswith("sh") for h in hits), repr(hits))
    check("命令建议: 已输入完整命令就不再提示",
          "show arp" not in dictionary.suggest("show arp"), repr(dictionary.suggest("show arp")))

    dictionary.mark_used("show version")
    hits = dictionary.suggest("show v")
    check("命令建议: 执行过的排最前", hits and hits[0] == "show version", repr(hits[:3]))

    dictionary.add("show version")
    check("命令建议: 重复收录不重复出现",
          dictionary.all_commands().count("show version") == 1,
          str(dictionary.all_commands().count("show version")))

    # 文档里抽出来的参数碎片不能进提示表 —— 否则就是"提示的命令是错的"
    from ui.command_hint import looks_like_command
    for junk in ("text", "un", "at", "state", "description", "line-detect"):
        check(f"命令建议: 文档碎片不算命令 {junk!r}",
              looks_like_command(junk) is False, repr(junk))
    for good in ("show version", "no shutdown", "display interface brief",
                 "reload", "write", "system-view"):
        check(f"命令建议: 真命令照收 {good!r}",
              looks_like_command(good) is True, repr(good))
    dirty = CommandDictionary("ruijie")
    dirty.add("state")
    check("命令建议: 碎片进不了提示表",
          "state" not in dirty.all_commands(), repr(dirty.all_commands()))
    # 用户亲手敲过的命令不受这条判断限制（设备已经认了）
    manual = CommandDictionary("ruijie")
    manual.mark_used("state")
    check("命令建议: 亲手敲过的不受碎片过滤影响",
          "state" in manual.all_commands(), repr(manual.all_commands()))


def test_terminal_line_resync():
    """设备那一行上还留着内容时，退格/回车要跟着它走（输入缓冲不能装作看不见）

    真实故障：屏幕与输入缓冲不同步时，缓冲是空的、屏幕上却有字 —— 表现为
    "退格删不掉内容""回车什么也不执行（也不报错）"。设备那一行上的残留
    必须能被认下来再删掉/执行。
    """
    from PyQt6.QtGui import QKeyEvent
    from ui.terminal_widget import TerminalWidget

    def press(widget, key, text="", mods=Qt.KeyboardModifier.NoModifier):
        widget.handle_key_press(QKeyEvent(QKeyEvent.Type.KeyPress, key, mods, text))

    widget = TerminalWidget(1)
    widget.append_output("Ruijie#")              # 设备打出提示符
    widget.append_output("show ver")             # 设备行上留着半截命令，缓冲却是空的
    check("残留处理: 设备行有内容时算作忙", widget.input_pending() is True)
    sent = []
    widget.raw_input.connect(sent.append)
    press(widget, Qt.Key.Key_Backspace)
    check("残留处理: 退格能删掉屏幕上的字",
          widget.command_buffer == "show ve", repr(widget.command_buffer))
    check("残留处理: 退格已下发设备", sent == ["\b"], repr(sent))

    press(widget, Qt.Key.Key_Return)
    check("残留处理: 回车把这一行记进历史",
          widget.command_history == ["show ve"], repr(widget.command_history))
    check("残留处理: 回车后缓冲清空", widget.command_buffer == "",
          repr(widget.command_buffer))

    # 提示符自带尾部空格时不能一直判成"正在输入"，否则型号探测全被挡掉
    widget2 = TerminalWidget(2)
    widget2.append_output("Ruijie# ")
    check("残留处理: 只有提示符后面的空格不算输入",
          widget2.input_pending() is False, repr(widget2._device_line_input()))
    widget.close()
    widget2.close()


def test_terminal_command_hint():
    """终端里边敲边给建议：上下选、Tab 采纳、Esc 收起"""
    from PyQt6.QtGui import QKeyEvent
    from ui.terminal_widget import TerminalWidget

    def press(widget, key, text="", mods=Qt.KeyboardModifier.NoModifier):
        widget.handle_key_press(QKeyEvent(QKeyEvent.Type.KeyPress, key, mods, text))

    widget = TerminalWidget(1)
    widget.set_vendor("ruijie")
    sent = []
    widget.raw_input.connect(sent.append)

    for ch in "sh":
        press(widget, Qt.Key.Key_S, ch)
    check("命令建议: 敲字后浮层出现", widget.hint_popup.is_active() is True,
          str(widget.hint_popup.count()))
    first = widget.hint_popup.current_command()
    press(widget, Qt.Key.Key_Down)
    check("命令建议: 下键切换候选项",
          widget.hint_popup.current_command() != first,
          repr((first, widget.hint_popup.current_command())))

    target = widget.hint_popup.current_command()
    sent.clear()
    press(widget, Qt.Key.Key_Tab)
    check("命令建议: Tab 采纳建议", widget.command_buffer == target,
          repr(widget.command_buffer))
    check("命令建议: 只补发差的尾巴",
          "".join(sent) == target[2:], repr("".join(sent)))
    check("命令建议: 采纳后收起浮层", widget.hint_popup.is_active() is False)

    # 回车执行：该命令被记住，之后提同类前缀排最前
    press(widget, Qt.Key.Key_Return)
    check("命令建议: 回车后命令已收录",
          target in widget.dictionary.all_commands(), repr(target))
    check("命令建议: 执行过的排最前",
          widget.dictionary.suggest(target.split()[0])[0] == target,
          repr(widget.dictionary.suggest(target.split()[0])[:3]))

    for ch in "sh":
        press(widget, Qt.Key.Key_S, ch)
    check("命令建议: 再敲又出现", widget.hint_popup.is_active() is True)
    press(widget, Qt.Key.Key_Escape)
    check("命令建议: Esc 收起浮层", widget.hint_popup.is_active() is False)

    # 浮层收起时 Tab 仍然交给设备做补全
    sent.clear()
    press(widget, Qt.Key.Key_Tab)
    check("命令建议: 浮层收起时 Tab 发给设备", "\t" in sent, repr(sent))
    widget.close()


def test_device_version_info():
    """各厂商 show/display version 回显都能认出型号与版本（各家真机格式）"""
    from core.terminal import extract_device_info

    samples = [
        # 锐捷：型号写在描述里；且 hardware version 也带 "version:" —— 不能抓成 1.05
        ("锐捷 S2928G-E", """
Ruijie>show version
System description     : Ruijie Full Gigabit Security & Intelligence Access Switch (S2928G-E) By Ruijie Networks
System start time      : 2026-09-22 20:52:16
System uptime          : 1:15:6
System hardware version: 1.05
System software version: RGOS 10.4(2b12) Release(154722)
System BOOT version    : 10.4(2b12) Release(154722)
System serial number   : G1G304J002013
""", "S2928G-E", "10.4(2b12) Release(154722)"),

        ("锐捷 S5750", """
Ruijie#show version
System description      : Ruijie Full 10G Routing Switch(S5750-24GT4XS-P) By Ruijie Networks
System hardware version : 1.10
System software version : RGOS 11.4(1)B70P1 Release(118875)
""", "S5750-24GT4XS-P", "11.4(1)B70P1 Release(118875)"),

        # 华为：版本行里也写型号（5.170 (S5720 ...)），要认开机行里的整机型号
        ("华为 S5720 VRP5", """
<HUAWEI>display version
Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.170 (S5720 V200R019C10SPC500)
Copyright (C) 2012-2020 HUAWEI TECH CO., LTD
HUAWEI S5720-28X-SI-AC Routing Switch uptime is 12 weeks, 2 days
""", "S5720-28X-SI-AC", "5.170"),

        ("华为 CE6865 VRP8", """
<HUAWEI>display version
Huawei Versatile Routing Platform Software
VRP (R) software, Version 8.180 (CE6865EI V200R005C10SPC800)
HUAWEI CE6865-48S8CQ-EI uptime is 30 days, 4 hours, 5 minutes
""", "CE6865-48S8CQ-EI", "8.180"),

        ("H3C S5560 Comware7", """
<H3C>display version
H3C Comware Software, Version 7.1.070, Release 6126P01
Copyright (c) 2004-2020 New H3C Technologies Co., Ltd.
H3C S5560-30C-EI uptime is 0 weeks, 0 days, 2 hours, 1 minute
""", "S5560-30C-EI", "7.1.070 Release 6126P01"),

        ("H3C S5500 Comware5", """
<H3C>display version
H3C Comware Platform Software
Comware Software, Version 5.20, Release 2220P02
Copyright (c) 2004-2014 Hangzhou H3C Tech. Co., Ltd.
H3C S5500-28C-EI uptime is 0 week, 0 day, 0 hour, 1 minute
""", "S5500-28C-EI", "5.20 Release 2220P02"),

        ("H3C MSR36-40", """
<H3C>display version
H3C Comware Software, Version 7.1.064, Release 0613P16
H3C MSR36-40 uptime is 1 week, 3 days, 2 hours
""", "MSR36-40", "7.1.064 Release 0613P16"),

        # Cisco：镜像名里的 C2960 不能盖过整机型号 WS-C2960-24TT-L
        ("Cisco 2960 IOS15", """
Switch>show version
Cisco IOS Software, C2960 Software (C2960-LANBASEK9-M), Version 15.0(2)SE11, RELEASE SOFTWARE (fc3)
cisco WS-C2960-24TT-L (PowerPC405) processor (revision B0) with 65536K bytes of memory.
""", "WS-C2960-24TT-L", "15.0(2)SE11"),

        ("Cisco C9300 IOS-XE", """
Switch>show version
Cisco IOS XE Software, Version 17.03.04a
Cisco IOS Software [Amsterdam], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 17.3.4a, RELEASE SOFTWARE (fc7)
cisco C9300-48P (X86) processor with 1526546K/6147K bytes of memory.
""", "C9300-48P", "17.03.04a"),

        ("Cisco Nexus NX-OS", """
switch# show version
Cisco Nexus Operating System (NX-OS) Software
TAC support: http://www.cisco.com/tac
  system:    version 9.3(5)
  BIOS:      version 07.66
cisco Nexus3048 Chassis ("48x1GE + 4x10G Supervisor + 2x10G")
""", "Nexus3048", "9.3(5)"),

        ("Cisco ISR4331 IOS-XE", """
Router>show version
Cisco IOS XE Software, Version 16.09.04
Cisco IOS Software [Fuji], ISR Software (X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 16.9.4
cisco ISR4331/K9 (1RU) processor with 1786636K/6147K bytes of memory.
""", "ISR4331", "16.09.04"),
    ]

    for label, text, want_model, want_version in samples:
        info = extract_device_info(text)
        check(f"型号识别: {label} 型号", info["model"] == want_model, repr(info))
        check(f"型号识别: {label} 版本",
              info["os_version"] == want_version, repr(info))
        check(f"型号识别: {label} 厂商认出来了", info["vendor"] != "unknown", repr(info))

    info = extract_device_info("% Unknown command\nRouter>")
    check("型号识别: 认不出来时留空",
          info["model"] == "" and info["os_version"] == "", repr(info))
    check("型号识别: 空输出不报错", extract_device_info("")["model"] == "")


def test_command_table_is_local():
    """自动补全的命令表由程序自己维护 —— 不再敲 ? 去问设备

    敲 ? 会让探测内容留在设备那一行上（翻页的空格更是直接插进用户正在敲的
    命令里），所以命令表完全改成程序侧来源：内置厂商表 + 资料库整理出的命令。
    """
    import shutil
    import tempfile
    from core.session import SessionInfo

    settings, snapshot = _settings_snapshot()
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_cmdtable_")
    try:
        settings.set("restore_sessions", False)
        settings.set("knowledge_dir", kb_dir)
        window = MainWindow()
        window.show()
        QApplication.setActiveWindow(window)
        window.session_manager.refresh_knowledge()
        _APP.processEvents()

        manager = window.session_manager
        session = SessionInfo(1, "Console", "Serial")
        session.connector = DummyConnector(simulate_prompt=False)
        session.config = {"host": "COM5", "protocol": "Serial"}
        manager._sessions[1] = session
        manager._next_id = 2
        window.on_session_created(1, "Console")
        _APP.processEvents()

        # 型号探测照常（只发 show version），但绝不能再往设备敲 ? 取命令表
        window.probe_device_version(1)
        _APP.processEvents()
        sent = [str(c) for c in session.connector.sent]
        check("命令表: 不再向设备敲 ? 问命令表",
              not any("?" in c for c in sent), repr(sent))
        check("命令表: 也不动设备的分屏设置",
              not any("length" in c.lower() for c in sent), repr(sent))
        check("型号探测: 照常发 show version",
              any("show version" in c for c in sent), repr(sent))

        # 候选来自程序自己的表：内置厂商命令 + 资料库整理出的命令
        terminal = window.tabs[1].terminal
        terminal.set_vendor("ruijie")
        check("命令表: 内置厂商命令可用",
              "show version" in terminal.dictionary.all_commands(),
              repr(terminal.dictionary.all_commands()[:5]))

        kb = manager.knowledge_base
        kb.save_commands("某型号 常用命令", ["show interfaces status"],
                         category="命令速查")
        window.load_kb_commands_into_sessions()
        check("命令表: 资料库整理出的命令进了建议",
              "show interfaces status" in terminal.dictionary.all_commands(),
              repr(terminal.dictionary.all_commands()[-5:]))
        window.close()
    finally:
        _settings_restore(settings, snapshot)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_document_digest():
    """导入文档后整理出「技术解释 + 命令」，并让命令进入自动提示"""
    import shutil
    import tempfile
    from core.knowledge import find_command_candidates, read_text_file
    from core.ai_agent import AIAgent
    from core.session import SessionInfo
    from ui import main_window as mw_module

    # 粗筛候选：只留"看起像命令"的行
    text = ("本文介绍接口配置。\n"
            "display interface brief\n"
            "interface GigabitEthernet0/1\n"
            "  该命令用于查看接口状态。\n"
            "The device supports the following features\n"
            "vlan 10, 20\n"
            "show running-config\n")
    candidates = find_command_candidates(text)
    check("文档整理: 粗筛出命令行",
          "display interface brief" in candidates, repr(candidates))
    check("文档整理: 丢掉中文说明行",
          all("该命令" not in c for c in candidates), repr(candidates))
    check("文档整理: 丢掉带标点的行",
          "vlan 10, 20" not in candidates, repr(candidates))

    explanation, commands = AIAgent.split_digest(
        "## 技术解释\n接口查看命令分两类。\n\n## 命令\n```\ndisplay interface brief\n```\n")
    check("文档整理: 拆出技术解释", "两类" in explanation, repr(explanation))
    check("文档整理: 拆出命令清单",
          commands == ["display interface brief"], repr(commands))

    # 厚手册要跨章节均匀采样：只取前 N 条会全是前几章的命令
    from core.knowledge import even_sample
    items = [f"cmd{index}" for index in range(1000)]
    picked = even_sample(items, 100)
    check("文档整理: 均匀采样覆盖全文",
          len(picked) == 100 and picked[0] == "cmd0" and picked[-1] == "cmd990",
          repr(picked[:3] + picked[-3:]))
    check("文档整理: 候选不够时原样返回",
          even_sample(["a", "b"], 5) == ["a", "b"])

    settings, snapshot = _settings_snapshot()
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_digest_")
    try:
        settings.set("restore_sessions", False)
        settings.set("knowledge_dir", kb_dir)
        settings.set("api_key", "sk-test")
        window = MainWindow()
        window.show()
        QApplication.setActiveWindow(window)
        window.session_manager.refresh_knowledge()
        _APP.processEvents()

        manual = os.path.join(kb_dir, "H3C接口配置手册.md")
        with open(manual, "w", encoding="utf-8") as f:
            f.write("# 接口配置\n" + "display interface brief\n" * 3 + "说明文字" * 60)

        class _FakeAgent:
            """替身：不联网，直接给一份整理结果"""
            def __init__(self, **kwargs):
                pass

            def digest_document(self, name, text, candidates=None):
                return ("接口状态用 display 系命令查看，注意先看 UP/DOWN。",
                        ["display interface brief", "display version"])

        original = mw_module.AIAgent
        mw_module.AIAgent = _FakeAgent
        try:
            window.start_document_digest([manual])
            deadline = time.time() + 5
            while time.time() < deadline and not window.note_paths("H3C接口配置手册 摘要"):
                time.sleep(0.05)
                _APP.processEvents()
        finally:
            mw_module.AIAgent = original

        paths = window.note_paths("H3C接口配置手册 摘要")
        check("文档整理: 生成摘要笔记", bool(paths), repr(os.listdir(kb_dir)))
        note = read_text_file(paths[0]) if paths else ""
        check("文档整理: 笔记含技术解释", "UP/DOWN" in note, repr(note[:120]))
        check("文档整理: 笔记含命令清单", "display version" in note, repr(note[-200:]))

        # 整理出的命令要能进终端提示（连上设备时自动装载）
        manager = window.session_manager
        session = SessionInfo(1, "Console", "Serial")
        session.connector = DummyConnector(simulate_prompt=False)
        session.config = {"host": "COM5", "protocol": "Serial"}
        manager._sessions[1] = session
        manager._next_id = 2
        window.load_kb_commands_into_sessions()
        window.on_session_created(1, "Console")
        _APP.processEvents()
        loaded = window.tabs[1].terminal.dictionary.all_commands()
        check("文档整理: 整理出的命令进了自动提示",
              "display interface brief" in loaded, repr(loaded[-5:]))

        # 没配 AI 也要能整理：命令清单由程序自己从正文里抽，技术解释留空
        settings.set("api_key", "")
        settings.set("provider_keys", {})
        manual2 = os.path.join(kb_dir, "锐捷S2928G-E手册.md")
        with open(manual2, "w", encoding="utf-8") as f:
            f.write("# 常用查看命令\n" + "show cpu\n" * 3 + "show version\n"
                    + "说明文字" * 60)
        window.start_document_digest([manual2])
        deadline = time.time() + 5
        while time.time() < deadline and not window.note_paths("锐捷S2928G-E手册 摘要"):
            time.sleep(0.05)
            _APP.processEvents()
        local_paths = window.note_paths("锐捷S2928G-E手册 摘要")
        check("文档整理: 没配 AI 也能整理（命令由程序抽）", bool(local_paths),
              repr(os.listdir(kb_dir)))
        local_note = read_text_file(local_paths[0]) if local_paths else ""
        check("文档整理: 本地抽出的命令进了笔记", "show cpu" in local_note,
              repr(local_note[-200:]))
        window.close()
    finally:
        _settings_restore(settings, snapshot)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_command_lines_parsing():
    """从模型回复里挑命令行：去掉编号/项目符号/围栏"""
    from core.ai_agent import AIAgent

    agent = AIAgent(api_key="x", provider="deepseek")

    reply = ("1. show version\n"
             "- show interfaces status\n"
             "```\n"
             "show running-config\n"          # 围栏内的也算命令
             "```\n"
             "说明：以上是常用命令\n"          # 中文说明行要丢掉
             "show version\n")                 # 重复的要丢掉
    commands = agent.parse_command_lines(reply)
    check("命令清单: 去掉编号与项目符号",
          commands[:3] == ["show version", "show interfaces status",
                           "show running-config"], repr(commands))
    check("命令清单: 丢掉中文说明行",
          all("说明" not in c for c in commands), repr(commands))
    check("命令清单: 去重", commands.count("show version") == 1, repr(commands))
    check("命令清单: 一条都没有时返回空",
          agent.parse_command_lines("") == [])
    # 围栏语言标记不能当命令（```text 被剥成 text 就是这个坑）
    check("命令清单: 围栏语言标记不当命令",
          agent.parse_command_lines("```text\nshow version\n```") == ["show version"],
          repr(agent.parse_command_lines("```text\nshow version\n```")))


def test_device_version_probe():
    """连上后自动探测型号/版本；命令建议只来自程序内的表（不再让 AI 猜）"""
    import shutil
    import tempfile
    from core.session import SessionInfo

    VERSION_OUTPUT = (
        "Console#show version\n"
        "Ruijie Full Internetworking Operating System (RGOS)\n"
        "Version 11.4(1)B70P1\n"
        "System description : Ruijie RG-S2910-24GT4XS-E\n")

    settings, snapshot = _settings_snapshot()
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_devcmd_")
    try:
        settings.set("restore_sessions", False)
        settings.set("knowledge_dir", kb_dir)
        window = MainWindow()
        window.show()
        QApplication.setActiveWindow(window)
        window.session_manager.refresh_knowledge()
        _APP.processEvents()

        manager = window.session_manager
        session = SessionInfo(1, "Console", "Serial")
        session.connector = DummyConnector(simulate_prompt=False)
        session.config = {"host": "COM5", "protocol": "Serial"}
        manager._sessions[1] = session
        manager._next_id = 2
        window.on_session_created(1, "Console")
        _APP.processEvents()

        window.probe_device_version(1)
        check("型号探测: 已发出 show version",
              session.connector.sent[-1] == "show version",
              repr(session.connector.sent[-2:]))

        window.on_version_output(1, VERSION_OUTPUT)
        check("型号探测: 型号已记到会话上",
              session.model == "RG-S2910-24GT4XS-E" and session.os_version == "11.4(1)B70P1",
              repr((session.model, session.os_version)))

        # 建议里只有程序内来源：内置厂商表 / 资料库整理出的手册命令 /
        # 本会话执行过的命令 —— 不再有"AI 按型号猜"的那一批
        commands = window.tabs[1].terminal.dictionary.all_commands()
        check("型号探测: 建议来自内置厂商表",
              "show interfaces status" in commands, repr(commands))
        check("型号探测: 不再塞入 AI 猜的命令",
              not any("fan" in c or "temperature" in c for c in commands),
              repr(commands))

        # 认不出来时最多换一次语法 —— 不能一直往设备发命令（曾因此刷屏）
        window.on_version_output(1, "% Unknown command.\nRuijie>")
        first = len(session.connector.sent)
        check("型号探测: 认不出时换一种语法再试",
              session.connector.sent[-1] == "display version",
              repr(session.connector.sent[-2:]))
        window.on_version_output(1, "% Unknown command.\nRuijie>")
        window.on_version_output(1, "% Unknown command.\nRuijie>")
        check("型号探测: 两种语法都试过就不再发",
              session.connector.sent.count("display version") == 1,
              repr(session.connector.sent))
        window.close()
    finally:
        _settings_restore(settings, snapshot)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_ai_agent():
    agent = AIAgent(api_key="test-key")

    check("危险命令识别: reboot", agent.is_dangerous("reboot"))
    check("危险命令识别: undo all", agent.is_dangerous("undo all"))
    check("危险命令识别: 普通命令", not agent.is_dangerous("display interface brief"))

    reply = "display version\nreboot\n# 注释行\nDONE\n"
    commands, blocked = agent.filter_commands(reply)
    check("命令拆分", commands == ["display version"], repr(commands))
    check("危险命令拦截", blocked == ["reboot"], repr(blocked))

    numbered = "1. system-view\n2) interface GigabitEthernet0/0/1\n- quit"
    commands, _ = agent.filter_commands(numbered)
    check("去除编号/项目符号", commands == ["system-view", "interface GigabitEthernet0/0/1", "quit"],
          repr(commands))

    agent.set_task("测试任务")
    agent.set_vendor("h3c")
    check("上下文初始为空", agent.get_context_length() == 0)
    agent.reset_context()
    check("上下文清空", agent.get_context_length() == 0)


def test_settings():
    """使用临时配置文件验证读写，避免污染用户真实配置"""
    settings = Settings()
    original_data = dict(settings.data)
    original_file = settings.config_file
    tmp_file = os.path.join(BASE_DIR, "tests", "_tmp_settings.json")
    try:
        settings.config_file = tmp_file
        settings.set("font_size", 12)
        # 先清空最近连接再测"追加"行为。
        # 注意：Settings() 会加载**真实**配置文件，用户只要用过软件，
        # recent_connections 里就会有记录；不清空的话下面 len==1 的断言
        # 会随真实数据漂移（这条测试踩过）。
        settings.set("recent_connections", [])
        settings.set_last_connection({"protocol": "SSH", "host": "10.0.0.1", "port": 22,
                                      "password": "secret", "name": "sw1"})
        settings.save()

        check("配置保存", os.path.exists(tmp_file))

        # 密码不应写入最近连接列表
        recents = settings.get_recent_connections()
        check("最近连接不含密码", all("password" not in r for r in recents), repr(recents))
        check("最近连接已记录", len(recents) == 1 and recents[0]["host"] == "10.0.0.1")
        check("配置项读取", settings.get("font_size") == 12)
    finally:
        if os.path.exists(tmp_file):
            os.remove(tmp_file)
        settings.config_file = original_file
        settings._data = original_data


def test_presets_and_plugins():
    check("预设连接非空", len(PRESET_CONNECTIONS) > 0)
    check("厂商命令库: h3c", "查看版本" in get_vendor_commands("h3c"))
    check("未知厂商回退", get_vendor_commands("unknown") == get_vendor_commands("h3c"))

    pm = PluginManager(os.path.join(BASE_DIR, "plugins"))
    plugins = pm.load_all()
    check("插件目录扫描不报错", isinstance(plugins, list))
    check("空钩子广播返回True", pm.dispatch("on_output", None, "text") is True)


def test_telnet_fallback():
    check("telnetlib 探测", isinstance(telnet_mod.HAS_TELNETLIB, bool),
          str(telnet_mod.HAS_TELNETLIB))
    check("MiniTelnet 回退实现存在", hasattr(telnet_mod, "_MiniTelnet"))
    check("连接器可正常导入", telnet_mod.TelnetConnector is not None)


# ------------------------------------------------------------------ v0.2 功能
def test_diff():
    old = ("sysname SW1\n"
           "vlan 10\n"
           " description USERS\n"
           "interface GigabitEthernet1/0/1\n"
           " port link-type access\n"
           "<H3C>\n")
    new = ("sysname SW1\n"
           "vlan 10\n"
           " description OFFICE\n"
           "vlan 20\n"
           " description GUEST\n"
           "interface GigabitEthernet1/0/1\n"
           " port link-type access\n"
           "<H3C>\n")

    added, removed = summarize(old, new)
    check("配置比对: 统计差异", added == 3 and removed == 1, f"+{added}/-{removed}")

    diff = diff_lines(old, new, context=1)
    tags = [t for t, _ in diff]
    check("配置比对: 含新增/删除标记", '+' in tags and '-' in tags, repr(tags))
    check("配置比对: 提示符行被剔除", all('H3C' not in line for _t, line in diff))
    check("配置比对: 相同文本无差异", not has_changes("a\nb", "a\nb"))

    unified = to_unified("a\nb", "a\nc", "旧", "新")
    check("配置比对: 生成unified diff",
          unified.startswith('---') and '+c' in unified, repr(unified))


def test_templates():
    check("模板库非空", len(COMMAND_TEMPLATES) > 0)

    commands = COMMAND_TEMPLATES["创建VLAN并划分端口"]["commands"]
    names = extract_variables(commands)
    check("模板: 变量提取", names == ["vlan_id", "vlan_name", "interface"], repr(names))

    rendered = render_template(commands, {
        "vlan_id": "20", "vlan_name": "OFFICE", "interface": "GE1/0/2"
    })
    check("模板: 渲染变量",
          "vlan 20" in rendered and "name OFFICE" in rendered and "interface GE1/0/2" in rendered,
          repr(rendered))

    partial = render_template(["vlan {vlan_id}", "name {vlan_name}"], {"vlan_id": "30"})
    check("模板: 未填变量保留占位", partial[1] == "name {vlan_name}", repr(partial))

    check("模板: 按名称渲染", template_commands("备份当前配置") == ["display current-configuration"])
    check("模板: 厂商保存命令",
          save_config_commands("cisco") == ["write memory"]
          and save_config_commands("h3c") == ["save force"])


def test_logger():
    import shutil
    tmp_dir = os.path.join(BASE_DIR, "tests", "_tmp_logs")
    try:
        logger = SessionLogger("测试/设备", "10.0.0.1", log_dir=tmp_dir)
        check("日志: 文件创建", logger.open() and os.path.exists(logger.path), logger.path)
        logger.write("display version\n")
        logger.mark("用户下发: save")
        logger.flush()
        logger.close()

        with open(logger.path, 'r', encoding='utf-8') as f:
            content = f.read()
        check("日志: 内容写入", "display version" in content and "用户下发: save" in content)
        check("日志: 文件名已转义",
              "/" not in os.path.basename(logger.path), os.path.basename(logger.path))
        check("日志: 目录扫描", len(list_logs(tmp_dir)) == 1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_batch():
    connector = DummyConnector()
    runner = BatchRunner(connector, ["display version", "reboot",
                                     "display interface brief", "   "],
                         delay_ms=0, block_dangerous=True)
    events = {"progress": [], "blocked": [], "finished": []}
    runner.progress.connect(lambda i, t, c: events["progress"].append((i, t, c)))
    runner.blocked.connect(lambda i, c: events["blocked"].append((i, c)))
    runner.finished.connect(lambda ok, msg: events["finished"].append((ok, msg)))

    runner._run()  # 同步执行，便于断言

    check("批量: 下发条数", len(events["progress"]) == 2, repr(events["progress"]))
    check("批量: 危险命令被拦截",
          len(events["blocked"]) == 1 and events["blocked"][0][1] == "reboot",
          repr(events["blocked"]))
    check("批量: 结束回调成功", bool(events["finished"]) and events["finished"][0][0] is True,
          repr(events["finished"]))
    check("批量: 空行被忽略",
          connector.sent == ["display version", "display interface brief"],
          repr(connector.sent))

    offline = DummyConnector()
    offline._connected = False
    runner2 = BatchRunner(offline, ["display version"], delay_ms=0)
    check("批量: 断开时拒绝执行", runner2.start() is False)


def test_snapshot_and_capture():
    session = SessionInfo(1, "测试设备", "SSH")
    session.connector = DummyConnector()

    session.start_capture()
    session.record_output("display current-configuration\n")
    session.record_output("sysname SW1\n")
    expected = len("display current-configuration\n") + len("sysname SW1\n")
    check("采集: 累计长度", session.capture_size() == expected, str(session.capture_size()))
    captured = session.stop_capture()
    check("采集: 取回内容", "sysname SW1" in captured)
    check("采集: 结束后不再累积", session.stop_capture() == "")

    session.add_snapshot("第一次", "vlan 10")
    session.add_snapshot("第二次", "vlan 20")
    check("快照: 最近一份", session.last_snapshot()[0] == "第二次")
    check("快照: 上一份", session.previous_snapshot()[0] == "第一次")

    for i in range(SessionInfo.MAX_SNAPSHOTS + 5):
        session.add_snapshot(f"第{i}次", "x")
    check("快照: 数量上限",
          len(session.snapshots) == SessionInfo.MAX_SNAPSHOTS, str(len(session.snapshots)))


def test_new_settings():
    settings = Settings()
    original = dict(settings.data)
    try:
        check("配置: 新增默认项",
              settings.auto_log is True and settings.batch_delay_ms == 500)

        settings.set("batch_delay_ms", 1200)
        check("配置: 批量间隔读取", settings.batch_delay_ms == 1200)

        settings.set("system_prompt", "  自定义提示词  ")
        check("配置: 提示词去空格", settings.system_prompt == "自定义提示词")

        encoded = Settings.encode_password("P@ssw0rd!")
        check("配置: 密码混淆",
              encoded.startswith("b64:") and "P@ssw0rd" not in encoded, encoded)
        check("配置: 密码还原", Settings.decode_password(encoded) == "P@ssw0rd!")

        settings.set_last_sessions([{"name": "sw1", "protocol": "SSH"}])
        check("配置: 会话列表存取", settings.get_last_sessions()[0]["name"] == "sw1")
    finally:
        settings._data = original


def test_ai_prompt_override():
    agent = AIAgent(api_key="test-key")
    check("提示词: 默认内置", "网络工程师" in agent.system_prompt)

    agent.set_system_prompt("只输出命令")
    check("提示词: 自定义生效", agent.system_prompt == "只输出命令")

    agent.set_system_prompt("")
    check("提示词: 清空恢复内置",
          agent.system_prompt == AIAgent.default_system_prompt())


def test_v02_dialogs():
    session = SessionInfo(1, "测试设备", "SSH")
    session.connector = DummyConnector()
    session.add_snapshot("快照A", "vlan 10\n description USERS")
    session.record_output("vlan 10\n description OFFICE")

    dialog = DiffDialog(None, session)
    dialog.run_diff()
    summary = dialog.summary_label.text()
    check("比对对话框: 生成结果", "新增" in summary or "一致" in summary, summary)
    html_text = dialog.diff_view.toHtml()
    from ui.theme import P as _THEME_P
    check("比对对话框: 差异着色",
          _THEME_P["ok"].lstrip("#").lower() in html_text.lower()
          or _THEME_P["err"].lstrip("#").lower() in html_text.lower())
    dialog.close()

    tpl = TemplateDialog(None, default_delay=300, session_name="测试设备", vendor="h3c")
    check("模板对话框: 模板已载入", tpl.tpl_list.count() == len(COMMAND_TEMPLATES),
          str(tpl.tpl_list.count()))
    check("模板对话框: 变量表单已生成", "vlan_id" in tpl.var_edits, repr(list(tpl.var_edits)))
    check("模板对话框: 预览非空", len(tpl.get_commands()) > 0)
    tpl.var_edits["vlan_id"].setText("88")
    check("模板对话框: 变量联动预览",
          "vlan 88" in tpl.preview.toPlainText(), tpl.preview.toPlainText())
    tpl.close()

    prompt_dialog = PromptDialog(None, "")
    check("提示词对话框: 默认填充", "网络工程师" in prompt_dialog.editor.toPlainText())
    check("提示词对话框: 默认值返回空", prompt_dialog.get_prompt() == "")
    prompt_dialog.editor.setPlainText("自定义规则")
    check("提示词对话框: 自定义返回", prompt_dialog.get_prompt() == "自定义规则")
    prompt_dialog.close()


# ------------------------------------------------------------------ 联网检索
BING_FIXTURE = """
<ol id="b_results">
<li class="b_algo" data-id><h2><a href="https://example.com/a">H3C 命令教程A</a></h2>
<p class="b_lineclamp2">这是摘要A</p></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=xx&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9i&amp;ntb=1">标题B</a></h2>
<div class="b_caption"><p>这是摘要B</p></div></li>
</ol>
"""

BAIDU_FIXTURE = """
<div class="result c-container"><h3 class="t"><a href="https://www.baidu.com/link?url=abc">交换机命令甲</a></h3>
<div class="c-abstract">摘要甲内容</div></div>
<div class="result c-container"><h3 class="t"><a href="https://www.baidu.com/link?url=def">交换机命令乙</a></h3>
<div class="c-abstract">摘要乙内容</div></div>
"""


def test_web_search_parsing():
    """离线校验各检索后端的 HTML 解析（不依赖网络）"""
    bing = ws.BingBackend.parse(BING_FIXTURE)
    check("检索: 必应解析条数", len(bing) == 2, str(len(bing)))
    check("检索: 必应标题", bing and bing[0].title == "H3C 命令教程A", repr(bing[0].title if bing else None))
    check("检索: 必应摘要", bing and bing[0].snippet == "这是摘要A")
    # 跳转链接要先反转义 &amp; 再解 base64
    check("检索: 必应跳转链接还原",
          len(bing) > 1 and bing[1].url == "https://example.com/b",
          repr(bing[1].url if len(bing) > 1 else None))
    check("检索: 必应保留原始链接",
          len(bing) > 1 and "bing.com/ck/a" in bing[1].raw_url)

    baidu = ws.BaiduBackend.parse(BAIDU_FIXTURE)
    check("检索: 百度解析条数", len(baidu) == 2, str(len(baidu)))
    check("检索: 百度标题", baidu and baidu[0].title == "交换机命令甲")
    check("检索: 百度摘要", baidu and baidu[0].snippet == "摘要甲内容", repr(baidu[0].snippet if baidu else None))

    check("检索: 跳转链接识别", ws.is_redirect_url("https://www.baidu.com/link?url=x"))
    check("检索: 非跳转链接识别", not ws.is_redirect_url("https://www.h3c.com/cn/"))

    text = ws.html_to_text("<html><head><style>x{}</style></head><body><p>第一行</p>"
                           "<script>bad()</script><div>第二行</div></body></html>")
    check("检索: HTML转文本", "第一行" in text and "第二行" in text and "bad()" not in text, repr(text))
    check("检索: 去标签反转义", ws.strip_tags("<b>A&amp;B</b>") == "A&B")


def test_web_search_client():
    client = WebSearchClient(backend="searxng", searxng_url="")
    check("检索: 无可用后端时返回空", client.search("test") == [])
    check("检索: 无后端时有错误说明", bool(client.last_error), client.last_error)

    results = ws.BaiduBackend.parse(BAIDU_FIXTURE)
    formatted = WebSearchClient.format_results(results)
    check("检索: 结果格式化", "交换机命令甲" in formatted and "链接:" in formatted)

    context = WebSearchClient.build_context({
        "query": "测试", "results": results,
        "pages": [("https://x.com", "标题X", "正文内容X")], "error": ""
    })
    check("检索: 上下文拼装",
          "联网检索关键词: 测试" in context and "正文内容X" in context)


def test_ai_search_flow():
    """校验 AI 的 SEARCH 协议、资料回填与联网问答（用假模型客户端，不联网）"""
    from core.ai_agent import SEARCH_INSTRUCTION

    agent = AIAgent(api_key="test-key")
    check("AI检索: 默认未启用", agent.search_enabled is False)
    check("AI检索: 未启用时提示词不含协议", SEARCH_INSTRUCTION.strip()[:10] not in agent.system_prompt)

    agent.set_search_client(WebSearchClient(backend="auto"))
    check("AI检索: 注入后启用", agent.search_enabled is True)
    check("AI检索: 提示词追加协议", "SEARCH:" in agent.system_prompt)

    check("AI检索: 解析检索请求",
          agent.parse_search_request("SEARCH: H3C 端口镜像 命令") == "H3C 端口镜像 命令")
    check("AI检索: 解析中文冒号",
          agent.parse_search_request("SEARCH：Cisco console 乱码") == "Cisco console 乱码")
    check("AI检索: 普通命令不算检索",
          agent.parse_search_request("display interface brief") is None)

    # 用桩客户端替代真实检索
    class _StubSearch:
        max_results = 3

        def search_and_read(self, query, top_n=2, max_chars=4000, max_results=None):
            return {
                "query": query,
                "results": [ws.SearchResult("H3C 端口镜像配置", "https://example.com/mirror",
                                            "使用 mirroring-group 命令", "stub")],
                "pages": [("https://example.com/mirror", "H3C 端口镜像配置",
                           "mirroring-group 1 local\nmirroring-group 1 mirroring-port ...")],
                "error": "",
            }

        @staticmethod
        def build_context(data, max_chars=6000):
            return WebSearchClient.build_context(data, max_chars)

    agent.set_search_client(_StubSearch())
    context = agent.web_context("H3C 端口镜像")
    check("AI检索: 资料回填包含命令", "mirroring-group" in context, repr(context[:120]))
    check("AI检索: 检索次数累加", agent.search_count == 1)

    class _EmptySearch:
        max_results = 3

        def search_and_read(self, query, top_n=2, max_chars=4000, max_results=None):
            return {"query": query, "results": [], "pages": [], "error": "超时"}

    agent.set_search_client(_EmptySearch())
    empty_context = agent.web_context("查不到的东西")
    check("AI检索: 无结果时明确告知", "没有返回结果" in empty_context, repr(empty_context[:80]))

    # 联网问答：用假模型客户端验证整条链路
    class _FakeLLM:
        def chat(self, system, messages, max_tokens=1024, temperature=0.3):
            return "结论: 用 mirroring-group 配置端口镜像 [1]"

        def describe(self):
            return "FakeLLM"

    agent.set_search_client(_StubSearch())
    agent.llm = _FakeLLM()
    captured = {}
    agent.research_ready.connect(lambda answer, sources: captured.update(
        {"answer": answer, "sources": sources}))
    answer = agent.research("H3C 怎么配置端口镜像", "display current-configuration")
    check("AI检索: 联网问答返回内容", "mirroring-group" in answer, repr(answer))
    check("AI检索: 发出结果信号", captured.get("answer") == answer)
    check("AI检索: 附带来源列表",
          captured.get("sources") and captured["sources"][0]["url"] == "https://example.com/mirror")

    agent.set_search_client(None)
    check("AI检索: 关闭后不联网", agent.research("任意问题") == "")


# ------------------------------------------------------------------ 串口 Console
class _FakeSerial:
    """模拟 pyserial 的串口对象，用于离线验证发送逻辑"""

    def __init__(self):
        self.is_open = True
        self.baudrate = 9600
        self.written = b""
        self.breaks = []
        self.dtr = None
        self.rts = None

    @property
    def in_waiting(self):
        return 0

    def write(self, data):
        self.written += data
        return len(data)

    def flush(self):
        pass

    def send_break(self, duration=0.25):
        self.breaks.append(duration)

    def close(self):
        self.is_open = False


def test_serial_console():
    connector = SerialConnector()
    check("串口: 声明支持串口控制", connector.supports_serial_control() is True)
    check("串口: Console 默认 CR 结尾", connector.line_ending == '\r',
          repr(connector.line_ending))

    # 端口枚举不应抛异常
    ports = SerialConnector.list_ports_detailed()
    check("串口: 端口枚举可用", isinstance(ports, list))

    # 打开不存在的端口应返回 False 并给出可读错误
    errors = []
    connector.error_occurred.connect(errors.append)
    opened = connector.connect(port="COM_DOES_NOT_EXIST_999", baudrate=9600,
                               auto_reconnect=False)
    check("串口: 打开不存在的端口返回False", opened is False)
    check("串口: 给出可读错误", bool(errors), repr(errors))

    # 用假串口验证发送与行结束符
    connector = SerialConnector()
    connector.ser = _FakeSerial()
    connector._connected = True
    connector._params = {'port': 'COM3', 'baudrate': 9600, 'bytesize': 8,
                         'parity': 'N', 'stopbits': 1, 'char_delay_ms': 0}

    connector.send("display version")
    check("串口: 命令以CR结尾", connector.ser.written == b"display version\r",
          repr(connector.ser.written))

    connector.send_raw("\x03")
    check("串口: 原始数据不追加结尾", connector.ser.written.endswith(b"\x03"))

    connector.set_line_ending("\r\n")
    connector.ser.written = b""
    connector.send("save")
    check("串口: 切换行结束符", connector.ser.written == b"save\r\n",
          repr(connector.ser.written))

    connector.set_char_delay(2)
    check("串口: 字符间隔生效", connector._char_delay == 0.002, str(connector._char_delay))

    check("串口: 发送Break", connector.send_break(0.3) is True and connector.ser.breaks == [0.3])
    check("串口: DTR控制", connector.set_dtr(True) is True and connector.ser.dtr is True)
    check("串口: RTS控制", connector.set_rts(False) is True and connector.ser.rts is False)
    check("串口: 运行时改波特率",
          connector.set_baudrate(115200) is True and connector.ser.baudrate == 115200)

    params = connector.connection_params()
    check("串口: 参数可回读",
          params.get('port') == 'COM3' and params.get('baudrate') == 115200, repr(params))
    check("串口: info 含端口与波特率", "COM3" in connector.info(), connector.info())

    # 常见报错翻译
    occupied = SerialConnector._explain_error(Exception("Access is denied."))
    check("串口: 占用报错翻译", "被占用" in occupied, occupied)

    # Console 预设与选项
    check("串口: Console 预设非空", len(CONSOLE_PRESETS) > 0)
    check("串口: 波特率表含115200", 115200 in BAUDRATES)
    check("串口: 校验选项完整", len(PARITY_OPTIONS) == 5)
    check("串口: 流控选项完整", len(FLOW_OPTIONS) == 4)
    check("串口: 行结束符含CR", any(v == "\r" for _l, v in LINE_ENDING_OPTIONS))


def test_ai_panel_tabs():
    """校验 AI 高级面板：只留 AI 设置入口与资料检索两个页签"""
    from ui.ai_panel import AIPanel
    panel = AIPanel()
    check("AI面板: 两个页签", panel.tabs.count() == 2, str(panel.tabs.count()))
    check("AI面板: 页签标题",
          [panel.tabs.tabText(i) for i in range(2)] == ["AI 助手", "资料检索"])
    check("AI面板: 有 AI 设置入口",
          hasattr(panel, "btn_ai_settings") and hasattr(panel, "ai_config_label"))
    check("AI面板: 待执行队列已移除", not hasattr(panel, "queue_list"))

    panel.show_research_result("回答内容", [
        {"title": "标题1", "url": "https://example.com/1", "snippet": "s"}
    ])
    check("AI面板: 联网结果显示", "回答内容" in panel.research_output.toPlainText())
    check("AI面板: 来源列表", panel.source_list.count() == 1)
    panel.close()


# ------------------------------------------------------------------ 本地资料库
def _make_minimal_pdf(path: str, lines):
    """手工拼一个含文本的最小 PDF，用来验证 pypdf 抽取路径"""
    import zipfile  # noqa: F401  (保持与其它样例一致的导入位置)

    body = b"BT /F1 12 Tf 72 720 Td "
    for index, line in enumerate(lines):
        if index:
            body += b"0 -16 Td "
        body += b"(" + line.encode("ascii", "replace") + b") Tj "
    body += b"ET"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref_position = len(pdf)
    pdf += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        pdf += ("%010d 00000 n \n" % offset).encode()
    pdf += (b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
            b"startxref\n" + str(xref_position).encode() + b"\n%%EOF\n")

    with open(path, "wb") as f:
        f.write(bytes(pdf))


def _make_sample_docs(src_dir: str):
    """造几种格式的样例资料：md / docx / xlsx / pdf / zip / 不支持的老格式"""
    import zipfile

    with open(os.path.join(src_dir, "H3C_S5560_命令手册.md"), "w", encoding="utf-8") as f:
        f.write("# H3C S5560 命令手册\n\n## 端口镜像\n"
                "mirroring-group 1 local\n"
                "mirroring-group 1 mirroring-port GigabitEthernet1/0/1 both\n"
                "mirroring-group 1 monitor-port GigabitEthernet1/0/24\n\n"
                "## VLAN 配置\nvlan 10\n port link-type access\n")

    with zipfile.ZipFile(os.path.join(src_dir, "华为S5700巡检.docx"), 'w') as z:
        z.writestr('word/document.xml',
                   '<w:document><w:body><w:p><w:r><w:t>华为 S5700 巡检命令</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>display device</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>display cpu-usage</w:t></w:r></w:p></w:body></w:document>')

    with zipfile.ZipFile(os.path.join(src_dir, "接口对照表.xlsx"), 'w') as z:
        z.writestr('xl/sharedStrings.xml',
                   '<sst><si><t>接口</t></si><si><t>对端设备</t></si>'
                   '<si><t>GE1/0/1</t></si><si><t>核心交换机</t></si></sst>')
        z.writestr('xl/worksheets/sheet1.xml',
                   '<worksheet><sheetData>'
                   '<row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
                   '<row><c t="s"><v>2</v></c><c t="s"><v>3</v></c></row>'
                   '</sheetData></worksheet>')

    # PDF 命令手册（手工构造，用于验证 pypdf 抽取）
    _make_minimal_pdf(os.path.join(src_dir, "Cisco_console手册.pdf"),
                      ["console 9600 8N1 troubleshooting",
                       "show line console 0",
                       "speed 9600"])

    # 打成 zip，验证压缩包导入
    with zipfile.ZipFile(os.path.join(src_dir, "手册合集.zip"), 'w') as z:
        z.writestr('Cisco巡检.txt', 'show version\nshow ip interface brief\n')

    with open(os.path.join(src_dir, "老格式.doc"), "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0 old binary doc")


def test_knowledge_base():
    """本地资料库：导入 / 抽取 / 建索引 / 检索 / 删除"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase

    src_dir = tempfile.mkdtemp(prefix="aiterminal_kb_src_")
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_kb_root_")
    try:
        _make_sample_docs(src_dir)
        kb = KnowledgeBase(root_dir=kb_dir)

        result = kb.import_paths([src_dir])
        check("资料库: 导入新增", len(result["added"]) >= 5, repr(result["added"]))
        check("资料库: 不支持格式被跳过并说明原因",
              any("老格式.doc" in p for p, _m in result["errors"]),
              repr(result["errors"]))
        check("资料库: zip 内文件被解出",
              any("Cisco巡检.txt" in name for name in result["added"]),
              repr(result["added"]))

        # 导入一段剪贴板文本
        saved = kb.import_text("手记.md", "console 乱码 通常是波特率不匹配\n")
        check("资料库: 导入剪贴文本", bool(saved) and os.path.exists(saved), str(saved))

        summary = kb.build_index()
        check("资料库: 建索引文档数", summary["documents"] >= 5, repr(summary))
        check("资料库: 建索引片段数", summary["chunks"] >= 5, repr(summary))
        check("资料库: 无解析失败", not summary["failed"], repr(summary["failed"]))

        # 增量复用
        again = kb.build_index()
        check("资料库: 增量复用已索引文档", again["reused"] == again["documents"],
              repr(again))

        hits = kb.search("端口镜像 mirroring-group", top_k=2)
        check("资料库: 英文命令命中", bool(hits) and "mirroring-group" in hits[0]["text"],
              repr(hits[:1]))
        check("资料库: 返回来源与段号",
              bool(hits) and "命令手册" in hits[0]["source"] and hits[0]["chunk_index"] >= 0)

        cn_hits = kb.search("华为巡检命令", top_k=2)
        check("资料库: 中文检索命中 docx", bool(cn_hits), repr(cn_hits[:1]))

        xlsx_hits = kb.search("GE1/0/1 对端", top_k=2)
        check("资料库: xlsx 表格可检索",
              bool(xlsx_hits) and "核心交换机" in xlsx_hits[0]["text"], repr(xlsx_hits[:1]))

        pdf_hits = kb.search("console troubleshooting speed", top_k=2)
        check("资料库: PDF 手册可检索",
              bool(pdf_hits) and ".pdf" in pdf_hits[0]["source"]
              and "show line console" in pdf_hits[0]["text"], repr(pdf_hits[:1]))

        check("资料库: 无关词不命中", kb.search("zzzz_不存在的内容_zzzz") == [])

        context = kb.search_text("mirroring-group", top_k=2, max_chars=2000)
        check("资料库: 生成给AI的上下文",
              "[本地资料1]" in context and "mirroring-group" in context, repr(context[:80]))

        documents = kb.list_documents()
        check("资料库: 文档列表", len(documents) >= 4, str(len(documents)))
        check("资料库: 列表含状态字段",
              all("error" in d and "chunks" in d for d in documents))

        stats = kb.stats()
        check("资料库: 概况统计",
              stats["documents"] >= 4 and stats["chunks"] >= 4, repr(stats))

        # 删除
        target = documents[0]["name"]
        check("资料库: 删除文档", kb.remove_document(target) is True)
        check("资料库: 删除后数量减少", kb.document_count() == len(documents) - 1)
        check("资料库: 拒绝删除库外文件",
              kb.remove_document(os.path.join(src_dir, "老格式.doc")) is False)
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_ai_local_lookup():
    """AI 优先查本地资料库，再联网"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase

    src_dir = tempfile.mkdtemp(prefix="aiterminal_lk_src_")
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_lk_root_")
    try:
        _make_sample_docs(src_dir)
        kb = KnowledgeBase(root_dir=kb_dir)
        kb.import_paths([src_dir])
        kb.build_index()

        agent = AIAgent(api_key="test-key", knowledge_base=kb)
        check("本地检索: 具备检索能力", agent.lookup_enabled is True)
        check("本地检索: 仅本地也启用协议", "SEARCH:" in agent.system_prompt)
        check("本地检索: 未启用联网", agent.search_enabled is False)

        local = agent.local_context("端口镜像 命令")
        check("本地检索: 命中本地资料", "mirroring-group" in local, repr(local[:100]))
        check("本地检索: 记录命中来源", bool(agent.last_local_hits))

        context = agent.lookup_context("端口镜像 命令")
        check("本地检索: 合并上下文含本地标记", "【本地资料库】" in context, repr(context[:60]))
        check("本地检索: 来源标注为 local",
              agent.last_lookup_sources
              and agent.last_lookup_sources[0]["source_type"] == "local",
              repr(agent.last_lookup_sources[:1]))

        # 只开本地、不联网也能回答问题
        class _FakeLLM:
            def chat(self, system, messages, max_tokens=1024, temperature=0.3):
                return "用 mirroring-group 配置端口镜像 [1]"

            def describe(self):
                return "FakeLLM"

        agent.llm = _FakeLLM()
        captured = {}
        agent.research_ready.connect(lambda answer, sources: captured.update(
            {"answer": answer, "sources": sources}))
        answer = agent.research("H3C 怎么配置端口镜像")
        check("本地检索: 仅本地资料即可作答", "mirroring-group" in answer, repr(answer))
        check("本地检索: 来源列表为本地资料",
              captured.get("sources")
              and captured["sources"][0]["source_type"] == "local",
              repr(captured.get("sources", [])[:1]))

        # 本地查不到、联网也关闭时给出明确提示
        agent.set_knowledge_base(None)
        check("本地检索: 都关闭时不再检索", agent.lookup_enabled is False)
        errors = []
        agent.error_occurred.connect(errors.append)
        check("本地检索: 无能力时返回空", agent.research("任意问题") == "")
        check("本地检索: 给出未启用提示", bool(errors), repr(errors))

        # 开关关闭后不检索
        agent.set_knowledge_base(kb)
        kb.set_enabled(False)
        check("本地检索: 开关关闭后不命中", agent.local_context("端口镜像") == "")
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_experience_memory():
    """经验总结：后台归档 → 索引预热 → 下次提同类需求时快速匹配并注入"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase, EXPERIENCE_CATEGORY

    kb_dir = tempfile.mkdtemp(prefix="aiterminal_exp_root_")
    try:
        kb = KnowledgeBase(root_dir=kb_dir)
        # 模拟后台整理出来的经验
        path = kb.save_note(
            "经验 · 找出 down 的端口",
            "**需求**：找出所有 down 的端口\n"
            "**有效做法**：先 `display interface brief` 再挑 down 的端口详查\n"
            "**踩坑**：直接 `display interface` 输出太长，翻页时容易漏看",
            category=EXPERIENCE_CATEGORY, tags="ruijie, display interface")
        check("经验: 归档成功", bool(path) and os.path.exists(path), str(path))
        # 同库里再放一条别的分类，验证"经验检索"不会捞错
        kb.save_note("命令速查 · 端口镜像", "```\nmirroring-group 1 local\n```",
                     category="命令速查")

        kb.build_index()
        check("经验: 预热返回可检索片段数", kb.warm_up() > 0)

        agent = AIAgent(api_key="test-key", knowledge_base=kb)
        context = agent.experience_context("帮我看看哪些接口是 down 的")
        check("经验: 匹配到历史经验",
              "display interface brief" in context, repr(context[:60]))
        check("经验: 记录命中来源", len(agent.last_experience_hits) == 1,
              repr(agent.last_experience_hits))
        check("经验: 不混入其它分类",
              all(EXPERIENCE_CATEGORY in h["source"]
                  for h in agent.last_experience_hits))

        check("经验: 无关需求不命中",
              agent.experience_context("zzzz_量子纠缠_zzzz") == "")

        # 归档：把一轮交互压成一条经验
        class _FakeLLM:
            def __init__(self):
                self.calls = []
            def chat(self, system, messages, max_tokens=900, temperature=0.4):
                self.calls.append((system, messages))
                return ("**需求**：找出 down 的端口\n"
                        "**有效做法**：`display interface brief`")
            def describe(self):
                return "FakeLLM"

        agent.llm = _FakeLLM()
        note = agent.summarize_experience(
            "找出 down 的端口", "建议先列接口", ["display interface brief"],
            "Ruijie# show interfaces status", device="Console（Serial）", vendor="ruijie")
        check("经验: 生成总结", "display interface brief" in note, repr(note[:60]))
        check("经验: 用归档提示词", "运维经验" in agent.llm.calls[0][0])
        check("经验: 总结带上了设备与输出",
              "Console（Serial）" in agent.llm.calls[0][1][0]["content"]
              and "show interfaces status" in agent.llm.calls[0][1][0]["content"])

        # 模型判断没什么可归档 → 空字符串，调用方静默跳过
        agent.llm.chat = lambda **k: "SKIP"
        check("经验: SKIP 时不落库", agent.summarize_experience("你好") == "")

        # 归档调用失败也不能把对话带崩
        def _boom(**k):
            raise RuntimeError("网络断了")
        agent.llm.chat = _boom
        check("经验: 调用失败静默返回空", agent.summarize_experience("随便") == "")

        # 注入：经验只发给模型这一轮，不写进对话历史（否则每轮都重复烧 token）
        class _FakeLLM2:
            def __init__(self):
                self.last = None
            def chat(self, system, messages, max_tokens=900, temperature=0.4):
                self.last = messages
                return "好的，先列接口状态。"
        agent.llm = _FakeLLM2()
        agent.converse("看看哪些口 down 了",
                       experience="[历史经验1] 经验 · 找出 down 的端口\ndisplay interface brief")
        sent = agent.llm.last[-1]["content"]
        check("经验: 本轮已注入给模型",
              "历史经验" in sent and "display interface brief" in sent, repr(sent[:80]))
        check("经验: 注入不写进对话历史",
              all("历史经验" not in m["content"] for m in agent.context),
              repr(agent.context))
    finally:
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_knowledge_dialog():
    """资料库管理界面冒烟测试"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase
    from ui.knowledge_dialog import KnowledgeDialog, build_file_filter

    src_dir = tempfile.mkdtemp(prefix="aiterminal_kd_src_")
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_kd_root_")
    try:
        _make_sample_docs(src_dir)
        kb = KnowledgeBase(root_dir=kb_dir)
        kb.import_paths([src_dir])
        kb.build_index()

        dialog = KnowledgeDialog(None, kb)
        dialog.wait_loaded()          # 2026-10-17 起构造是异步的（载入在后台线程）
        check("资料库界面: 列表已填充", dialog.table.rowCount() >= 4,
              str(dialog.table.rowCount()))
        check("资料库界面: 统计信息", "篇资料" in dialog.stats_label.text(),
              dialog.stats_label.text())
        check("资料库界面: 文件过滤器含pdf", "*.pdf" in build_file_filter())

        dialog.query_input.setText("mirroring-group")
        dialog.test_search()
        dialog.wait_search_done()                 # 检索已改成后台线程，要等结果落地
        output = dialog.result_view.toPlainText()
        check("资料库界面: 检索测试命中", "命中" in output and "mirroring-group" in output,
              output[:120])

        dialog.query_input.setText("完全不存在的关键词zzz")
        dialog.test_search()
        dialog.wait_search_done()
        check("资料库界面: 检索测试无命中提示",
              "没有命中" in dialog.result_view.toPlainText())
        dialog.close()
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(kb_dir, ignore_errors=True)


# ------------------------------------------------------------------ 知识沉淀
def test_save_note():
    """把 AI 回答 / 命令存回资料库，并能被检索到"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase

    kb_dir = tempfile.mkdtemp(prefix="aiterminal_note_")
    try:
        kb = KnowledgeBase(root_dir=kb_dir)

        path = kb.save_note(
            "H3C 端口镜像配置命令",
            "mirroring-group 1 local\n"
            "mirroring-group 1 mirroring-port GigabitEthernet1/0/1 both",
            category="命令速查",
            tags="端口镜像, mirroring-group, S5560",
            meta={"设备": "核心交换机（SSH 10.0.0.1）", "厂商": "h3c"},
        )
        check("沉淀: 文件已保存", bool(path) and os.path.exists(path), str(path))
        check("沉淀: 归入分类子目录", "命令速查" in (path or ""), str(path))

        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
        check("沉淀: 标题写在首行", text.startswith("# H3C 端口镜像配置命令"), text[:40])
        check("沉淀: 含记录时间与分类",
              "记录时间:" in text and "分类: 命令速查" in text)
        check("沉淀: 含标签", "标签: 端口镜像, mirroring-group, S5560" in text)
        check("沉淀: 含设备元信息", "厂商: h3c" in text and "核心交换机" in text)
        check("沉淀: 含正文命令", "mirroring-group 1 local" in text)

        # 建索引后应该能被检索到（这就是"知识库越用越厚"的关键）
        kb.build_index()
        hits = kb.search("端口镜像 mirroring-group", top_k=3)
        check("沉淀: 可被本地检索命中",
              bool(hits) and "mirroring-group" in hits[0]["text"], repr(hits[:1]))
        check("沉淀: 可按标签/型号检索",
              bool(kb.search("S5560 端口镜像", top_k=3)))

        # 命令清单类笔记
        cmd_path = kb.save_commands(
            "Cisco 上电巡检命令",
            ["show version", "show ip interface brief"],
            tags=["cisco", "巡检"], note="设备上电后先跑这几条",
        )
        check("沉淀: 命令清单已保存", bool(cmd_path) and os.path.exists(cmd_path))
        with open(cmd_path, 'r', encoding='utf-8') as f:
            cmd_text = f.read()
        check("沉淀: 命令用代码块包裹",
              "```" in cmd_text and "show version" in cmd_text)
        check("沉淀: 附带说明文字", "上电后先跑这几条" in cmd_text)

        # 同名不覆盖、空标题自动命名
        second = kb.save_note("H3C 端口镜像配置命令", "第二次内容", category="命令速查")
        check("沉淀: 同名自动加序号", second != path and os.path.exists(second), str(second))
        auto = kb.save_note("", "内容")
        check("沉淀: 空标题自动命名", bool(auto) and os.path.exists(auto))
        check("沉淀: 标签可省略", bool(kb.save_note("无标签笔记", "内容", tags=None)))
        check("沉淀: 统计已更新", kb.stats()["documents"] >= 4, repr(kb.stats()))
    finally:
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_note_dialog():
    """沉淀对话框回填与取值"""
    from ui.note_dialog import NoteDialog

    dialog = NoteDialog(None, title="H3C 端口镜像", content="mirroring-group 1 local",
                        category="命令速查", tags=["h3c", "镜像"], root_dir="C:/kb")
    values = dialog.get_values()
    check("沉淀对话框: 标题回填", values["title"] == "H3C 端口镜像")
    check("沉淀对话框: 分类回填", values["category"] == "命令速查", values["category"])
    check("沉淀对话框: 标签回显", values["tags"] == "h3c, 镜像", values["tags"])
    check("沉淀对话框: 内容回填", "mirroring-group" in values["content"])
    check("沉淀对话框: 提示保存路径", "命令速查" in dialog.path_label.text(),
          dialog.path_label.text())

    dialog.title_input.setText("改过的标题")
    dialog.category_combo.setCurrentText("配置案例")
    check("沉淀对话框: 标题可改", dialog.get_values()["title"] == "改过的标题")
    check("沉淀对话框: 分类可改", dialog.get_values()["category"] == "配置案例")
    dialog.close()


def test_ai_panel_save_note():
    """资料检索结果 → 存为资料"""
    from ui.ai_panel import AIPanel

    panel = AIPanel()
    captured = []
    panel.save_note_requested.connect(
        lambda title, content, category: captured.append((title, content, category)))

    # 资料检索结果 → 存为资料
    answer = "结论: 用 mirroring-group 配置端口镜像 [1]"
    panel.question_input.setPlainText("H3C 怎么配置端口镜像")
    panel.show_research_result(answer, [
        {"title": "H3C命令手册（第 1 段）", "path": "C:/kb/a.md", "source_type": "local"},
        {"title": "某博客", "url": "https://example.com/x", "source_type": "web"},
    ])
    panel.on_save_research()
    check("AI面板: 检索结果可存为资料", len(captured) == 1, str(captured))
    title, content, category = captured[-1]
    check("AI面板: 标题取自提问", title.startswith("H3C 怎么配置端口镜像"), title)
    check("AI面板: 内容含回答", "mirroring-group" in content)
    check("AI面板: 内容含参考来源", "参考来源" in content and "命令手册" in content)
    check("AI面板: 分类为命令速查", category == "命令速查", category)
    panel.close()


def test_main_window_note_sinking():
    """主窗口：沉淀元信息、标签建议与落库"""
    import shutil
    import tempfile

    window = MainWindow()
    settings = window.settings
    original = dict(settings.data)
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_mw_kb_")
    try:
        settings.set("knowledge_dir", kb_dir)
        window.session_manager.refresh_knowledge()

        manager = window.session_manager
        session = SessionInfo(1, "核心交换机", "SSH")
        session.connector = DummyConnector()
        session.config = {"host": "10.0.0.1", "protocol": "SSH"}
        session.vendor = "h3c"
        session.ai_task = "检查端口镜像配置"
        manager._sessions[1] = session
        manager._next_id = 2
        window.on_session_created(1, "核心交换机")

        meta = window.build_note_meta(session)
        check("沉淀: 元信息含设备", "核心交换机" in meta.get("设备", ""), repr(meta))
        check("沉淀: 元信息含厂商", meta.get("厂商") == "h3c", repr(meta))
        check("沉淀: 元信息含任务", meta.get("任务") == "检查端口镜像配置", repr(meta))

        tags = window.suggest_tags(session, "端口镜像", "mirroring-group 1 local")
        check("沉淀: 建议标签含厂商与协议",
              "h3c" in tags and "SSH" in tags, tags)

        path = window.save_note_to_kb(
            "H3C 端口镜像命令", "mirroring-group 1 local",
            category="命令速查", tags=tags, meta=meta)
        check("沉淀: 主窗口保存成功", bool(path) and os.path.exists(path), str(path))

        # 等后台索引落盘后再检索（build_index 内部有锁，会等前一个完成）
        kb = window.session_manager.knowledge_base
        kb.build_index()
        hits = kb.search("端口镜像 mirroring-group", top_k=2)
        check("沉淀: 沉淀内容可被检索", bool(hits), repr(hits[:1]))
        check("沉淀: 资料库状态已刷新",
              "篇资料" in window.ai_panel.kb_status.text(),
              window.ai_panel.kb_status.text())

        window.close_session(0)
    finally:
        settings._data = original
        settings.save()
        shutil.rmtree(kb_dir, ignore_errors=True)
        window.close()


# ------------------------------------------------------------------ Excel 写入
def test_xlsx_writer():
    """轻量 xlsx 写入器（巡检导出用）"""
    import shutil
    import tempfile
    import zipfile
    from core.knowledge import extract_xlsx

    tmp = tempfile.mkdtemp(prefix="aiterminal_xlsx_")
    try:
        path = os.path.join(tmp, "巡检结果.xlsx")
        ok = write_xlsx(path, [
            ("巡检汇总", [
                ["序号", "设备名称", "状态", "耗时(秒)"],
                [1, "核心交换机", "成功", 3.2],
                [2, "接入交换机", "失败", 10.0],
            ]),
            ("命令明细", [
                ["设备", "命令", "输出"],
                ["核心交换机", "display version", "含特殊字符 & < > 与换行\n第二行"],
            ]),
        ], column_widths={"巡检汇总": [6, 20, 8, 10], "命令明细": [16, 26, 80]})

        check("xlsx: 文件已生成", ok and os.path.exists(path))
        with zipfile.ZipFile(path) as zf:
            check("xlsx: zip 结构完整", zf.testzip() is None)
            names = zf.namelist()
        check("xlsx: 含必要部件",
              all(part in names for part in (
                  '[Content_Types].xml', 'xl/workbook.xml', 'xl/styles.xml',
                  'xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml')))

        text = extract_xlsx(path)
        check("xlsx: 读回表头", "设备名称" in text and "状态" in text)
        check("xlsx: 读回数据行", "核心交换机" in text and "接入交换机" in text)
        check("xlsx: 读回数字", "3.2" in text and "10.0" in text)
        check("xlsx: 特殊字符与换行正确",
              "含特殊字符 & < > 与换行" in text and "第二行" in text)
        check("xlsx: 使用真实工作表名",
              "--- 巡检汇总 ---" in text and "--- 命令明细 ---" in text)

        # 非法字符与超长内容不应写坏文件
        dirty = os.path.join(tmp, "脏数据.xlsx")
        check("xlsx: 非法控制字符被清理",
              write_xlsx(dirty, [("S", [["正常\x00\x08文本", "x" * 40000]])]))
        dirty_text = extract_xlsx(dirty)
        check("xlsx: 清理后仍可读", "正常文本" in dirty_text and "已截断" in dirty_text)

        # 工作表名去重与非法字符
        dup = os.path.join(tmp, "重名.xlsx")
        check("xlsx: 工作表名处理",
              write_xlsx(dup, [("a/b", [["1"]]), ("a?b", [["2"]]), ("a*b", [["3"]])]))
        dup_text = extract_xlsx(dup)
        check("xlsx: 非法字符被替换", "--- a_b ---" in dup_text, repr(dup_text[:60]))
        check("xlsx: 重名自动加序号",
              "a_b_2" in dup_text and "a_b_3" in dup_text, repr(dup_text[:90]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 笔记手册
def test_manual():
    """笔记筛选与批量导出成手册"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase

    kb_dir = tempfile.mkdtemp(prefix="aiterminal_manual_")
    try:
        kb = KnowledgeBase(root_dir=kb_dir)
        kb.save_note("H3C S5560 端口镜像配置命令",
                     "```\nmirroring-group 1 local\n```",
                     category="命令速查", tags="h3c, 端口镜像",
                     meta={"设备": "核心交换机", "厂商": "h3c",
                           "记录时间": "2026-09-21 19:46:24"})
        kb.save_note("Cisco 上电巡检命令",
                     "```\nshow version\n```",
                     category="命令速查", tags="cisco, 巡检")
        kb.save_note("接入交换机排错记录",
                     "端口 err-disable，先 shutdown 再 undo shutdown",
                     category="排错记录", tags="h3c, 排错")
        kb.build_index()

        # 解析
        parsed = manual_mod.parse_note(open(
            os.path.join(kb_dir, "命令速查", "H3C S5560 端口镜像配置命令.md"),
            encoding="utf-8").read())
        check("手册: 解析标题", parsed["title"] == "H3C S5560 端口镜像配置命令")
        check("手册: 解析分类", parsed["category"] == "命令速查", parsed["category"])
        check("手册: 解析标签", parsed["tags"] == ["h3c", "端口镜像"], repr(parsed["tags"]))
        check("手册: 解析设备元信息", parsed["meta"].get("设备") == "核心交换机")
        check("手册: 解析正文", "mirroring-group" in parsed["body"])
        check("手册: 正文不含署名行", "NetOps Terminal 保存" not in parsed["body"])

        notes = manual_mod.load_notes(kb)
        check("手册: 载入笔记数量", len(notes) == 3, str(len(notes)))
        check("手册: 分类来自元信息",
              {n["category"] for n in notes} == {"命令速查", "排错记录"},
              repr({n["category"] for n in notes}))

        tags = manual_mod.collect_tags(notes)
        check("手册: 标签统计", tags.get("h3c") == 2 and tags.get("cisco") == 1, repr(tags))
        categories = manual_mod.collect_categories(notes)
        check("手册: 分类统计", categories.get("命令速查") == 2, repr(categories))

        # 筛选
        check("手册: 按分类筛选",
              len(manual_mod.filter_notes(notes, categories=["命令速查"])) == 2)
        check("手册: 按标签筛选",
              len(manual_mod.filter_notes(notes, tags=["cisco"])) == 1)
        check("手册: 按关键词筛选",
              len(manual_mod.filter_notes(notes, keyword="端口镜像")) == 1)
        check("手册: 多条件组合",
              len(manual_mod.filter_notes(notes, categories=["命令速查"],
                                          tags=["h3c"])) == 1)

        # Markdown 手册
        markdown = manual_mod.build_manual(notes, "命令手册", "测试副标题")
        check("手册: 含主标题", markdown.startswith("# 命令手册"))
        check("手册: 含副标题与统计", "测试副标题" in markdown and "共 3 篇笔记" in markdown)
        check("手册: 含目录", "## 目录" in markdown)
        check("手册: 按分类分章", markdown.count("# 命令速查") == 1
              and markdown.count("# 排错记录") == 1)
        check("手册: 笔记标题为二级标题", "## H3C S5560 端口镜像配置命令" in markdown)
        check("手册: 保留元信息", "厂商: h3c" in markdown)

        # HTML 手册
        html_text = manual_mod.build_manual_html(notes, "命令手册")
        check("手册: HTML 自包含", html_text.startswith("<!DOCTYPE html>")
              and "<style>" in html_text)
        check("手册: HTML 含目录清单", "笔记清单（3 篇）" in html_text)
        check("手册: HTML 渲染标题", "<h2>H3C S5560 端口镜像配置命令</h2>" in html_text)
        check("手册: HTML 渲染代码块", "<pre><code>" in html_text)
        check("手册: HTML 转义尖括号", "&lt;" in html_text or "<h2" in html_text)

        # 行内渲染
        inline = manual_mod.markdown_to_html("**粗体** 和 `代码` 和 [链接](https://x.com)")
        check("手册: 行内格式",
              "<strong>粗体</strong>" in inline and "<code>代码</code>" in inline
              and '<a href="https://x.com">链接</a>' in inline, inline)

        # 导出
        md_path = os.path.join(kb_dir, "out", "手册.md")
        check("手册: 导出 Markdown", manual_mod.export_manual(md_path, notes, "命令手册"))
        check("手册: Markdown 文件存在", os.path.exists(md_path))
        html_path = os.path.join(kb_dir, "out", "手册.html")
        check("手册: 导出 HTML", manual_mod.export_manual(html_path, notes, "命令手册"))
        with open(html_path, encoding="utf-8") as f:
            check("手册: HTML 内容完整", "</html>" in f.read())
        csv_path = os.path.join(kb_dir, "out", "清单.csv")
        check("手册: 导出清单 CSV", manual_mod.export_notes_csv(csv_path, notes))
        with open(csv_path, encoding="utf-8-sig") as f:
            csv_text = f.read()
        check("手册: CSV 含分类与标签",
              "命令速查" in csv_text and "端口镜像" in csv_text)
    finally:
        shutil.rmtree(kb_dir, ignore_errors=True)


# ------------------------------------------------------------------ 等提示符
class _ScriptedConnector(DummyConnector):
    """按脚本输出回显的连接器，用于验证提示符判定"""

    def __init__(self, on_send: str = "", on_raw: str = ""):
        super().__init__(simulate_prompt=False)
        self.on_send = on_send
        self.on_raw = on_raw

    def send(self, command: str):
        self.sent.append(command)
        if self.on_send:
            self._buffer += self.on_send          # 同时写缓冲区，验证监听器不消耗它
            self._notify_output(self.on_send)

    def send_raw(self, data: str):
        self.sent.append(data)
        if self.on_raw:
            self._buffer += self.on_raw
            self._notify_output(self.on_raw)


def test_command_waiter():
    """按提示符判断命令是否执行完"""
    # 1) 正常返回提示符
    connector = _ScriptedConnector(on_send="display version\r\nH3C Comware\r\n<H3C>")
    waiter = CommandWaiter(connector, quiet_time=0.1, timeout=5, silence_timeout=1.0)
    waiter.start()
    status, elapsed, output = waiter.run("display version")
    check("提示符: 判定为完成", status == "prompt", status)
    check("提示符: 收集到输出", "H3C Comware" in output, repr(output[:60]))
    check("提示符: 不消耗连接器缓冲区",
          "H3C Comware" in connector.get_output())
    waiter.stop()

    # 2) 需要人工确认
    confirm = _ScriptedConnector(on_send="Are you sure? [Y/N]")
    waiter = CommandWaiter(confirm, quiet_time=0.1, timeout=5, silence_timeout=2.0)
    waiter.start()
    status, _elapsed, _output = waiter.run("reboot")
    check("提示符: 识别确认提示", status == "confirm", status)
    waiter.stop()

    # 3) 分页自动翻页
    pager = _ScriptedConnector(on_send="display current-configuration\r\nvlan 10\r\n"
                                        " ---- More ----", on_raw=" port link-type access\r\n<H3C>")
    waiter = CommandWaiter(pager, quiet_time=0.1, timeout=5, silence_timeout=1.0)
    waiter.start()
    status, _elapsed, output = waiter.run("display current-configuration")
    check("提示符: 自动翻页后完成", status == "prompt", status)
    check("提示符: 记录翻页次数", waiter.pages == 1, str(waiter.pages))
    check("提示符: 翻页补发空格", " " in pager.sent, repr(pager.sent))
    check("提示符: 翻页后内容仍保留", "vlan 10" in output, repr(output[:80]))
    waiter.stop()

    # 4) 无提示符但有输出 → 静默兜底
    silent = _ScriptedConnector(on_send="部分输出但没有提示符")
    waiter = CommandWaiter(silent, quiet_time=0.1, timeout=5, silence_timeout=0.5)
    waiter.start()
    status, _elapsed, _output = waiter.run("show x")
    check("提示符: 静默兜底", status == "silence", status)
    waiter.stop()

    # 5) 一直没输出 → 超时
    quiet = _ScriptedConnector(on_send="")
    waiter = CommandWaiter(quiet, quiet_time=0.1, timeout=0.6, silence_timeout=60)
    waiter.start()
    status, elapsed, _output = waiter.run("show x")
    check("提示符: 超时返回", status == "timeout", status)
    check("提示符: 超时耗时合理", 0.5 <= elapsed < 3.0, f"{elapsed:.2f}s")
    waiter.stop()

    # 6) 断开
    offline = DummyConnector(simulate_prompt=False)
    waiter = CommandWaiter(offline, quiet_time=0.1, timeout=5, silence_timeout=5)
    waiter.start()
    offline._connected = False
    status, _elapsed = waiter.wait()
    check("提示符: 连接断开识别", status == "disconnected", status)
    waiter.stop()
    check("提示符: 监听器已注销", offline._output_listeners == [])


def test_batch_prompt_mode():
    """批量下发：等提示符模式与结果汇总"""
    connector = DummyConnector(simulate_prompt=True)
    runner = BatchRunner(connector, ["display version", "display interface brief"],
                         delay_ms=0, block_dangerous=True,
                         wait_mode=BatchRunner.WAIT_PROMPT,
                         quiet_time=0.1, prompt_timeout=5, silence_timeout=1.0)
    done_events = []
    finished = []
    runner.command_done.connect(lambda i, s, e: done_events.append((i, s, round(e, 1))))
    runner.finished.connect(lambda ok, msg: finished.append((ok, msg)))
    runner._run()

    check("批量: 等提示符模式全部完成",
          len(done_events) == 2 and all(s == "prompt" for _i, s, _e in done_events),
          repr(done_events))
    check("批量: 结束回调成功", bool(finished) and finished[0][0] is True, repr(finished))
    check("批量: 汇总含条数", "共 2 条" in finished[0][1], finished[0][1])

    rows = runner.summary_rows()
    check("批量: 结果行含状态文字",
          rows and rows[0][2] == "完成（提示符返回）", repr(rows[:1]))
    check("批量: 结果行含输出",
          "OK" in rows[0][4], repr(rows[0][4][:40]))

    # 固定间隔模式仍然可用
    connector2 = DummyConnector(simulate_prompt=True)
    runner2 = BatchRunner(connector2, ["display version"], delay_ms=0,
                          wait_mode=BatchRunner.WAIT_DELAY)
    runner2._run()
    check("批量: 固定间隔模式",
          runner2.results and runner2.results[0]["status"] == "delay",
          repr(runner2.results[:1]))

    # 需要确认时中止后续
    confirm = _ScriptedConnector(on_send="Are you sure? [Y/N]")
    runner3 = BatchRunner(confirm, ["reboot", "display version"], delay_ms=0,
                          block_dangerous=False, wait_mode=BatchRunner.WAIT_PROMPT,
                          quiet_time=0.1, prompt_timeout=5, silence_timeout=2.0)
    messages = []
    runner3.finished.connect(lambda ok, msg: messages.append((ok, msg)))
    runner3._run()
    check("批量: 确认提示后暂停",
          messages and messages[0][0] is False and "人工确认" in messages[0][1],
          repr(messages))
    check("批量: 暂停后不再下发后续命令",
          confirm.sent == ["reboot"], repr(confirm.sent))


def test_patrol():
    """多设备并发巡检与导出"""
    import shutil
    import tempfile
    from core.knowledge import extract_xlsx

    devices = [
        {"name": "核心交换机", "protocol": "SSH", "host": "10.0.0.1",
         "username": "admin", "password": "x"},
        {"name": "接入交换机", "protocol": "SSH", "host": "10.0.0.2",
         "username": "admin", "password": "x"},
        {"name": "汇聚交换机", "protocol": "Telnet", "host": "10.0.0.3",
         "username": "admin", "password": "x"},
    ]
    commands = ["display version", "display interface brief"]

    original_build = patrol_mod.build_connector
    original_connect = patrol_mod.connect_device
    patrol_mod.build_connector = lambda config: (
        DummyConnector(simulate_prompt=True), config.get('protocol', 'SSH'))
    patrol_mod.connect_device = lambda connector, protocol, config, timeout=10: (True, "")
    tmp = tempfile.mkdtemp(prefix="aiterminal_patrol_")
    try:
        runner = PatrolRunner(devices, commands, concurrency=3,
                              quiet_time=0.1, prompt_timeout=5, silence_timeout=1.0,
                              task_name="测试巡检")
        done = []
        runner.device_done.connect(lambda i, r: done.append((i, r)))
        runner._run()

        check("巡检: 设备全部执行", len(runner.results) == 3, str(len(runner.results)))
        check("巡检: 每台都成功", all(r["ok"] for r in runner.results),
              repr([(r["name"], r["ok"], r["error"]) for r in runner.results]))
        check("巡检: 每台命令数正确",
              all(len(r["commands"]) == 2 for r in runner.results))
        check("巡检: 命令结果含输出",
              all("OK" in c["output"] for r in runner.results for c in r["commands"]))
        check("巡检: 发出设备完成信号", len(done) == 3, str(len(done)))

        rows = runner.summary_rows()
        check("巡检: 汇总表表头", rows[0][:4] == ["序号", "设备名称", "协议", "地址"])
        check("巡检: 汇总表行数", len(rows) == 4, str(len(rows)))
        check("巡检: 汇总表状态为成功", rows[1][4] == "成功", repr(rows[1]))
        detail = runner.detail_rows()
        check("巡检: 明细表行数", len(detail) == 1 + 3 * 2, str(len(detail)))
        check("巡检: 明细含设备与命令",
              detail[1][0] == "核心交换机" and detail[1][2] == "display version",
              repr(detail[1]))
        info = runner.info_rows({"done": 3, "ok": 3, "failed": 0})
        check("巡检: 信息表含任务名", any("测试巡检" in str(row) for row in info))

        # Excel 导出并读回
        xlsx_path = os.path.join(tmp, "巡检结果.xlsx")
        check("巡检: 导出 Excel",
              export_patrol_excel(xlsx_path, runner,
                                  {"done": 3, "ok": 3, "failed": 0}))
        xlsx_text = extract_xlsx(xlsx_path)
        check("巡检: Excel 含三个工作表",
              all(f"--- {name} ---" in xlsx_text
                  for name in ("巡检信息", "巡检汇总", "命令明细")))
        check("巡检: Excel 含设备与输出",
              "核心交换机" in xlsx_text and "display version" in xlsx_text)

        # CSV 导出
        csv_path = os.path.join(tmp, "巡检结果.csv")
        check("巡检: 导出 CSV", export_patrol_csv(csv_path, runner))
        check("巡检: CSV 汇总与明细都生成",
              os.path.exists(csv_path)
              and os.path.exists(os.path.join(tmp, "巡检结果_明细.csv")))

        # 空任务校验
        check("巡检: 无设备时拒绝启动",
              PatrolRunner([], commands).start() is False)
        check("巡检: 无命令时拒绝启动",
              PatrolRunner(devices, []).start() is False)

        # 连接失败要如实记录
        patrol_mod.connect_device = lambda connector, protocol, config, timeout=10: (
            False, "认证失败：用户名或密码错误")
        failed = patrol_mod.patrol_device(devices[0], commands)
        check("巡检: 连接失败被记录",
              failed["ok"] is False and "认证失败" in failed["error"], repr(failed))
    finally:
        patrol_mod.build_connector = original_build
        patrol_mod.connect_device = original_connect
        shutil.rmtree(tmp, ignore_errors=True)


def test_patrol_dialog():
    """巡检界面：设备勾选、模板载入、参数"""
    from ui.patrol_dialog import PatrolDialog, DEFAULT_COMMANDS

    settings, snapshot = _settings_snapshot()
    # 清掉上次保存的巡检命令，保证"默认命令"断言不受历史状态影响
    settings.set_patrol_commands([])
    try:
        sessions = [
            {"name": "核心交换机", "protocol": "SSH", "host": "10.0.0.1",
             "username": "admin", "password": "x"},
            {"name": "接入交换机", "protocol": "Telnet", "host": "10.0.0.2",
             "username": "admin", "password": "x"},
        ]
        candidates = [{"name": "历史设备", "protocol": "SSH", "host": "10.0.0.9",
                       "username": "admin"}]

        dialog = PatrolDialog(None, candidates=candidates, sessions=sessions)
        check("巡检界面: 当前会话已加入", dialog.table.rowCount() == 2,
              str(dialog.table.rowCount()))
        check("巡检界面: 默认全部勾选", len(dialog.checked_devices()) == 2)

        dialog.set_all_checked(False)
        check("巡检界面: 全不选", dialog.checked_devices() == [])
        dialog.set_all_checked(True)

        dialog.add_from_candidates()
        check("巡检界面: 候选设备已加入", dialog.table.rowCount() == 3)
        dialog.add_from_candidates()
        check("巡检界面: 重复设备不重复添加", dialog.table.rowCount() == 3)

        check("巡检界面: 默认命令非空",
              dialog.get_commands() == DEFAULT_COMMANDS.split("\n"),
              repr(dialog.get_commands()[:3]))

        dialog.template_combo.setCurrentText("设备基础巡检")
        dialog.load_template()
        commands = dialog.get_commands()
        check("巡检界面: 模板载入",
              "display version" in commands and "display logbuffer" in commands,
              repr(commands))

        dialog.command_edit.setPlainText("# 注释行\n\ndisplay version\n  \ndisplay cpu-usage")
        check("巡检界面: 过滤注释与空行",
              dialog.get_commands() == ["display version", "display cpu-usage"],
              repr(dialog.get_commands()))

        dialog.concurrency_spin.setValue(8)
        dialog.save_settings()
        check("巡检界面: 参数已保存", dialog.settings.patrol_concurrency == 8)

        check("巡检界面: 默认导出文件名",
              dialog.default_name("xlsx").startswith("巡检结果_")
              and dialog.default_name("xlsx").endswith(".xlsx"))
        dialog.close()
    finally:
        _settings_restore(settings, snapshot)


def test_knowledge_dialog_filters():
    """资料库界面：分类/标签筛选与手册导出选择"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase
    from ui.knowledge_dialog import KnowledgeDialog

    settings, snapshot = _settings_snapshot()
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_kdf_")
    try:
        kb = KnowledgeBase(root_dir=kb_dir)
        kb.save_note("H3C 端口镜像", "```\nmirroring-group 1 local\n```",
                     category="命令速查", tags="h3c, 端口镜像")
        kb.save_note("Cisco 巡检", "```\nshow version\n```",
                     category="命令速查", tags="cisco, 巡检")
        kb.save_note("排错记录一", "端口 err-disable 处理", category="排错记录", tags="h3c")
        # 一份没有元信息的原始 txt（会被当成"未分类"笔记）
        with open(os.path.join(kb_dir, "随手记.txt"), "w", encoding="utf-8") as f:
            f.write("原始文档内容")
        # 一份 PDF（非笔记，不参与手册合并）
        _make_minimal_pdf(os.path.join(kb_dir, "官方手册.pdf"),
                          ["official manual page one"])
        kb.build_index()

        dialog = KnowledgeDialog(None, kb)
        dialog.wait_loaded()          # 2026-10-17 起构造是异步的（载入在后台线程）
        check("资料库界面: 列表含全部资料", dialog.table.rowCount() == 5,
              str(dialog.table.rowCount()))
        check("资料库界面: 分类下拉含分类",
              any(dialog.category_combo.itemData(i) == "命令速查"
                  for i in range(dialog.category_combo.count())))
        check("资料库界面: 标签下拉含标签",
              any(dialog.tag_combo.itemData(i) == "h3c"
                  for i in range(dialog.tag_combo.count())))

        # 按分类筛选
        index = dialog.category_combo.findData("命令速查")
        dialog.category_combo.setCurrentIndex(index)
        check("资料库界面: 按分类筛选", dialog.table.rowCount() == 2,
              str(dialog.table.rowCount()))

        # 叠加标签筛选
        index = dialog.tag_combo.findData("cisco")
        dialog.tag_combo.setCurrentIndex(index)
        check("资料库界面: 按标签筛选", dialog.table.rowCount() == 1,
              str(dialog.table.rowCount()))

        # 关键词筛选
        dialog.reset_filter()
        dialog.filter_input.setText("排错")
        check("资料库界面: 关键词筛选", dialog.table.rowCount() == 1,
              str(dialog.table.rowCount()))
        dialog.reset_filter()
        check("资料库界面: 重置筛选", dialog.table.rowCount() == 5)

        # 分类列显示：笔记显示元信息分类，无元信息的显示"未分类"
        category_by_name = {
            dialog.table.item(row, 1).text(): dialog.table.item(row, 2).text()
            for row in range(dialog.table.rowCount())
        }
        check("资料库界面: 笔记显示元信息分类",
              category_by_name.get("命令速查\\H3C 端口镜像.md") == "命令速查",
              repr(category_by_name))
        check("资料库界面: 无元信息的 txt 归为未分类",
              category_by_name.get("随手记.txt") == "未分类",
              repr(category_by_name.get("随手记.txt")))

        # 导出选择：未勾选时按筛选条件
        dialog.filter_input.setText("命令速查")
        selected = dialog.select_notes_for_export()
        check("资料库界面: 按筛选选笔记",
              len(selected) == 2 and all(n["category"] == "命令速查" for n in selected),
              str(len(selected)))

        # 勾选后优先用勾选，且非文本资料不参与手册合并
        dialog.reset_filter()
        dialog.set_all_checked(True)
        check("资料库界面: 全选后勾选路径数", len(dialog.checked_paths()) == 5,
              str(len(dialog.checked_paths())))
        selected = dialog.select_notes_for_export()
        check("资料库界面: 勾选时只导出文本笔记",
              len(selected) == 4
              and all(n["name"].endswith(('.md', '.txt')) for n in selected),
              repr([n["name"] for n in selected]))
        check("资料库界面: PDF 不参与手册合并",
              all(not n["name"].endswith('.pdf') for n in selected))

        check("资料库界面: 检索测试页签可用", dialog.tabs.count() == 2)
        dialog.close()
    finally:
        _settings_restore(settings, snapshot)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_main_window():
    window = MainWindow()

    check("主窗口创建", window is not None)
    check("初始无标签页", window.tab_widget.count() == 0)

    # 注入一个假会话，验证标签页接线与输出渲染
    manager = window.session_manager
    session = SessionInfo(1, "测试设备", "SSH")
    session.connector = DummyConnector()
    manager._sessions[1] = session
    manager._next_id = 2
    window.on_session_created(1, "测试设备")

    check("标签页已创建", window.tab_widget.count() == 1)
    check("标签页携带 session_id",
          window.tab_widget.widget(0).property('session_id') == 1)

    # 模拟设备输出 -> 定时器刷新 -> 终端显示
    session.connector._buffer = "\x1b[32mOK\x1b[0m\r\nswitch#"
    window.update_output()
    text = window.tabs[1].terminal.text()
    check("输出渲染到终端", "OK" in text and "switch#" in text, repr(text))

    # 模拟用户输入命令
    window.on_command_entered(1, "display version")
    check("命令已下发", session.connector.sent == ["display version"], repr(session.connector.sent))

    # 中断信号
    window.on_interrupt(1)
    check("Ctrl+C 中断", session.connector.sent[-1] == "\x03", repr(session.connector.sent))

    # AI 未启用时心跳不动作
    window.ai_tick()
    check("AI未启用时心跳安全", session.ai_busy is False)

    # 会话输出记录（供 AI 使用）
    session.record_output("x" * 25000)
    check("输出记录有上限", len(session.recent_output) <= SessionInfo.MAX_RECENT)

    # v0.2: 工具菜单动作已挂上
    check("工具菜单: 自动日志开关", window.act_auto_log is not None)

    # v0.3: 串口按钮默认禁用（非 Console 会话）
    check("工具栏: 串口控制默认禁用", window.action_serial.isEnabled() is False)

    # v0.3: AI 高级面板只留 AI 设置入口与资料检索
    check("AI面板: 主窗口内两个页签", window.ai_panel.tabs.count() == 2)
    check("AI面板: 待执行队列已移除",
          not hasattr(window.ai_panel, "queue_list"))

    # v0.3: 本地回显（Console 场景）
    window.tabs[1].terminal.set_local_echo(True)
    window.tabs[1].terminal._echo_text("ping 1.1.1.1")
    check("终端: 本地回显生效",
          "ping 1.1.1.1" in window.tabs[1].terminal.text())
    window.tabs[1].terminal.set_local_echo(False)

    # v0.3: 批量下发接线（后台线程执行，等提示符模式下每条约 0.3s）
    window.run_batch(1, ["display version", "display interface brief"], 0, True)
    time.sleep(1.6)
    sent = session.connector.sent
    check("批量下发已执行到连接器",
          "display version" in sent and "display interface brief" in sent, repr(sent))
    if window.progress_dialog:
        window.progress_dialog.close()
        window.progress_dialog = None

    # 关闭会话
    window.close_session(0)
    check("会话已关闭", window.tab_widget.count() == 0 and len(manager.get_all_sessions()) == 0)

    window.close()
    check("窗口关闭无异常", True)


def main():
    global _APP
    print("=" * 60)
    print("NetOpsTerminal 冒烟测试")
    print("=" * 60)
    _APP = QApplication.instance() or QApplication([])
    test_terminal_buffer()
    test_terminal_input_tracking()
    test_terminal_backspace_keeps_device_echo()
    test_terminal_local_echo_no_duplicate()
    test_terminal_cr_nul_not_swallowed()
    test_console_echo_no_doubling_any_shape()
    test_patrol_device_edit()
    test_session_connection_lost_visible()
    test_terminal_tab_reaches_device()
    test_nav_rail()
    test_terminal_find_and_zoom()
    test_terminal_tab_repeat()
    test_command_hint()
    test_terminal_line_resync()
    test_terminal_command_hint()
    test_device_version_info()
    test_command_table_is_local()
    test_document_digest()
    test_command_lines_parsing()
    test_device_version_probe()
    test_ai_agent()
    test_settings()
    test_presets_and_plugins()
    test_telnet_fallback()
    print("-" * 60)
    print("v0.2 功能")
    print("-" * 60)
    test_diff()
    test_templates()
    test_logger()
    test_batch()
    test_snapshot_and_capture()
    test_new_settings()
    test_ai_prompt_override()
    print("-" * 60)
    print("v0.3 联网检索 / Console")
    print("-" * 60)
    test_web_search_parsing()
    test_web_search_client()
    test_ai_search_flow()
    test_serial_console()
    print("-" * 60)
    print("v0.4 本地资料库")
    print("-" * 60)
    test_knowledge_base()
    test_ai_local_lookup()
    test_experience_memory()
    test_save_note()

    # 主窗口用例会写配置（批量间隔、会话列表等），测试后还原，避免污染用户配置
    settings = Settings()
    snapshot = dict(settings.data)
    try:
        test_main_window()
    finally:
        settings._data = snapshot
        settings.save()

    test_v02_dialogs()
    test_ai_panel_tabs()
    test_knowledge_dialog()
    test_note_dialog()
    test_ai_panel_save_note()
    test_main_window_note_sinking()
    print("-" * 60)
    print("v0.6 Excel 导出 / 笔记手册 / 等提示符 / 并发巡检")
    print("-" * 60)
    test_xlsx_writer()
    test_manual()
    test_command_waiter()
    test_batch_prompt_mode()
    test_patrol()
    test_patrol_dialog()
    test_knowledge_dialog_filters()
    print("-" * 60)
    print("v0.7 命令安全分级 / 配置命令确认闸门")
    print("-" * 60)
    test_command_safety()
    test_config_confirm_dialog()
    test_paging_off_commands()
    test_vendor_detect_robustness()
    print("-" * 60)
    print("v0.7 大模型提供方（Anthropic / DeepSeek / OpenAI 兼容）")
    print("-" * 60)
    test_llm_client()
    test_ai_provider_settings()
    print("-" * 60)
    print("v0.7 NetCatty 式界面：设备库 Vault + Catty 对话")
    print("-" * 60)
    test_vault_and_converse()

    print("-" * 60)
    failed = [r for r in RESULTS if not r[1]]
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        for name, _, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    print("全部通过 ✔")
    return 0




# ------------------------------------------------------------------ 命令安全分级
def test_command_safety():
    """只读命令与配置命令的分级（配置命令必须人工确认的依据）"""
    read_only = [
        "display version", "display interface brief", "display current-configuration",
        "show running-config", "show ip route", "ping 1.1.1.1",
        "traceroute 8.8.8.8", "dir flash:", "screen-length disable",
        "terminal length 0", "quit", "return", "exit",
    ]
    config = [
        "system-view", "configure terminal", "vlan 901", "undo vlan 901",
        "interface GigabitEthernet1/0/1", "ip address 1.1.1.1 255.255.255.0",
        "name TEST", "port link-type access", "snmp-agent", "save",
        "write memory", "reboot", "shutdown", "no shutdown", "delete flash:/x",
    ]
    for command in read_only:
        check(f"安全分级: 只读 {command[:28]}", safety_mod.is_read_only(command) is True)
    for command in config:
        check(f"安全分级: 配置 {command[:28]}", safety_mod.is_config_command(command) is True)

    check("安全分级: 未知命令按配置处理",
          safety_mod.is_config_command("some-unknown-cmd 123") is True)
    check("安全分级: 空命令不算配置", safety_mod.is_config_command("") is False)
    check("安全分级: 大小写不敏感",
          safety_mod.is_read_only("DISPLAY VERSION") is True)

    result = safety_mod.classify(["display version", "vlan 901", "save", ""])
    check("安全分级: 分类统计",
          result["total"] == 3 and len(result["config"]) == 2
          and len(result["read_only"]) == 1, repr(result))
    check("安全分级: 含配置命令判定",
          safety_mod.has_config_command(["display version", "vlan 901"]) is True)
    check("安全分级: 纯只读判定",
          safety_mod.has_config_command(["display version", "show ip route"]) is False)

    notes = safety_mod.risk_notes(["system-view", "vlan 901", "save", "undo vlan 902"])
    check("安全分级: 风险提示覆盖进入配置视图",
          any("配置视图" in n for n in notes), repr(notes))
    check("安全分级: 风险提示覆盖保存", any("启动配置" in n for n in notes), repr(notes))
    check("安全分级: 风险提示覆盖删除", any("删除" in n for n in notes), repr(notes))

    text = safety_mod.describe(["display version", "vlan 901"])
    check("安全分级: 说明文本含配置命令", "vlan 901" in text and "只读" in text)


def test_config_confirm_dialog():
    """配置命令确认框"""
    from ui.command_confirm import ConfigConfirmDialog

    commands = ["system-view", "vlan 901", "quit", "return"]
    dialog = ConfigConfirmDialog(None, commands, target="核心交换机", action="批量下发")
    check("确认框: 列出全部命令", dialog.table.rowCount() == 4,
          str(dialog.table.rowCount()))
    check("确认框: 识别配置命令数", len(dialog.result_info["config"]) == 2,
          repr(dialog.result_info))
    check("确认框: 退出视图不算配置命令",
          set(dialog.result_info["read_only"]) == {"quit", "return"},
          repr(dialog.result_info["read_only"]))
    check("确认框: 默认未确认", dialog.confirmed is False)
    check("确认框: 命令原文可复制",
          dialog.text_view.toPlainText() == "\n".join(commands))
    check("确认框: 类型列标注",
          dialog.table.item(1, 2).text() == "配置"
          and dialog.table.item(1, 1).text() == "vlan 901")
    dialog.close()

    # 纯只读命令也应能构造（用于统一提示）
    readonly = ConfigConfirmDialog(None, ["display version"], target="sw1")
    check("确认框: 只读命令无需二次确认",
          len(readonly.result_info["config"]) == 0, repr(readonly.result_info))
    readonly.close()


def test_paging_off_commands():
    """关分屏命令要按厂商选择，并在被设备拒绝时自动换下一条"""
    from core.batch import PAGING_OFF_COMMANDS

    check("关分屏: Comware5 先试 disable",
          PAGING_OFF_COMMANDS["h3c"][0] == "screen-length disable",
          repr(PAGING_OFF_COMMANDS["h3c"]))
    check("关分屏: Cisco 用 terminal length 0",
          PAGING_OFF_COMMANDS["cisco"] == ("terminal length 0",))
    check("关分屏: 未知厂商覆盖三家",
          len(PAGING_OFF_COMMANDS["unknown"]) == 3,
          repr(PAGING_OFF_COMMANDS["unknown"]))

    # 设备拒绝第一条命令时应自动换下一条
    class _RejectingConnector(_ScriptedConnector):
        def __init__(self):
            super().__init__(on_send="")
            self.count = 0

        def send(self, command: str):
            self.sent.append(command)
            self.count += 1
            if self.count == 1:
                out = (f"{command}\r\n^\r\n"
                       "% Unrecognized command found at '^' position.\r\n<H3C>")
            else:
                out = (f"{command}\r\n"
                       "% Screen-length configuration is disabled for current user.\r\n<H3C>")
            self._buffer += out
            self._notify_output(out)

    connector = _RejectingConnector()
    ok, command, response = disable_paging(connector, vendor="h3c",
                                           quiet_time=0.1, timeout=3)
    check("关分屏: 被拒绝后换下一条", ok is True and command == "screen-length 0 temporary",
          f"{ok} {command}")
    check("关分屏: 返回设备响应", "disabled" in response.lower(), repr(response[:60]))
    check("关分屏: 实际发了两次", len(connector.sent) == 2, repr(connector.sent))

    # 全部被拒绝时要如实返回失败
    class _AllReject(_ScriptedConnector):
        def send(self, command: str):
            self.sent.append(command)
            out = f"{command}\r\n% Unrecognized command found at '^' position.\r\n<H3C>"
            self._buffer += out
            self._notify_output(out)

    ok2, command2, _resp = disable_paging(_AllReject(), vendor="h3c",
                                          quiet_time=0.1, timeout=2)
    check("关分屏: 全部被拒绝时返回失败", ok2 is False and command2 == "")


def test_vendor_detect_robustness():
    """厂商识别：不能被 description 里的"对端设备名"带偏"""
    from core.terminal import detect_vendor

    # 强特征优先
    check("厂商识别: H3C Comware",
          detect_vendor("H3C Comware Platform Software\nComware Software, Version 5.20") == "h3c")
    check("厂商识别: 华为 VRP",
          detect_vendor("Huawei Versatile Routing Platform Software VRP (R) software") == "huawei")
    check("厂商识别: Cisco IOS",
          detect_vendor("Cisco IOS Software, C2960 Software") == "cisco")

    # 关键回归：接口描述里写了 huawei，但设备是 H3C
    config = """
#
 version 5.20, Release 5101P05
#
 sysname zhengfayonghuhuiju
#
 interface GigabitEthernet1/0/1
  description to 2-4 huawei
  port link-type access
#
 interface GigabitEthernet1/0/2
  description to 2-4_huawei25
#
"""
    check("厂商识别: 描述里的 huawei 不误判（无强特征时为 unknown）",
          detect_vendor(config) == "unknown", detect_vendor(config))
    check("厂商识别: 版本信息与配置一起看能认出 H3C",
          detect_vendor("H3C Comware Platform Software\n" + config) == "h3c")

    check("厂商识别: 注释里的 cisco 不误判",
          detect_vendor("# ! remark cisco uplink\n vlan 10") == "unknown",
          detect_vendor("# ! remark cisco uplink\n vlan 10"))


# ------------------------------------------------------------------ 大模型提供方
def test_llm_client():
    """统一大模型客户端：地址拼接、请求构造、错误翻译（用本地假服务，不联网）"""
    import http.server
    import json as json_mod
    import threading as th
    from core.llm import LLMClient, LLMError, chat_url, provider_defaults

    # 地址拼接
    check("LLM: DeepSeek 地址",
          chat_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/chat/completions",
          chat_url("https://api.deepseek.com"))
    check("LLM: 带 /v1 的地址不重复拼",
          chat_url("https://x.com/v1") == "https://x.com/v1/chat/completions")
    check("LLM: 完整地址原样使用",
          chat_url("https://x.com/v1/chat/completions") == "https://x.com/v1/chat/completions")
    try:
        chat_url("")
        check("LLM: 空地址应报错", False)
    except LLMError as e:
        check("LLM: 空地址报错提示", "接口地址" in str(e), str(e))

    # 提供方默认值
    check("LLM: Anthropic 默认模型",
          provider_defaults("anthropic")["model"].startswith("claude"))
    check("LLM: DeepSeek 默认配置",
          provider_defaults("deepseek")["base_url"] == "https://api.deepseek.com"
          and provider_defaults("deepseek")["model"] == "deepseek-chat")
    check("LLM: 未知提供方回落 Anthropic",
          provider_defaults("nope")["model"].startswith("claude"))

    # 未配置密钥时直接报错，不发请求
    client = LLMClient(provider="deepseek", api_key="", model="deepseek-chat")
    check("LLM: 无密钥时 configured=False", client.configured is False)
    try:
        client.chat(system="s", messages=[{"role": "user", "content": "hi"}])
        check("LLM: 无密钥应报错", False)
    except LLMError as e:
        check("LLM: 无密钥报错提示", "API Key" in str(e), str(e))

    # 本地假服务验证 OpenAI 兼容链路
    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            payload = json_mod.loads(self.rfile.read(length).decode('utf-8'))
            captured['path'] = self.path
            captured['auth'] = self.headers.get('Authorization')
            captured['payload'] = payload

            if 'bad' in payload.get('model', ''):
                body = json_mod.dumps({"error": {"message": "Model Not Exist"}}).encode()
                self.send_response(404)
            elif 'deny' in payload.get('model', ''):
                body = json_mod.dumps({"error": {"message": "Authentication Fails"}}).encode()
                self.send_response(401)
            else:
                body = json_mod.dumps({
                    "model": payload.get("model"),
                    "choices": [{"message": {"role": "assistant", "content": "命令已生成"}}],
                    "usage": {"total_tokens": 12},
                }, ensure_ascii=False).encode()
                self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    th.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}/v1"
        client = LLMClient(provider="deepseek", api_key="sk-test",
                           model="deepseek-chat", base_url=base)
        check("LLM: 配置完整时 configured=True", client.configured is True)
        reply = client.chat(system="你是网络工程师", messages=[
            {"role": "user", "content": "分析输出"}], max_tokens=64, temperature=0.2)
        check("LLM: 解析出回复", reply == "命令已生成", repr(reply))
        check("LLM: 请求路径正确", captured.get('path') == "/v1/chat/completions",
              str(captured.get('path')))
        check("LLM: 带 Bearer 认证",
              captured.get('auth') == "Bearer sk-test", str(captured.get('auth')))
        payload = captured.get('payload') or {}
        check("LLM: system 作为首条消息",
              payload.get("messages", [{}])[0].get("role") == "system",
              repr(payload.get("messages", [])[:1]))
        check("LLM: 传递模型与生成参数",
              payload.get("model") == "deepseek-chat"
              and payload.get("max_tokens") == 64
              and payload.get("temperature") == 0.2, repr(
                  {k: payload.get(k) for k in ("model", "max_tokens", "temperature")}))
        check("LLM: 非流式请求", payload.get("stream") is False)

        # 模型名错误 → 404 → 提示里要说清
        bad = LLMClient(provider="deepseek", api_key="sk-test",
                        model="bad-model", base_url=base)
        try:
            bad.chat(system="s", messages=[{"role": "user", "content": "hi"}])
            check("LLM: 404 应报错", False)
        except LLMError as e:
            check("LLM: 404 提示含模型/地址", "模型名" in str(e) or "接口地址" in str(e), str(e))

        # 密钥无效 → 401
        deny = LLMClient(provider="deepseek", api_key="sk-bad",
                         model="deny-model", base_url=base)
        try:
            deny.chat(system="s", messages=[{"role": "user", "content": "hi"}])
            check("LLM: 401 应报错", False)
        except LLMError as e:
            check("LLM: 401 提示密钥无效", "API Key" in str(e), str(e))

        # 网络不可达
        offline = LLMClient(provider="deepseek", api_key="sk-test",
                            model="deepseek-chat", base_url="http://127.0.0.1:9/v1")
        try:
            offline.chat(system="s", messages=[{"role": "user", "content": "hi"}])
            check("LLM: 不可达应报错", False)
        except LLMError as e:
            check("LLM: 不可达时给出可读提示",
                  any(mark in str(e) for mark in ("网络", "网关", "连接")), str(e))
    finally:
        server.shutdown()
        server.server_close()

    # 空消息兜底
    check("LLM: 空消息不崩",
          LLMClient._clean_messages([]) == [{"role": "user", "content": "你好"}])


def test_ai_provider_settings():
    """提供方切换时密钥/模型/地址各存各的"""
    settings, snapshot = _settings_snapshot()
    try:
        settings.set("provider_keys", {})
        settings.set("provider_configs", {})

        settings.ai_provider = "deepseek"
        settings.set_api_key_for("deepseek", "sk-ds-1")
        settings.set_provider_config("deepseek", model="deepseek-reasoner",
                                     base_url="https://api.deepseek.com")
        check("提供方: 读取当前密钥", settings.api_key == "sk-ds-1", settings.api_key)
        check("提供方: 读取模型", settings.model == "deepseek-reasoner", settings.model)
        check("提供方: 读取地址",
              settings.api_base == "https://api.deepseek.com", settings.api_base)

        settings.ai_provider = "anthropic"
        settings.set_api_key_for("anthropic", "sk-ant-1")
        settings.set_provider_config("anthropic", model="claude-sonnet-4-20250514",
                                     base_url="")
        check("提供方: 切换后密钥互不覆盖",
              settings.api_key == "sk-ant-1"
              and settings.get_api_key_for("deepseek") == "sk-ds-1",
              f"{settings.api_key} / {settings.get_api_key_for('deepseek')}")
        check("提供方: 切回 DeepSeek 配置还在",
              settings.get_provider_config("deepseek")["model"] == "deepseek-reasoner",
              repr(settings.get_provider_config("deepseek")))
        check("提供方: 未配置的用默认值",
              settings.get_provider_config("openai_compat")["model"] == "gpt-4o-mini",
              repr(settings.get_provider_config("openai_compat")))
    finally:
        _settings_restore(settings, snapshot)

    # AIAgent 会按提供方建客户端
    from core.ai_agent import AIAgent
    agent = AIAgent(api_key="sk-ds-2", provider="deepseek")
    check("AIAgent: 提供方生效", agent.provider == "deepseek")
    check("AIAgent: 默认模型按提供方取", agent.model == "deepseek-chat", agent.model)
    check("AIAgent: 客户端地址正确",
          agent.llm.base_url == "https://api.deepseek.com", agent.llm.base_url)
    check("AIAgent: 描述信息含提供方",
          "DeepSeek" in agent.describe_model(), agent.describe_model())

    agent.set_provider("anthropic", api_key="sk-ant-x")
    check("AIAgent: 切换提供方",
          agent.provider == "anthropic" and agent.model.startswith("claude"),
          f"{agent.provider}/{agent.model}")
    agent.set_provider("deepseek", model="deepseek-reasoner")
    check("AIAgent: 切换时指定模型",
          agent.model == "deepseek-reasoner" and agent.provider == "deepseek")


# ------------------------------------------------------------------ 设备库 + 对话
def test_vault_and_converse():
    """设备库 Vault 持久化 + AIAgent.converse 对话解析（不连真机、不调 AI）"""
    from config.settings import Settings
    from core.ai_agent import AIAgent

    settings, snapshot = _settings_snapshot()
    try:
        settings.set("vault_devices", [])
        # 设备库增删改查
        d1 = {"name": "核心", "protocol": "SSH", "host": "10.0.0.1", "port": 22,
              "vendor": "h3c"}
        check("Vault: 添加设备", settings.add_vault_device(d1) is True)
        check("Vault: 列表含 1 台", len(settings.get_vault_devices()) == 1)
        # 同 host+port 去重，但允许改名
        check("Vault: 同键去重不新增",
              settings.add_vault_device({"name": "核心改名", "protocol": "SSH",
                                        "host": "10.0.0.1", "port": 22}) is False)
        check("Vault: 改名已更新",
              settings.get_vault_devices()[0]["name"] == "核心改名")
        check("Vault: 删除生效",
              settings.remove_vault_device({"protocol": "SSH", "host": "10.0.0.1",
                                             "port": 22}) is True
              and len(settings.get_vault_devices()) == 0)
        check("Vault: 不存在的删除返回 False",
              settings.remove_vault_device({"protocol": "SSH", "host": "9.9.9.9",
                                            "port": 1}) is False)

        # converse：把“说明 + bash 代码块命令”解析为 (文本, 命令)
        agent = AIAgent(api_key="x", provider="deepseek")
        captured = {}

        class _FakeLLM:
            def chat(self, system, messages, max_tokens=900, temperature=0.4):
                captured["system"] = system
                captured["messages"] = messages
                return ("建议先查版本再列接口：\n"
                        "```bash\n"
                        "display version\n"
                        "display interface brief\n"
                        "```")
            def describe(self):
                return "FakeLLM"

        agent.llm = _FakeLLM()
        text, cmds = agent.converse("检查设备的版本和接口", device_output="<输出>",
                                    task="巡检")
        check("converse: 说明文本被解析", "建议先查版本" in text, repr(text))
        check("converse: 命令被解析",
              cmds == ["display version", "display interface brief"], repr(cmds))
        check("converse: 带设备输出上下文",
              any("<输出>" in m.get("content", "") for m in captured["messages"]))
        check("converse: 用对话式提示词", "Catty" in captured["system"],
              captured.get("system", "")[:30])
        # 多轮上下文累计
        agent.llm.chat = lambda **k: "好的，已记录。"
        t2, c2 = agent.converse("那 VLAN 呢")
        check("converse: 多轮上下文保留前文",
              len(agent.context) >= 4 and t2 and c2 == [], repr((t2, c2)))
        # 纯文本回复（无命令）
        agent.llm.chat = lambda **k: "这是一台 H3C 交换机，版本正常，无需操作。"
        t3, c3 = agent.converse("有什么问题吗")
        check("converse: 纯文本无命令", t3 and c3 == [], repr((t3, c3)))
    finally:
        _settings_restore(settings, snapshot)


if __name__ == '__main__':
    sys.exit(main())
