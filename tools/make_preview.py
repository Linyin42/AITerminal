"""
生成界面预览图（离屏渲染，不需要真实显示器）。

用途：改完 UI 后快速看效果，产出 layout_preview.png。
用法：python tools/make_preview.py [输出路径]
"""
import os
import sys
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFontDatabase, QFont

from config.settings import Settings
Settings().set("restore_sessions", False)

from ui.main_window import MainWindow

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJECT_ROOT, "layout_preview.png")

# 真实设备回显样本（取自 10.3.8.2 实机）
SAMPLE_OUTPUT = """<zhengfayonghuhuiju>display version
H3C Comware Platform Software
Comware Software, Version 5.20, Release 5101P05
H3C S5500-34C-HI uptime is 0 week, 1 day, 9 hours, 38 minutes

<zhengfayonghuhuiju>display interface brief
The brief information of interface(s) under route mode:
Link: ADM - administratively down; Stby - standby
Interface            Link Protocol Main IP         Description
Vlan1                UP   UP       10.3.8.2
Vlan99               UP   UP       10.10.99.2
Vlan100              ADM  DOWN     --
Vlan619              UP   UP       172.18.19.2

The brief information of interface(s) under bridge mode:
Interface            Link Speed   Duplex Type PVID Description
GE1/0/1              UP   1G(a)   F(a)   T    1
GE1/0/2              DOWN auto    A      T    1
GE1/0/19             UP   1G(a)   F(a)   A    619
XGE1/0/29            DOWN auto    A      T    1
BAGG2                DOWN auto    A      T    1    to 2-4 huawei
BAGG3                UP   2G(a)   F(a)   T    1    to jiankonghex

<zhengfayonghuhuiju>display vlan
 Total 26 VLAN exist(s).
 The following VLANs exist:
  1(default), 99-100, 601-620, 2004, 3001, 3003

<zhengfayonghuhuiju>display vlan brief
                     ^
% Wrong parameter found at '^' position.
<zhengfayonghuhuiju>
"""


def main():
    app = QApplication(sys.argv)
    # 加载中文字体，避免预览图里中文变方块
    fid = QFontDatabase.addApplicationFont(r"C:/Windows/Fonts/msyh.ttc")
    fams = QFontDatabase.applicationFontFamilies(fid)
    if fams:
        f = QFont(fams[0])
        f.setPointSize(9)
        app.setFont(f)

    w = MainWindow()
    if fams:
        w.setStyleSheet(w.styleSheet() + "\n* { font-family: '%s'; }" % fams[0])
    app.processEvents()
    time.sleep(0.3)
    app.processEvents()

    w.vault_panel.set_devices([
        {"name": "核心交换机-A", "protocol": "SSH", "host": "10.3.8.2", "port": 22,
         "vendor": "h3c", "username": "root"},
        {"name": "汇聚-B", "protocol": "Telnet", "host": "10.0.0.2", "port": 23,
         "username": "admin"},
        {"name": "Cisco-Edge", "protocol": "SSH", "host": "172.16.1.1", "port": 22,
         "vendor": "cisco", "username": "netops"},
        {"name": "Console-Serial", "protocol": "Serial", "serial_port": "COM3",
         "vendor": "unknown", "username": ""},
    ])
    w.vault_panel._on_card_clicked(w.vault_panel._devices[0])
    w.vault_panel.set_connected(w.vault_panel._devices[0], True)

    from ui.session_tab import SessionTab
    tab = SessionTab(1, "核心交换机-A", "SSH 10.3.8.2:22")
    tab.append_output(SAMPLE_OUTPUT)
    w.tab_widget.addTab(tab, "核心交换机-A")
    w.tab_widget.setCurrentWidget(tab)

    w.chat_view.add_user("检查所有接口状态，找出 down 的端口")
    w.chat_view.add_ai("已读取设备输出。当前 UP 的接口有 Vlan1/Vlan99/Vlan619、GE1/0/1、"
                       "GE1/0/19、BAGG3；DOWN 的有 GE1/0/2、XGE1/0/29、BAGG2。"
                       "建议执行以下命令核对：",
                       commands=["display interface brief", "display ip interface brief"],
                       notes="均为只读命令")

    w.resize(1500, 940)
    w.show()
    app.processEvents()
    time.sleep(0.6)
    app.processEvents()

    ok = w.grab().save(OUT, "PNG")
    print(f"预览图: {OUT}  成功={ok}")
    w.close()


if __name__ == "__main__":
    main()