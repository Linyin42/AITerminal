"""Console 回车/回显（用户 2026-10-17 反馈：「进入后按回车不会回显内容，也没有反应」）。

跑法：python tests\test_console_echo.py
覆盖：① 本地回显开着时，敲的字要看得见、回车要把行断开；
      ② 本地回显关着时，屏幕不由本地改（避免与设备回显重影）；
      ③ Console 连上后设备零输出 → 自动开本地回显；有输出则保持关闭。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QEvent, Qt                          # noqa: E402
from PyQt6.QtGui import QKeyEvent                            # noqa: E402
from PyQt6.QtWidgets import QApplication                     # noqa: E402

app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))


def press(widget, key, text=""):
    widget.handle_key_press(QKeyEvent(QEvent.Type.KeyPress, key,
                                      Qt.KeyboardModifier.NoModifier, text))


from ui.terminal_widget import TerminalWidget                # noqa: E402

print("=== ① 本地回显：敲字可见 + 回车断行 ===")
w = TerminalWidget(1)
sent = []
w.raw_input.connect(lambda t: sent.append(t))
w.set_local_echo(True)
for ch in "abc":
    press(w, getattr(Qt.Key, "Key_" + ch.upper()), ch)
check("敲的 3 个字符都下发给设备", sent == ["a", "b", "c"], str(sent))
check("本地屏幕上看得见 abc", "abc" in w.text(), repr(w.text()))
blocks_before = w.text_edit.document().blockCount()
press(w, Qt.Key.Key_Return, "\r")
check("回车下发了行结束符", sent[-1] == "\r", repr(sent))
check("回车后本地断行（新起一行）",
      w.text_edit.document().blockCount() > blocks_before,
      f"{blocks_before} -> {w.text_edit.document().blockCount()} 段")

print("\n=== ② 本地回显关闭：屏幕不由本地改 ===")
w2 = TerminalWidget(2)
sent2 = []
w2.raw_input.connect(lambda t: sent2.append(t))
for ch in "xy":
    press(w2, getattr(Qt.Key, "Key_" + ch.upper()), ch)
check("仍然逐字符下发（设备端收得到）", sent2 == ["x", "y"], str(sent2))
check("屏幕上没有本地回显的痕迹", "xy" not in w2.text(), repr(w2.text()))

print("\n=== ③ Console 无回显 → 自动开本地回显 ===")
from ui.main_window import MainWindow                        # noqa: E402
from ui.session_tab import SessionTab                        # noqa: E402


class FakeConnector:
    def is_connected(self):
        return True

    def info(self):
        return "COM3 @9600"


class FakeSession:
    def __init__(self, out=""):
        self.connector = FakeConnector()
        self._out = out
        self.name = "假 Console"
        self.protocol = "Serial"
        self.config = {}

    def peek_output(self):
        return self._out


window = MainWindow()
quiet = FakeSession("")
window.session_manager._sessions[77] = quiet          # 设备零输出
window.tabs[77] = SessionTab(77, "假 Console", "COM3 @9600")
window.check_console_echo(77)
check("设备零输出 → 本会话自动开启本地回显",
      window.tabs[77].terminal.local_echo is True)
check("状态条说明原因", "本地回显" in window.tabs[77].info_label.text(),
      window.tabs[77].info_label.text())

chatty = FakeSession("Ruijie#")
window.session_manager._sessions[78] = chatty         # 设备有回显
window.tabs[78] = SessionTab(78, "假 Console", "COM3 @9600")
window.check_console_echo(78)
check("设备有回显 → 保持关闭（不重影）",
      window.tabs[78].terminal.local_echo is False)

print(f"\n=== 合计 PASS {len(PASS)} / FAIL {len(FAIL)} ===")
sys.exit(1 if FAIL else 0)
