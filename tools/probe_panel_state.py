# -*- coding: utf-8 -*-
"""主窗口面板与 AI 区状态的探针（离屏，构造的是真实 MainWindow）。

对应：
  章二 AI/对话栏默认收起；收起后不能再被拖开；中间的欢迎页（"新建连接"）
       不会被面板挤到看不见
  章七 没有任何会话时，AI 区（Catty 对话）的设备条不再停留在上一台设备

跑法：python -u tools\\probe_panel_state.py
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


from PyQt6.QtWidgets import QApplication, QPushButton              # noqa: E402

app = QApplication.instance() or QApplication([])

from ui.main_window import MainWindow                               # noqa: E402

win = MainWindow()
win.resize(1400, 860)
win.show()
app.processEvents()

print("=== ① 章二：默认状态 ===")
sizes = win.side_splitter.sizes()
check("AI 栏默认收起（宽度 0）", sizes[1] == 0, f"sizes={sizes}")
check("默认状态是关的", win._ai_open is False)
check("设备库默认是开的", win._vault_open is True, f"宽度={sizes[0]}")
check("收起后分隔条被禁用（拖不动）",
      win.side_splitter.handle(2).isEnabled() is False)

print("\n=== ② 章二：中间栏有真实下限 ===")
check("中间栏最小宽 = 360", win.tab_stack.minimumWidth() == 360,
      str(win.tab_stack.minimumWidth()))
win.side_splitter.setSizes([280, 1100, 0])          # 硬把终端栏压到 0
app.processEvents()
# 注意：Ignored 策略下 QSplitter.sizes() 仍会报 0 —— 那是分配值不是实际几何，
# 要看子控件真实几何（用户说的症状就是"新建连接被挤到看不见"）。
check("欢迎页仍有宽度（>=360）", win.welcome_page.width() >= 360,
      f"welcome={win.welcome_page.width()}")
btns = [b for b in win.welcome_page.findChildren(QPushButton)
        if b.text().strip() == "新建连接" and b.isVisible()]
check("欢迎页里有可见的「新建连接」按钮", bool(btns), f"找到 {len(btns)} 个")
if btns:
    rect = win.welcome_page.rect()
    geo = btns[0].geometry()
    check("按钮落在欢迎页可视区内",
          geo.x() >= 0 and geo.y() >= 0
          and geo.right() <= rect.width() and geo.bottom() <= rect.height(),
          f"按钮=({geo.x()},{geo.y()}) {geo.width()}x{geo.height()} 页={rect.width()}x{rect.height()}")

print("\n=== ③ 章二：开关面板时分隔条状态跟着变 ===")
win.set_ai_panel_visible(True)
app.processEvents()
sizes = win.side_splitter.sizes()
check("打开后宽度 > 0", sizes[1] > 0, f"sizes={sizes}")
check("打开后分隔条可用（能拖）", win.side_splitter.handle(2).isEnabled() is True)
check("打开后页签栏出现", win.right_tabs.tabBar().isVisible())
win.set_ai_panel_visible(False)
app.processEvents()
sizes = win.side_splitter.sizes()
check("收起后宽度回到 0", sizes[1] == 0, f"sizes={sizes}")
check("收起后分隔条又禁用", win.side_splitter.handle(2).isEnabled() is False)

print("\n=== ④ 章七：没有会话时 AI 设备条要清掉 ===")
win.chat_view.set_device("R1", "Telnet", "192.168.31.33", connected=True, vendor="h3c")
stale = win.chat_view.device_bar.text()
check("先造出'上一台设备'的样子（复现用户看到的现象）",
      "R1" in stale and "已连接" in stale, stale[:60])
win.sync_session_context()                          # 0 个会话
now = win.chat_view.device_bar.text()
check("同步后不再显示旧设备", "R1" not in now and "已连接" not in now, now[:60])
check("显示的是未连接设备", "未连接设备" in now, now[:60])

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
