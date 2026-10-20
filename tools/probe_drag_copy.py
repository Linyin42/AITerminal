#!/usr/bin/env python
"""终端拖拽/复制行为验证（离屏，不碰真机）。

要验证的东西（都是用户实测反馈出来的）：
  T1 拖动框选时，插入光标宽度 = 0（不跟着鼠标跑）
  T2 松开后光标宽度恢复，选区还在（可以复制）
  T3 按在已有选区里拖动 → 旧选区先被清掉（不会拖出"文字幽灵"）
  T4 Ctrl+Shift+C 复制选中内容，且**不**触发中断
  T5 Ctrl+C 仍然触发中断（终端语义不能被复制功能抢走）
  T6 点击空白处 → 光标回到输入位置、选区消失

用法：QT_QPA_PLATFORM=offscreen python tools\\probe_drag_copy.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt          # noqa: E402
from PyQt6.QtGui import QGuiApplication, QKeyEvent, QMouseEvent  # noqa: E402
from PyQt6.QtTest import QTest                                # noqa: E402
from PyQt6.QtWidgets import QApplication                      # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("  " + detail if detail else ""))


def send_move(widget, pos: QPoint) -> None:
    """带按键状态的鼠标移动（QTest.mouseMove 不带按钮，模拟不了拖动）"""
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), QPointF(widget.mapToGlobal(pos)),
                     Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, ev)


def point_of(te, text: str) -> QPoint:
    """文档里某段文字的屏幕坐标（用它当拖动起点/终点）"""
    cur = te.document().find(text)
    return te.cursorRect(cur).center()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    app = QApplication(sys.argv)
    from ui.terminal_widget import TerminalWidget

    w = TerminalWidget(1)
    w.resize(900, 400)
    for i in range(8):
        w.append_output(f"line {i}: hello world 0123456789 abcdef\r\n")
    w.show()
    app.processEvents()
    te = w.text_edit
    vp = te.viewport()
    caret_w = te._caret_width
    print(f"控件就绪：块光标宽度={caret_w}  文本行数={te.document().blockCount()}")

    p1 = point_of(te, "line 1")
    p2 = point_of(te, "line 4")

    # ---- T1/T2 拖动框选
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
    app.processEvents()
    width_during = te.cursorWidth()
    for step in range(1, 6):
        send_move(vp, QPoint(p1.x() + (p2.x() - p1.x()) * step // 5,
                             p1.y() + (p2.y() - p1.y()) * step // 5))
        app.processEvents()
    selected_during = te.textCursor().selectedText().replace("\u2029", "\n")
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p2)
    app.processEvents()
    width_after = te.cursorWidth()
    selected_after = te.textCursor().selectedText().replace("\u2029", "\n")

    record("T1 拖动中插入光标不显示（宽度 0）", width_during == 0, f"拖动中宽度={width_during}")
    record("T2 松开后恢复宽度且选区还在",
           width_after == caret_w and bool(selected_after.strip()),
           f"宽度={width_after} 选中 {len(selected_after)} 字符：{selected_after.strip()[:40]!r}")

    # ---- T3 按在已有选区里再拖 → 旧选区先清掉
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     point_of(te, "line 2"))
    app.processEvents()
    cleared_at_press = not te.textCursor().hasSelection()
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       point_of(te, "line 3"))
    app.processEvents()
    record("T3 按在选区里拖动会先清掉旧选区（不拖出幽灵）",
           cleared_at_press, f"按下瞬间 hasSelection={not cleared_at_press}")

    # ---- T4/T5 复制与中断
    interrupted: list[int] = []
    w.interrupt_requested.connect(lambda: interrupted.append(1))

    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
    send_move(vp, QPoint(p2.x(), p2.y()))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p2)
    app.processEvents()
    expect = te.textCursor().selectedText().replace("\u2029", "\n")

    QGuiApplication.clipboard().setText("(before)")
    ev_copy = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C,
                        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    w.handle_key_press(ev_copy)
    app.processEvents()
    copied = QGuiApplication.clipboard().text()
    record("T4 Ctrl+Shift+C 复制了选中内容且不打断设备",
           bool(copied.strip()) and copied == expect and not interrupted,
           f"剪贴板 {len(copied)} 字符，中断次数={len(interrupted)}")

    ev_int = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    w.handle_key_press(ev_int)
    app.processEvents()
    record("T5 Ctrl+C 仍然是打断设备", len(interrupted) == 1, f"中断次数={len(interrupted)}")

    # ---- T6 点空白处：选区消失、光标回到末尾
    blank = QPoint(vp.width() - 40, vp.height() - 30)
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, blank)
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, blank)
    app.processEvents()
    cur = te.textCursor()
    record("T6 点空白处：选区清空、光标回到输入位置",
           not cur.hasSelection() and cur.position() == te.document().characterCount() - 1,
           f"选区={cur.hasSelection()} 光标={cur.position()}/末尾={te.document().characterCount() - 1}")

    ok_n = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n结论：{ok_n}/{len(RESULTS)} 通过")
    w.close()
    return 0 if ok_n == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
