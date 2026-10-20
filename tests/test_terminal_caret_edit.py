"""回归：行内编辑（光标移动后删字/插字不能跑到行尾去删）

用户反馈（2026-10-20，console 连锐捷串口）：
    "console 连接的时候无法删除命令，删除时移动光标到其它位置后删除会回到
     末尾进行删除"
根因：终端把输入当成"只能从末尾追加/退格"的字符串，``_echo_backspace()``
一律 ``movePosition(End)`` 再删 —— 光标在行中间时，删掉的却是行尾那个字符。

跑法： python tests/test_terminal_caret_edit.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QKeyEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ui.terminal_widget import TerminalWidget  # noqa: E402

app = QApplication.instance() or QApplication([])

_failures = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


def press(widget, key, text="", mods=Qt.KeyboardModifier.NoModifier):
    widget.handle_key_press(QKeyEvent(QKeyEvent.Type.KeyPress, key, mods, text))


def type_text(widget, text):
    for ch in text:
        press(widget, getattr(Qt.Key, "Key_" + ch.upper()) if ch.isalpha() else Qt.Key.Key_unknown,
              ch)


def new_widget(sid, prompt="Ruijie#", local_echo=True):
    """造一个"已经连上、设备打完提示符"的终端"""
    w = TerminalWidget(sid)
    w.set_local_echo(local_echo)
    w.append_output(prompt)
    w._learn_prompt()
    return w


def caret_col(w):
    return w.text_edit.textCursor().positionInBlock()


def test_backspace_at_middle():
    w = new_widget(101)
    sent = []
    w.raw_input.connect(sent.append)
    type_text(w, "show clock")
    check("普通输入：缓冲正确", w.command_buffer == "show clock", repr(w.command_buffer))
    check("普通输入：屏幕末行", w._display_last_line() == "Ruijie#show clock",
          repr(w._display_last_line()))
    check("普通输入：光标在末尾", caret_col(w) == len("Ruijie#show clock"), str(caret_col(w)))
    check("普通输入：只发了字符", "".join(sent) == "show clock", repr("".join(sent)))

    # 光标左移 5 格 → "show |clock"
    for _ in range(5):
        press(w, Qt.Key.Key_Left)
    check("左移 5 格：光标落在输入段内", caret_col(w) == len("Ruijie#show "), str(caret_col(w)))
    check("左移 5 格：光标不跨进提示符", caret_col(w) >= len("Ruijie#"), str(caret_col(w)))

    # 退格：删光标左边的空格（"show clock" → "showclock"），不是删行尾的 k
    sent.clear()
    press(w, Qt.Key.Key_Backspace)
    check("行中退格：缓冲", w.command_buffer == "showclock", repr(w.command_buffer))
    check("行中退格：屏幕同步", w._display_last_line() == "Ruijie#showclock",
          repr(w._display_last_line()))
    check("行中退格：光标不前移", caret_col(w) == len("Ruijie#show"), str(caret_col(w)))
    check("行中退格：发给设备的是『删尾巴 + 补回』", "".join(sent) == "\b" * 6 + "clock",
          repr("".join(sent)))
    w.close()


def test_insert_at_middle():
    w = new_widget(102)
    sent = []
    w.raw_input.connect(sent.append)
    type_text(w, "show clock")
    for _ in range(5):
        press(w, Qt.Key.Key_Left)
    sent.clear()
    type_text(w, "x")
    check("行中插字：缓冲", w.command_buffer == "show xclock", repr(w.command_buffer))
    check("行中插字：屏幕", w._display_last_line() == "Ruijie#show xclock",
          repr(w._display_last_line()))
    check("行中插字：光标跟在插入点后", caret_col(w) == len("Ruijie#show x"), str(caret_col(w)))
    check("行中插字：设备收到『删尾巴 + 新尾巴』", "".join(sent) == "\b" * 5 + "xclock",
          repr("".join(sent)))
    w.close()


def test_delete_key():
    w = new_widget(103)
    sent = []
    w.raw_input.connect(sent.append)
    type_text(w, "show clock")
    for _ in range(5):
        press(w, Qt.Key.Key_Left)         # 光标在 "show |clock"
    sent.clear()
    press(w, Qt.Key.Key_Delete)           # 删右边那个 'c'
    check("Delete：删光标右侧字符", w.command_buffer == "show lock", repr(w.command_buffer))
    check("Delete：屏幕", w._display_last_line() == "Ruijie#show lock",
          repr(w._display_last_line()))
    # old="show clock" / new="show lock"：公共前缀 "show " 5 个，尾巴要重发 5 个退格 + "lock"
    check("Delete：设备同步", "".join(sent) == "\b" * 5 + "lock", repr("".join(sent)))
    w.close()


def test_edges():
    w = new_widget(104)
    sent = []
    w.raw_input.connect(sent.append)
    type_text(w, "en")

    for _ in range(10):
        press(w, Qt.Key.Key_Home)          # Home：到输入段开头
    check("Home：光标在输入段最前", caret_col(w) == len("Ruijie#"), str(caret_col(w)))
    sent.clear()
    press(w, Qt.Key.Key_Backspace)         # 已经在最前：一个字节都不发
    check("最前面退格：不删缓冲", w.command_buffer == "en", repr(w.command_buffer))
    check("最前面退格：不发退格字节", "".join(sent) == "", repr("".join(sent)))
    check("最前面退格：提示符没被吃掉", w._display_last_line() == "Ruijie#en",
          repr(w._display_last_line()))

    press(w, Qt.Key.Key_End)
    check("End：光标回到末尾", caret_col(w) == len("Ruijie#en"), str(caret_col(w)))

    # 行尾退格仍然是"发一个 \b"（老行为不变）
    sent.clear()
    press(w, Qt.Key.Key_Backspace)
    check("行尾退格：缓冲", w.command_buffer == "e", repr(w.command_buffer))
    check("行尾退格：只发一个退格", "".join(sent) == "\b", repr("".join(sent)))

    # 空行退格：不发字节（否则会吃掉提示符）
    press(w, Qt.Key.Key_Backspace)
    sent.clear()
    press(w, Qt.Key.Key_Backspace)
    check("空行退格：不发字节", "".join(sent) == "", repr("".join(sent)))

    # 回车后光标归零，可直接开始下一行
    press(w, Qt.Key.Key_Return)
    check("回车：缓冲清空且光标归位",
          w.command_buffer == "" and w._caret == 0,
          repr((w.command_buffer, w._caret)))
    w.close()


def test_device_echo_does_not_move_caret():
    """设备回显（local_echo=False）时，光标不该被回显顶回末尾"""
    w = new_widget(105, local_echo=False)
    w.append_output("Ruijie#")
    for ch in "show clock":
        press(w, Qt.Key.Key_S, ch)
        w.append_output(ch)                # 设备逐字符回显
    check("回显模式：缓冲跟上设备", w.command_buffer == "show clock", repr(w.command_buffer))
    for _ in range(5):
        press(w, Qt.Key.Key_Left)
    before = caret_col(w)
    w.append_output("")                    # 再来一批（空）也不该动光标
    check("回显模式：空输出不动光标", caret_col(w) == before, f"{before} -> {caret_col(w)}")
    w.close()


def test_device_erase_echo():
    """锐捷把"擦掉一个字符"回显成 "\\b \\b"（退格-空格-退格），那个空格不是内容

    真机（COM5 / S2928G-E）行内插字后屏上多出一个空格：'Ruijie>show clo ck'。
    """
    from core.terminal import TerminalBuffer
    buf = TerminalBuffer()
    buf.feed("Ruijie>show clok")
    buf.feed("\b \b")
    check("擦除回显 \\b \\b：只吃掉那一个字符",
          buf.last_line() == "Ruijie>show clo", repr(buf.last_line()))

    buf2 = TerminalBuffer()
    buf2.feed("Ruijie>show clok")
    buf2.feed("\b ")          # 串口把 "\b \b" 拆成两包
    buf2.feed("\b")
    check("擦除回显拆包：跨包也能拼成一次擦除",
          buf2.last_line() == "Ruijie>show clo", repr(buf2.last_line()))

    # 界面级：真机那条"行中插字"的完整字节流（new_widget 已经打过提示符）
    w = new_widget(106, local_echo=False)
    for ch in "show clok":
        press(w, Qt.Key.Key_S if ch.isalpha() else Qt.Key.Key_unknown, ch)
        w.append_output(ch)                    # 设备逐字符回显
    press(w, Qt.Key.Key_Left)                  # 光标挪到 "show clo|k"
    press(w, Qt.Key.Key_C, "c")                # 行中插 c
    w.append_output("\b \b")                   # 设备回显这次擦除
    w.append_output("ck")                      # 设备回显补回的尾巴
    check("擦除回显：行中插字后屏幕上没有多余空格",
          w._display_last_line() == "Ruijie#show clock", repr(w._display_last_line()))
    w.close()


def main():
    test_backspace_at_middle()
    test_insert_at_middle()
    test_delete_key()
    test_edges()
    test_device_echo_does_not_move_caret()
    test_device_erase_echo()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + ", ".join(_failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
