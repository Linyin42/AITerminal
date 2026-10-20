# -*- coding: utf-8 -*-
"""对话框居中 + 进程图标 的探针（离屏）。

对应：
  章三 打开资料库后对话框要居中
  章十 左上角图标（进程级 setWindowIcon，窗口/对话框/任务栏都继承）

跑法：python -u tools\\probe_center_icon.py
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


from PyQt6.QtGui import QGuiApplication, QIcon                       # noqa: E402
from PyQt6.QtWidgets import QApplication                            # noqa: E402

app = QApplication.instance() or QApplication([])


def near(a, b, tol=3):
    return abs(a - b) <= tol


def center_of(widget):
    geo = widget.frameGeometry()
    return geo.center().x(), geo.center().y()


def screen_center(widget):
    screen = widget.screen() or QGuiApplication.primaryScreen()
    avail = screen.availableGeometry()
    return avail.center().x(), avail.center().y()


print("=== ① 章十：进程级图标 ===")
icon_path = os.path.join(BASE, "resources", "icons", "app.ico")
check("图标文件在", os.path.exists(icon_path), icon_path)
check("图标能加载（非空）", not QIcon(icon_path).isNull())
app.setWindowIcon(QIcon(icon_path))
check("QApplication.windowIcon() 已设置", not app.windowIcon().isNull())
check("main.py 里有 setWindowIcon（窗口/对话框都会继承）",
      "setWindowIcon" in open(os.path.join(BASE, "main.py"), encoding="utf-8").read())

print("\n=== ② 章三：资料管理对话框居中 ===")
from ui.knowledge_dialog import KnowledgeDialog                     # noqa: E402

kb = KnowledgeDialog()
kb.should_show_import_tip = lambda: False        # 别弹提示框挡住探针
kb.show()
app.processEvents()
cx, cy = center_of(kb)
sx, sy = screen_center(kb)
check("居中（横）", near(cx, sx), f"框心={cx} 屏心={sx}")
check("居中（纵）", near(cy, sy), f"框心={cy} 屏心={sy}")
check("没有跑出可用区",
      kb.frameGeometry().left() >= 0 and kb.frameGeometry().top() >= 0,
      f"左上=({kb.frameGeometry().left()},{kb.frameGeometry().top()})")

print("\n=== ③ 居只居一次（用户挪过就别拉回来）===")
kb.move(12, 34)
kb.hide()
kb.show()
app.processEvents()
check("位置保持用户挪的地方", (kb.x(), kb.y()) == (12, 34), f"({kb.x()},{kb.y()})")

print("\n=== ④ 新建连接对话框也居中 ===")
from ui.connect_dialog import ConnectDialog                         # noqa: E402

dlg = ConnectDialog()
dlg.show()
app.processEvents()
cx, cy = center_of(dlg)
sx, sy = screen_center(dlg)
check("居中（横）", near(cx, sx), f"框心={cx} 屏心={sx}")
check("居中（纵）", near(cy, sy), f"框心={cy} 屏心={sy}")

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
