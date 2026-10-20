# -*- coding: utf-8 -*-
"""离屏渲染「新建连接」对话框（**带主题**），量真实高度并截图。

为什么要它：probe 里的高度是无主题测值，而用户看到的是带主题的样子（更高）。
本脚本把主题按应用的方式（`app.setStyleSheet(THEME_QSS)`，与 main_window 同源）
铺上后再量一次高度，并各存一张 PNG 供人眼确认"底部没被切"。

跑法：python -u tools\\shot_connect_dialog.py
输出：%TEMP%\\ait_shots\\connect-<协议>.png
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from PyQt6.QtWidgets import QApplication                        # noqa: E402

app = QApplication.instance() or QApplication([])

import ui.connect_dialog as cd                                  # noqa: E402
from config.settings import Settings as RealSettings            # noqa: E402


class FakeSettings(RealSettings):
    def get_last_connection(self):
        return None


cd.Settings = FakeSettings
cd.SerialPortScanner.cached = staticmethod(lambda: [])
cd.SerialPortScanner.busy = staticmethod(lambda: False)


def apply_theme_like_app():
    """复刻 main_window._apply_theme（ui/main_window.py:430-440）：优先 dark.qss"""
    qss_path = os.path.join(BASE, "resources", "styles", "dark.qss")
    if os.path.exists(qss_path):
        with open(qss_path, encoding="utf-8") as fh:
            qss = fh.read()
        icons = os.path.join(BASE, "resources", "icons").replace("\\", "/")
        app.setStyleSheet(qss.replace("url(../icons/", f"url({icons}/"))
        return "resources/styles/dark.qss"
    from ui.theme import QSS
    app.setStyleSheet(QSS)
    return "ui/theme.QSS（兜底）"


print("主题来源: " + apply_theme_like_app(), flush=True)

out_dir = os.path.join(os.environ.get("TEMP", "."), "ait_shots")
os.makedirs(out_dir, exist_ok=True)

# 1080p 屏上任务栏之后大约可用 1040 px；这里给个"放得下"的判据
SCREEN_AVAILABLE = 1040

for proto, name in (("SSH", "ssh"), ("Telnet", "telnet"),
                    ("Serial", "console"), ("eNSP", "ensp")):
    dlg = cd.ConnectDialog()
    dlg.set_protocol(proto)
    dlg.show()
    app.processEvents()
    dlg.adjustSize()
    app.processEvents()
    png = os.path.join(out_dir, f"connect-{name}.png")
    dlg.grab().save(png)
    height = dlg.height()
    hint = dlg.sizeHint().height()
    flag = "OK " if hint <= SCREEN_AVAILABLE else "超过屏幕可用高度"
    print(f"{proto:<7} 显示高度 {height:>4} px / 需要 {hint:>4} px  {flag}  → {png}",
          flush=True)
    dlg.close()
