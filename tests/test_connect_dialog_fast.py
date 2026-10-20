"""连接对话框不能被串口枚举拖住（用户 2026-10-17：「高级选项点击后要卡一下才会启动」）。

跑法：python tests\test_connect_dialog_fast.py
判据：把串口枚举换成"睡 1 秒"的假实现后，ConnectDialog() 仍然立刻返回；且后台
      扫完的结果会自己填进下拉框（证明不是"干脆不扫了"）。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication                    # noqa: E402

app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))


from ui.connect_dialog import (ConnectDialog, SerialAdvancedDialog,   # noqa: E402
                              SerialPortScanner)

SLOW = 1.0


def slow_ports():
    time.sleep(SLOW)        # 模拟插着 USB 转串口时 setupapi 的枚举耗时
    return [{"device": "COM9", "description": "假串口"}]


ConnectDialog.list_serial_ports = staticmethod(slow_ports)
SerialPortScanner._cache = []
SerialPortScanner._busy = False

print("=== ① 打开对话框：不枚举、不卡 ===")
t0 = time.monotonic()
d = ConnectDialog()
cost = time.monotonic() - t0
check(f"连接对话框立刻打开（{cost * 1000:.0f} ms < 300 ms，枚举要 {SLOW:.1f}s）", cost < 0.3)
check("打开时已触发后台枚举（busy=True）", SerialPortScanner.busy() is True)
check("下拉框立刻有内容（占位或上次用过的串口），不是空白",
      d.serial_port.currentText() != "", d.serial_port.currentText())

print("\n=== ② 后台扫完要自己填进来 ===")
t_end = time.monotonic() + SLOW + 3


def texts():
    return [d.serial_port.itemText(i) for i in range(d.serial_port.count())]


while time.monotonic() < t_end and not any("COM9" in t for t in texts()):
    app.processEvents()
    time.sleep(0.02)
check("后台枚举结果落到下拉框里", any("COM9" in t for t in texts()), str(texts()))

print("\n=== ③ 高级选项对话框本身 ===")
t0 = time.monotonic()
adv = SerialAdvancedDialog(None, dict(d.serial_advanced))
cost_adv = time.monotonic() - t0
check(f"高级选项窗口构造 < 200 ms（{cost_adv * 1000:.0f} ms）", cost_adv < 0.2)

print("\n=== ④ 二次打开走缓存 ===")
SerialPortScanner._busy = False
t0 = time.monotonic()
d2 = ConnectDialog()
cost_warm = time.monotonic() - t0
check(f"二次打开 < 150 ms（{cost_warm * 1000:.0f} ms）", cost_warm < 0.15)
check("缓存命中时下拉框直接有串口",
      any("COM9" in t for t in [d2.serial_port.itemText(i)
                                for i in range(d2.serial_port.count())]),
      str([d2.serial_port.itemText(i) for i in range(d2.serial_port.count())]))

print(f"\n=== 合计 PASS {len(PASS)} / FAIL {len(FAIL)} ===")
sys.exit(1 if FAIL else 0)
