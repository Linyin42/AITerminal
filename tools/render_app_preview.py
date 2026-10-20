# -*- coding: utf-8 -*-
"""离屏渲染一张 app 主界面截图（换配色后肉眼复核用；不开窗口、不动用户配置）。

跑法：python tools\\render_app_preview.py [输出路径]
默认输出：%TEMP%\\ait-app-preview.png

说明：离屏（QT_QPA_PLATFORM=offscreen）下没有真实窗口，`grab()` 拿到的是控件自绘
      结果，足以核对配色与形制；设备库/终端内容是用户配置里已有的，不会去连设备。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("TEMP", "."), "ait-app-preview.png")

from PyQt6.QtWidgets import QApplication                     # noqa: E402
from PyQt6.QtCore import QTimer                              # noqa: E402

app = QApplication([])
# 兜底：万一主窗口构造里弹了模态框，30 秒后也要退出，别把探针挂死
QTimer.singleShot(30_000, app.quit)

from ui.main_window import MainWindow                        # noqa: E402
from ui.theme import P                                       # noqa: E402

win = MainWindow()
win.resize(1360, 820)
win.show()


def shoot():
    pix = win.grab()
    ok = pix.save(out)
    print(f"画布色 {P['bg']} / 面板色 {P['bg_panel']} / 强调色 {P['accent']}")
    print(f"截图 {pix.width()}x{pix.height()} saved={ok} -> {out}")
    app.quit()


QTimer.singleShot(1800, shoot)
app.exec()
sys.exit(0 if os.path.exists(out) else 1)
