"""冻结包内的拖拽/复制验证（不依赖 PyQt6.QtTest —— 它在 build.spec 里被排除）。

跑法（GUI 包无控制台，所以结果落盘 + 退出码）:
    NetOpsTerminal.exe --script C:\\aitprobe\\probe_drag_frozen.py
结果: C:\\aitprobe\\drag_frozen.txt

检查项与 probe_drag_copy.py 一一对应，只是把 QTest.mousePress/mouseMove 换成直接构造 QMouseEvent：
  F1 拖动中插入光标宽度 0
  F2 松开后恢复宽度且选区仍在
  F3 Ctrl+Shift+C 复制选中内容，且不触发中断
  F4 Ctrl+C 触发中断
"""
import os
import sys

# 源码方式跑时把项目根放进 sys.path；冻结包里 ui 已在 sys.path 上，这行只是兜底。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QGuiApplication, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication

OUT = r"C:\aitprobe\drag_frozen.txt"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))


def _send(w, typ, pos, buttons, button):
    QApplication.sendEvent(w, QMouseEvent(
        typ, QPointF(pos), QPointF(w.mapToGlobal(pos)),
        button, buttons, Qt.KeyboardModifier.NoModifier))


def press(vp, pos):
    _send(vp, QEvent.Type.MouseButtonPress, pos,
          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)


def move(vp, pos):
    _send(vp, QEvent.Type.MouseMove, pos,
          Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)


def release(vp, pos):
    _send(vp, QEvent.Type.MouseButtonRelease, pos,
          Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)


def key(w, k, mods=Qt.KeyboardModifier.NoModifier):
    QApplication.sendEvent(w, QKeyEvent(QEvent.Type.KeyPress, k, mods))


def point_of(te, text):
    cur = te.document().find(text)
    return te.cursorRect(cur).center()


def main():
    app = QApplication(sys.argv)
    from ui.terminal_widget import TerminalWidget

    interrupted = {"n": 0}
    w = TerminalWidget(1)
    w.resize(900, 400)
    for i in range(8):
        w.append_output(f"line {i}: hello world 0123456789 abcdef\r\n")
    w.show()
    w.interrupt_requested.connect(lambda: interrupted.__setitem__("n", interrupted["n"] + 1))
    app.processEvents()

    te = w.text_edit
    vp = te.viewport()
    base_width = te.cursorWidth()
    rec("F0 初始光标宽度非 0", base_width > 0, f"宽度={base_width}")

    p1 = point_of(te, "line 1")
    p2 = point_of(te, "line 4")

    press(vp, p1)
    app.processEvents()
    widths = [te.cursorWidth()]
    for step in range(1, 6):
        pos = QPoint(p1.x() + (p2.x() - p1.x()) * step // 5,
                     p1.y() + (p2.y() - p1.y()) * step // 5)
        move(vp, pos)
        app.processEvents()
        widths.append(te.cursorWidth())
    rec("F1 拖动中光标宽度为 0", all(v == 0 for v in widths), f"采样={sorted(set(widths))}")

    release(vp, p2)
    app.processEvents()
    rec("F2 松开后恢复宽度", te.cursorWidth() == base_width, f"宽度={te.cursorWidth()}")
    sel = te.textCursor().selectedText()
    rec("F2b 松开后选区仍在", len(sel) > 10, f"选中 {len(sel)} 字符")

    QGuiApplication.clipboard().clear()
    key(te, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    app.processEvents()
    clip = QGuiApplication.clipboard().text()
    rec("F3 Ctrl+Shift+C 复制成功", len(clip) > 10, f"剪贴板 {len(clip)} 字符")
    rec("F3b 复制不触发中断", interrupted["n"] == 0, f"中断次数={interrupted['n']}")

    key(te, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()
    rec("F4 Ctrl+C 触发中断", interrupted["n"] == 1, f"中断次数={interrupted['n']}")

    passed = sum(1 for _, ok, _ in results if ok)
    lines = [f"{'[PASS]' if ok else '[FAIL]'} {name}  {detail}" for name, ok, detail in results]
    lines.append(f"\n总结: {passed}/{len(results)} 通过")
    text = "\n".join(lines)
    try:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        print("写结果失败:", exc)
    print(text)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
