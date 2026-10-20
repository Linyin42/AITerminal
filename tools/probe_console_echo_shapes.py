"""复演用户截图的场景：锐捷 console 上敲 "show run"，看屏幕/缓冲/自动提示三处

用户 2026-10-19 截图：
    Ruijie(config)#sshhooww
    浮层提示：sshhooww rruunnn
    % Incomplete command
即"每个字符被画两遍，提示也跟着按错的缓冲算"。

这个脚本不靠肉眼：逐键复演，并在每一步测量
  ① 屏幕上那一行  ② command_buffer  ③ 提示浮层这次是按什么前缀算的
对三种设备回显形态各跑一遍（逐字回 / 整段回 / 带换行回），任何一种坏了都要修。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                        # noqa: E402
from PyQt6.QtCore import Qt                                     # noqa: E402
from PyQt6.QtGui import QKeyEvent                               # noqa: E402

app = QApplication.instance() or QApplication([])
FAILS = []


def check(name, condition, detail=""):
    print(("  [PASS] " if condition else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not condition:
        FAILS.append(name)


KEYS = {" ": (Qt.Key.Key_Space, " ")}


def key_for(ch):
    if ch == " ":
        return Qt.Key.Key_Space
    return getattr(Qt.Key, f"Key_{ch.upper()}")


def replay(title, echo_shape, command="show run"):
    print(f"\n[{title}] 命令 {command!r}  回显形态={echo_shape}")
    from ui.terminal_widget import TerminalWidget

    widget = TerminalWidget(101)
    widget.append_output("Ruijie(config)#")      # 设备提示符
    widget.set_local_echo(True)                  # 旧代码误判"设备不回显"后的状态

    seen_prefixes = []
    original_suggest = widget.dictionary.suggest

    def spy(prefix):
        seen_prefixes.append(prefix)
        return original_suggest(prefix)

    widget.dictionary.suggest = spy

    for ch in command:
        event = QKeyEvent(QKeyEvent.Type.KeyPress, key_for(ch),
                          Qt.KeyboardModifier.NoModifier, ch)
        widget.handle_key_press(event)
        if echo_shape == "per_char":
            widget.append_output(ch)             # 设备逐字回显（真实 console 节奏）
        elif echo_shape == "whole":
            pass                                 # 整段回显放到最后

    if echo_shape == "whole":
        widget.append_output(command)
    elif echo_shape == "with_crlf":
        widget.append_output(command + "\r\n")

    line = widget.screen.last_line()
    # ⚠ 判据用"最后一行**非空**内容"：设备整段回显带 \r\n 时，光标停在新的一行，
    #   last_line() 本来就是 ''（命令在上一行）。拿 last_line().endswith() 判会把
    #   好代码误报成坏代码 —— 这个坑这个脚本自己踩过一次。
    shown = [ln for ln in widget.text().splitlines() if ln.strip()]
    last_shown = shown[-1] if shown else ""
    print(f"  屏幕最后一行: {line!r}（最后一段非空: {last_shown!r}）")
    print(f"  command_buffer: {widget.command_buffer!r}")
    print(f"  提示浮层算过的前缀: {seen_prefixes[-3:]}")
    print(f"  local_echo: {widget.local_echo}")
    print(f"  浮层当前候选: {[widget.hint_popup.item(i).text() for i in range(widget.hint_popup.count())][:3] if hasattr(widget.hint_popup, 'count') else '(n/a)'}")

    check(f"{title}: 屏幕上没有双字", "sshhooww" not in last_shown
          and last_shown.endswith(command), repr(last_shown))
    check(f"{title}: 输入缓冲就是命令原文", widget.command_buffer == command,
          repr(widget.command_buffer))
    check(f"{title}: 自动提示按原文算前缀（不按重字算）",
          bool(seen_prefixes) and all(p == command[:len(p)] for p in seen_prefixes)
          and seen_prefixes[-1] in (command, command[:-1], ""),
          f"最后一次前缀 {seen_prefixes[-1] if seen_prefixes else None!r}")
    check(f"{title}: 设备一证明会回显就撤掉本地回显", widget.local_echo is False,
          f"local_echo={widget.local_echo}")
    widget.close()


if __name__ == "__main__":
    replay("逐字回显", "per_char")
    replay("整段回显", "whole")
    replay("整段回显+换行", "with_crlf")
    replay("不重字（空格+短命令）", "per_char", command="sh run")

    print()
    if FAILS:
        print(f"[FAIL] {len(FAILS)} 项: {FAILS}")
        sys.exit(1)
    print("[PASS] 三种回显形态下都不重字、缓冲不错、提示按原文算")
