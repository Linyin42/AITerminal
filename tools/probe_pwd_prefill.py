# -*- coding: utf-8 -*-
"""探针：新建连接对话框的「密码」框在**已有上次保存值**时，键盘输入会怎样。

背景：2026-10-21 全量键鼠测试 gui_drive_1.log 里
    [FAIL] 密码按键盘输入进去了   19 位
19 位 = 上次连接里存的密码长度（解码后 19），而测试敲的是 10 位 →
怀疑"点进密码框 + 键盘输入"被整段吞掉（或输入进了别的控件）。

本探针只做界面层复现，不连设备、不写配置。
跑法：python -u tools\\probe_pwd_prefill.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from PyQt6.QtCore import Qt                                   # noqa: E402
from PyQt6.QtTest import QTest                                 # noqa: E402
from PyQt6.QtWidgets import QApplication, QLineEdit           # noqa: E402

app = QApplication.instance() or QApplication([])

from ui.connect_dialog import ConnectDialog                   # noqa: E402

TYPED = "Huawei@123"


def report(tag, dlg):
    print(f"[{tag}] 密码框内容 len={len(dlg.password_input.text())} "
          f"text={dlg.password_input.text()!r}")
    print(f"[{tag}] 焦点控件 = {type(app.focusWidget()).__name__} "
          f"({app.focusWidget().objectName() if app.focusWidget() else '-'})")
    print(f"[{tag}] 密码框 hasFocus={dlg.password_input.hasFocus()} "
          f"readOnly={dlg.password_input.isReadOnly()} "
          f"enabled={dlg.password_input.isEnabled()} "
          f"visible={dlg.password_input.isVisible()}")


def pump(ms=120):
    QTest.qWait(ms)
    app.processEvents()


dlg = ConnectDialog()
dlg.show()
pump(300)
print("协议 =", dlg.protocol_combo.currentText())
print("预填密码 =", repr(dlg.password_input.text()),
      f"len={len(dlg.password_input.text())}")
report("初始", dlg)

# ① 真实测试里的动作：鼠标点密码框 → 键盘逐字敲
QTest.mouseClick(dlg.password_input, Qt.MouseButton.LeftButton)
pump(150)
report("点完密码框", dlg)
QTest.keyClicks(dlg.password_input, TYPED)
pump(150)
report("敲完密码", dlg)

# ② 反向对照：清空后同样的动作
dlg.password_input.clear()
pump(100)
QTest.mouseClick(dlg.password_input, Qt.MouseButton.LeftButton)
pump(100)
QTest.keyClicks(dlg.password_input, TYPED)
pump(100)
report("清空后重敲", dlg)

# ③ 用户视角的期望：点进已有内容的密码框，能否整段替换？
dlg.password_input.setText("saved-password-19ch"[:19])
pump(100)
QTest.mouseClick(dlg.password_input, Qt.MouseButton.LeftButton)
pump(100)
print("[③] 点击后 selectedText len =",
      len(dlg.password_input.selectedText()),
      "cursorPos =", dlg.password_input.cursorPosition())

dlg.close()
print("\n结论行：上面「敲完密码」若仍是 19 位 ⇒ 键盘输入被吞，是真 bug。")
