# -*- coding: utf-8 -*-
"""真机端到端：Console(COM5, 锐捷 Ruijie>) 上验证"回车该发就发"这条修复。

背景：ui/terminal_widget.py 有一处未提交改动 —— 命令提示浮层（hint_popup）弹着、但候选与
已输入内容**完全一样**（没有尾巴可补）时，回车以前会被吞掉，用户看到"按了回车没反应，得按
两次"。改法是这种情况收起浮层、继续走正常发送路径。

两个场景（都发只读命令，不改设备配置）：
  A. 敲完整 `show version`，浮层候选与输入相同 → 回车必须**立刻发给设备**并看到回显；
  B. 敲半截 `show vers`，浮层候选 `show version` → 第 1 次回车**只补全不发**，第 2 次才发。

跑法：python -u tools/probe_console_enter_real.py [COM5] [9600]
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer          # noqa: E402
from PyQt6.QtGui import QKeyEvent                            # noqa: E402
from PyQt6.QtWidgets import QApplication                     # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM5"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 9600

app = QApplication.instance() or QApplication([])

from core.serial_connector import SerialConnector             # noqa: E402
from ui.terminal_widget import TerminalWidget                 # noqa: E402

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


conn = SerialConnector()
if not conn.connect(PORT, baudrate=BAUD, auto_reconnect=False):
    print("打不开 %s：%s" % (PORT, getattr(conn, "last_error", "?")))
    raise SystemExit(2)

sent = []
w = TerminalWidget(1)
w.set_local_echo(False)          # 真机会回显，本地别再回显一遍（否则重影）
w.raw_input.connect(lambda t: (sent.append(t), conn.send_raw(t)))
w.resize(900, 500)
w.show()


def pump(seconds):
    """把串口收到的字节灌进终端，模拟主窗口的读循环。"""
    end = time.time() + seconds
    while time.time() < end:
        chunk = conn.get_output()
        if chunk:
            w.append_output(chunk)
        app.processEvents()
        time.sleep(0.05)


pump(0.6)
print("连接 %s @%d，初始屏幕：%r" % (PORT, BAUD, w.text()[-60:]))

# ---------------------------------------------------------------- 场景 A
print("\n=== A. 完整命令 + 浮层候选与输入相同 → 回车必须真发 ===")
type_text(w, "show version")
pump(0.5)
print("  输入缓冲: %r" % w.command_buffer)
w.hint_popup.show_candidates(["show version"], QPoint(10, 10))
check("浮层是弹着的", w.hint_popup.is_active())
check("候选与已输入内容相同（没有尾巴可补）",
      w.hint_popup.current_command() == w.command_buffer,
      "%r vs %r" % (w.hint_popup.current_command(), w.command_buffer))
n_before = len(sent)
press(w, Qt.Key.Key_Return, "\r")
check("回车后确实往串口发了行结束符", sent[n_before:] == ["\r"], str(sent[n_before:]))
check("浮层已收起", not w.hint_popup.is_active())
check("命令进了历史", "show version" in w.command_history, str(w.command_history[-3:]))
pump(2.5)
screen = w.text()
check("设备回显里看得到 show version", "show version" in screen)
check("拿到了设备输出（含锐捷特征词）",
      any(k in screen for k in ("Ruijie", "RGOS", "Version", "version")),
      repr(screen[-160:]))

# ---------------------------------------------------------------- 场景 B
print("\n=== B. 半截命令 + 浮层有可补的尾巴 → 第 1 次回车只补全 ===")
w.command_buffer = ""
type_text(w, "show vers")
pump(0.3)
w.hint_popup.show_candidates(["show version"], QPoint(10, 10))
check("浮层弹着且有尾巴可补", w.hint_popup.current_command() == "show version")
n_before = len(sent)
press(w, Qt.Key.Key_Return, "\r")
# 采纳时会把差的尾巴（"ion"）发给设备，让设备那一行与本地一致 —— 但**不能发回车**
check("第 1 次回车没发回车符", "\r" not in sent[n_before:], str(sent[n_before:]))
check("只把补全尾巴发给设备", sent[n_before:] == ["ion"], str(sent[n_before:]))
check("缓冲被补成完整命令", w.command_buffer == "show version", repr(w.command_buffer))
press(w, Qt.Key.Key_Return, "\r")
check("第 2 次回车才真正发送", sent[len(sent) - 1:] == ["\r"], str(sent[-1:]))
pump(2.0)
check("第二次也拿到了设备输出", "show vers" in w.text().lower() or "version" in w.text().lower())

conn.disconnect()
print("\n结果: %d 通过 / %d 失败" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项: %s" % FAIL)
raise SystemExit(0 if not FAIL else 1)
