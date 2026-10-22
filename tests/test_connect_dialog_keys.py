# -*- coding: utf-8 -*-
"""新建连接对话框的**键盘行为**回归：回车别提交、预填密码别拼接。

现场（2026-10-21 `%TEMP%\\gui_drive_1.log`，G3 三条红了）：
  · 鼠标点协议下拉 + 键盘选到 eNSP Pro + 按回车 ⇒ 弹「参数不完整」
    （QComboBox 不认回车，事件冒泡到 QDialog，被当成默认按钮"连接"）
  · 上次"记住该连接"存了 9 位密码，新密码 10 位敲进去 ⇒ 框里 19 位
    （密码全是圆点，用户看不出两段拼接，连上去只会密码错误）

跑法：python tests\\test_connect_dialog_keys.py
判据：
  ① 下拉框上的回车**不关闭**对话框、不弹任何警告框，且下拉项确实已选定
  ② 预填密码后直接敲字符 ⇒ 内容变成"新敲的那串"（整段替换）
  ③ 预填密码后先按 ← 再敲 ⇒ 是插入（不误伤"我要改一个字"的正常编辑）
"""
import base64
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from PyQt6.QtCore import Qt                                    # noqa: E402
from PyQt6.QtTest import QTest                                 # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox          # noqa: E402

app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []
OLD_PWD = "admin@123"          # 上次"记住该连接"留下的旧密码（9 位）
NEW_PWD = "Huawei@123"         # 这次敲的新密码（10 位）


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""),
          flush=True)


def pump(sec):
    end = __import__("time").monotonic() + sec
    while __import__("time").monotonic() < end:
        app.processEvents()
        QTest.qWait(10)


# ---- 用一份**假的**配置当"上次连接"，绝不碰用户真实 settings.json
tmp_dir = tempfile.mkdtemp(prefix="ait-connkeys-")
cfg_path = os.path.join(tmp_dir, "settings.json")
with open(cfg_path, "w", encoding="utf-8") as fh:
    json.dump({"last_connection": {
        "name": "eNSP", "protocol": "eNSP", "host": "192.168.31.11", "port": 8443,
        "username": "", "remember": True,
        "password": "b64:" + base64.b64encode(OLD_PWD.encode()).decode("ascii"),
    }}, fh, ensure_ascii=False)

import ui.connect_dialog as cd                                  # noqa: E402
from config.settings import Settings as RealSettings           # noqa: E402

cd.Settings = lambda *a, **kw: RealSettings(config_file=cfg_path)

WARNS = []


def fake_warning(parent, title, text, *a, **kw):
    WARNS.append((title, text))
    return QMessageBox.StandardButton.Ok


QMessageBox.warning = staticmethod(fake_warning)

dlg = cd.ConnectDialog()
dlg.show()
pump(0.3)

print("=== ① 下拉框上按回车，不许提交对话框 ===")
check("对话框带着上次的 eNSP Pro 打开", dlg.protocol_combo.currentText() == "eNSP Pro",
      dlg.protocol_combo.currentText())
check("预填了上次的旧密码", dlg.password_input.text() == OLD_PWD,
      f"len={len(dlg.password_input.text())}")

QTest.mouseClick(dlg.protocol_combo, Qt.MouseButton.LeftButton)
pump(0.2)
QTest.keyClick(dlg.protocol_combo, Qt.Key.Key_Home)
QTest.keyClick(dlg.protocol_combo, Qt.Key.Key_Return)
pump(0.3)
check("协议下拉框回车后对话框还开着", dlg.isVisible())
check("回车没有触发校验警告", not WARNS, str(WARNS))
check("协议确实选成了 SSH", dlg.protocol_combo.currentText() == "SSH",
      dlg.protocol_combo.currentText())

# 切回 eNSP Pro，供后面看密码
dlg.protocol_combo.setCurrentText("eNSP Pro")
pump(0.2)

print("\n=== ② 预填密码 + 直接敲 ⇒ 整段替换 ===")
QTest.mouseClick(dlg.password_input, Qt.MouseButton.LeftButton)
pump(0.1)
QTest.keyClicks(dlg.password_input, NEW_PWD)
pump(0.1)
check("敲完就是新密码（不是两段拼接）", dlg.password_input.text() == NEW_PWD,
      f"{dlg.password_input.text()!r}（len={len(dlg.password_input.text())}）")

print("\n=== ③ 先按 ← 再敲 ⇒ 正常插入（别把编辑功能弄坏）===")
dlg.password_input.setText(OLD_PWD)
pump(0.05)
QTest.mouseClick(dlg.password_input, Qt.MouseButton.LeftButton)
pump(0.05)
QTest.keyClick(dlg.password_input, Qt.Key.Key_Left)
QTest.keyClicks(dlg.password_input, "X")
pump(0.05)
check("按过方向键后是插入而不是替换",
      dlg.password_input.text() == OLD_PWD[:-1] + "X" + OLD_PWD[-1],
      repr(dlg.password_input.text()))

print("\n=== ④ 预填密码 + 粘贴 ⇒ 也是整段替换 ===")
dlg.password_input.setText(OLD_PWD)
pump(0.05)
app.clipboard().setText(NEW_PWD)
QTest.mouseClick(dlg.password_input, Qt.MouseButton.LeftButton)
pump(0.05)
QTest.keyClick(dlg.password_input, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
pump(0.1)
check("粘贴进去的是新密码，不是拼接", dlg.password_input.text() == NEW_PWD,
      repr(dlg.password_input.text()))

print("\n=== ⑤ 设备下拉框（拉取失败后的占位项）回车也不许提交 ===")
WARNS.clear()
dlg.ensp_device_combo.clear()
dlg.ensp_device_combo.addItem("（拉取失败）", None)
QTest.mouseClick(dlg.ensp_device_combo, Qt.MouseButton.LeftButton)
pump(0.2)
QTest.keyClick(dlg.ensp_device_combo, Qt.Key.Key_Return)
pump(0.3)
check("设备下拉框回车后对话框还开着", dlg.isVisible())
check("设备下拉框回车没有触发警告", not WARNS, str(WARNS))

dlg.close()
print("\n" + "-" * 60)
print(f"结果: {len(PASS)} 通过 / {len(FAIL)} 失败（共 {len(PASS) + len(FAIL)} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
