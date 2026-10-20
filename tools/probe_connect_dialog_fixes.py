# -*- coding: utf-8 -*-
"""连接对话框行为探针（离屏，不动用户真实配置）。

覆盖：
  ① 打开即展开（2026-10-17 用户要求删掉「快速预设」后的新行为）
  ② 按协议显隐各行：Console 只留串口；eNSP Pro 收起用户名/密钥文件
  ③ 对话框高度（用户反馈"显示不完"）
  ④ 串口"刷新"按钮崩溃（章五）
  ⑤ 带上次连接打开 / ⑥ 设备库记录注入：直接铺开，不再需要先选预设

跑法：python -u tools\\probe_connect_dialog_fixes.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

FAIL = []
TOTAL = [0]


def check(name, cond, detail=""):
    TOTAL[0] += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAIL.append(name)


from PyQt6.QtWidgets import QApplication, QPushButton                 # noqa: E402

app = QApplication.instance() or QApplication([])

import ui.connect_dialog as cd                                        # noqa: E402
from config.settings import Settings as RealSettings                  # noqa: E402


class FakeSettings(RealSettings):
    """只改「上次连接」：不动用户真实 settings.json（只读它的默认值）"""

    LAST = None

    def get_last_connection(self):
        return FakeSettings.LAST


cd.Settings = FakeSettings
cd.SerialPortScanner.cached = staticmethod(lambda: [])                # 不真去枚举串口
cd.SerialPortScanner.busy = staticmethod(lambda: False)

print("=== ① 打开即展开：快速预设区已删除，表单直接可用 ===")
FakeSettings.LAST = None
dlg = cd.ConnectDialog()
dlg.show()                       # 不 show 的话 isVisible() 恒为 False，断言会看错
app.processEvents()
check("快速预设控件已消失",
      not hasattr(dlg, "preset_combo") and not hasattr(dlg, "preset_tip"))
check("基本信息显示", dlg.basic_group.isVisible())
check("连接参数显示（默认 SSH）", dlg.conn_group.isVisible())
check("串口参数收起", not dlg.serial_group.isVisible())
check("eNSP Pro 分组收起", not dlg.ensp_group.isVisible())
check("「记住该连接」显示", dlg.chk_remember.isVisible())
check("默认协议 SSH", dlg.protocol_combo.currentText() == "SSH",
      dlg.protocol_combo.currentText())
check("SSH 下用户名/密钥文件都在",
      dlg.username_input.isVisible() and dlg.key_input.isVisible())

print("\n=== ② 按协议显隐：只留该协议用得上的行 ===")
dlg.set_protocol("Serial")
app.processEvents()
check("Console：串口参数显示、连接参数隐藏",
      dlg.serial_group.isVisible() and not dlg.conn_group.isVisible(),
      f"协议={dlg.protocol_combo.currentText()}")
dlg.set_protocol("eNSP")
app.processEvents()
check("eNSP Pro：分组显示、端口自动 8443",
      dlg.ensp_group.isVisible() and dlg.port_spin.value() == 8443,
      f"端口={dlg.port_spin.value()}")
check("eNSP Pro：用户名行收起", not dlg.username_input.isVisible())
check("eNSP Pro：密钥文件行收起", not dlg.key_input.isVisible())
check("eNSP Pro：密码行留着（要填设备登录密码）", dlg.password_input.isVisible())
dlg.set_protocol("Telnet")
app.processEvents()
check("Telnet：用户名回来、密钥文件仍藏",
      dlg.username_input.isVisible() and not dlg.key_input.isVisible())

print("\n=== ③ 对话框高度（用户反馈：小屏上显示不完）===")
for proto, limit in (("SSH", 660), ("eNSP", 660), ("Serial", 620), ("Telnet", 660)):
    dlg.set_protocol(proto)
    app.processEvents()
    dlg.adjustSize()
    app.processEvents()
    height = dlg.sizeHint().height()
    check(f"{proto} 下高度 {height} px < {limit}", height < limit, f"{height} px")

print("\n=== ④ 串口「刷新」不再抛异常（章五）===")
dlg.set_protocol("Serial")
app.processEvents()
btn = [b for b in dlg.findChildren(QPushButton) if b.text() == "刷新"]
check("找到刷新建", bool(btn))
try:
    btn[0].click()                                        # 修复前：TypeError → 异常框
    ok, err = True, ""
except Exception as e:                                    # noqa: BLE001
    ok, err = False, f"{type(e).__name__}: {e}"
check("点刷新没有抛异常", ok, err)
check("空结果显示占位项", dlg.serial_port.currentText() == "未检测到串口",
      dlg.serial_port.currentText())

print("\n=== ⑤ 带上次连接打开：直接填好，不用先选预设 ===")
FakeSettings.LAST = {"name": "HCL", "protocol": "Telnet", "host": "127.0.0.1",
                     "port": 30001, "username": "root"}
dlg2 = cd.ConnectDialog()
dlg2.show()
app.processEvents()
check("输入区直接显示", dlg2.basic_group.isVisible() and dlg2.conn_group.isVisible())
check("上次的主机/端口已填", dlg2.host_input.text() == "127.0.0.1"
      and dlg2.port_spin.value() == 30001,
      f"{dlg2.host_input.text()}:{dlg2.port_spin.value()}")

print("\n=== ⑥ 设备库记录注入（set_config）===")
FakeSettings.LAST = None
dlg3 = cd.ConnectDialog()
dlg3.show()
app.processEvents()
check("初始就展开", dlg3.basic_group.isVisible())
dlg3.set_config({"name": "Console", "protocol": "Serial", "serial_port": "COM5",
                 "baudrate": 9600, "stopbits": 1})
check("注入记录后串口组展开", dlg3.basic_group.isVisible() and dlg3.serial_group.isVisible())
check("串口填进控件", "COM5" in dlg3.serial_port.currentText(),
      dlg3.serial_port.currentText())

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
