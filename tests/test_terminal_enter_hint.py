"""回车与命令提示浮层的交互（2026-10-22 修的那条「回车没反应，得按两次」）。

规范（2026-10-18 用户定的，仿 PyCharm）：浮层提示时第 1 次回车**只采纳候选**，第 2 次才发送。
但「候选与已输入内容一模一样」时根本没有尾巴可补 —— 这时若也吃掉回车，用户看到的就是
按了没反应。修法：这种情况收起浮层、继续走正常发送。

跑法：python tests\\test_terminal_enter_hint.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QEvent, QPoint, Qt                     # noqa: E402
from PyQt6.QtGui import QKeyEvent                                # noqa: E402
from PyQt6.QtWidgets import QApplication                         # noqa: E402

app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("   " + detail) if detail else ""))


def press(widget, key, text=""):
    widget.handle_key_press(QKeyEvent(QEvent.Type.KeyPress, key,
                                      Qt.KeyboardModifier.NoModifier, text))


def type_text(widget, text):
    for ch in text:
        press(widget, getattr(Qt.Key, "Key_" + ch.upper()) if ch.isalpha() else Qt.Key.Key_Space,
              ch)


from ui.terminal_widget import TerminalWidget                     # noqa: E402


def fresh():
    w = TerminalWidget(1)
    w.dictionary.add("show version")
    w.dictionary.add("display version")
    sent = []
    w.raw_input.connect(sent.append)
    return w, sent


print("=== ① 候选与输入相同（没有尾巴可补）→ 回车必须真发 ===")
w, sent = fresh()
type_text(w, "show version")
w.hint_popup.show_candidates(["show version"], QPoint(10, 10))
check("浮层是弹着的", w.hint_popup.is_active())
check("候选与缓冲相同", w.hint_popup.current_command() == w.command_buffer,
      "%r vs %r" % (w.hint_popup.current_command(), w.command_buffer))
n = len(sent)
press(w, Qt.Key.Key_Return, "\r")
check("回车把行结束符发出去了", sent[n:] == ["\r"], str(sent[n:]))
check("浮层收起", not w.hint_popup.is_active())
check("命令进历史", w.command_history == ["show version"], str(w.command_history))
check("缓冲清空", w.command_buffer == "", repr(w.command_buffer))

print("\n=== ② 有尾巴可补 → 第 1 次只补全，第 2 次才发 ===")
w, sent = fresh()
type_text(w, "show vers")
w.hint_popup.show_candidates(["show version"], QPoint(10, 10))
n = len(sent)
press(w, Qt.Key.Key_Return, "\r")
check("第 1 次回车没发行结束符", "\r" not in sent[n:], str(sent[n:]))
check("只把尾巴 'ion' 发给设备（让设备那行与本地一致）",
      sent[n:] == ["ion"], str(sent[n:]))
check("缓冲补成完整命令", w.command_buffer == "show version", repr(w.command_buffer))
check("浮层收起", not w.hint_popup.is_active())
n = len(sent)
press(w, Qt.Key.Key_Return, "\r")
check("第 2 次回车才发", sent[n:] == ["\r"], str(sent[n:]))

print("\n=== ③ 没浮层时回车照发（老行为不能坏） ===")
w, sent = fresh()
type_text(w, "display version")
n = len(sent)
press(w, Qt.Key.Key_Return, "\r")
check("直接发送", sent[n:] == ["\r"], str(sent[n:]))

print("\n=== ④ 小键盘 Enter 与主键盘 Return 等价 ===")
w, sent = fresh()
type_text(w, "show version")
w.hint_popup.show_candidates(["show version"], QPoint(10, 10))
n = len(sent)
press(w, Qt.Key.Key_Enter, "\r")
check("小键盘回车也真发", sent[n:] == ["\r"], str(sent[n:]))

print("\n=== ⑤ 浮层候选比输入短/不相干（脏候选）→ 不许发错尾巴 ===")
w, sent = fresh()
type_text(w, "show vers")
w.hint_popup.show_candidates(["show vers"], QPoint(10, 10))   # 与缓冲相同 ⇒ 没尾巴
n = len(sent)
press(w, Qt.Key.Key_Return, "\r")
check("照样能发出去（不被脏浮层卡住）", sent[n:] == ["\r"], str(sent[n:]))

print("\n结果: %d 通过 / %d 失败（共 %d 项）" % (len(PASS), len(FAIL), len(PASS) + len(FAIL)))
raise SystemExit(0 if not FAIL else 1)
