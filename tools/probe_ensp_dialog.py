# -*- coding: utf-8 -*-
"""eNSP Pro 接入探针：协议项 / 字段 / 配置读写 / 真实拉取设备。

不动用户真实 settings.json（save 与 set_last_connection 都打桩）。

跑法: python -u tools\\probe_ensp_dialog.py     （平台地址可用 ENSP_HOST 覆盖）
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

FAIL = []
TOTAL = [0]


def check(name, cond, detail=""):
    TOTAL[0] += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (f"   {detail}" if detail else ""), flush=True)
    if not cond:
        FAIL.append(name)


from PyQt6.QtWidgets import QApplication                              # noqa: E402

app = QApplication.instance() or QApplication([])

import ui.connect_dialog as cd                                         # noqa: E402
from config.settings import Settings as RealSettings                   # noqa: E402


class FakeSettings(RealSettings):
    """只读真实配置的默认值；写入全部打桩，绝不碰用户文件"""

    LAST = None
    SAVED = None

    def get_last_connection(self):
        return FakeSettings.LAST

    def set_last_connection(self, cfg):
        FakeSettings.SAVED = cfg

    def save(self):
        return True


cd.Settings = FakeSettings
cd.SerialPortScanner.cached = staticmethod(lambda: [])
cd.SerialPortScanner.busy = staticmethod(lambda: False)

print("=== ① 协议下拉含 eNSP Pro，切换显示 eNSP 分组 ===")
FakeSettings.LAST = None
dlg = cd.ConnectDialog()
dlg.show()                       # 不 show 的话 isVisible() 恒 False
app.processEvents()
items = [dlg.protocol_combo.itemText(i) for i in range(dlg.protocol_combo.count())]
check("下拉含 eNSP Pro", "eNSP Pro" in items, str(items))
check("默认不显示 eNSP 分组", not dlg.ensp_group.isVisible())
dlg.set_protocol("eNSP")
app.processEvents()
check("切到 eNSP 后分组可见", dlg.ensp_group.isVisible())
check("切到 eNSP 后端口自动 8443", dlg.port_spin.value() == 8443,
      str(dlg.port_spin.value()))
check("eNSP 时连接参数仍可见", dlg.conn_group.isVisible())
check("eNSP 时 Console 参数隐藏", not dlg.serial_group.isVisible())

print("=== ② 填值 → get_config 带出 sandbox_id / node_id ===")
dlg.host_input.setText("192.168.31.11")
dlg.ensp_sandbox_spin.setValue(2)
dlg.ensp_node_spin.setValue(22)
dlg.password_input.setText("Huawei@123")
cfg = dlg.get_config()
check("protocol = eNSP", cfg.get("protocol") == "eNSP", str(cfg.get("protocol")))
check("sandbox_id = 2", cfg.get("sandbox_id") == 2, str(cfg.get("sandbox_id")))
check("node_id = 22", cfg.get("node_id") == 22, str(cfg.get("node_id")))
check("port = 8443", cfg.get("port") == 8443, str(cfg.get("port")))
check("host 带过去", cfg.get("host") == "192.168.31.11")
check("密码带过去", cfg.get("password") == "Huawei@123")
check("落盘配置也含编号", (FakeSettings.SAVED or {}).get("node_id") == 22,
      str((FakeSettings.SAVED or {}).get("node_id")))

print("=== ③ set_config 回填（设备库编辑复用）===")
dlg2 = cd.ConnectDialog()
dlg2.show()
app.processEvents()
dlg2.set_config({"protocol": "eNSP", "host": "192.168.31.11", "port": 8443,
                 "sandbox_id": 3, "node_id": 7, "name": "eNSP-测试"})
app.processEvents()
check("协议回填为 eNSP", dlg2.internal_protocol == "eNSP", dlg2.internal_protocol)
check("沙箱回填 3", dlg2.ensp_sandbox_spin.value() == 3,
      str(dlg2.ensp_sandbox_spin.value()))
check("设备回填 7", dlg2.ensp_node_spin.value() == 7,
      str(dlg2.ensp_node_spin.value()))
check("回填后分组可见", dlg2.ensp_group.isVisible())

print("=== ④ 从平台拉取设备（真实网络）===")
HOST = os.environ.get("ENSP_HOST", "192.168.31.11")
dlg3 = cd.ConnectDialog()
dlg3.show()
dlg3.set_protocol("eNSP")
dlg3.host_input.setText(HOST)
app.processEvents()
dlg3.pull_ensp_devices()
deadline = time.time() + 45
while time.time() < deadline and not dlg3.btn_ensp_pull.isEnabled():
    app.processEvents()
    time.sleep(0.2)
labels = [dlg3.ensp_device_combo.itemText(i)
          for i in range(dlg3.ensp_device_combo.count())]
check("拉取结束（按钮解禁）", dlg3.btn_ensp_pull.isEnabled(), str(labels[:3]))
check("列到了带 id 的设备项", any("id=" in x for x in labels), str(labels[:3]))
if any("id=" in x for x in labels):
    idx = next(i for i, x in enumerate(labels) if "id=" in x)
    dlg3.ensp_device_combo.setCurrentIndex(idx)
    app.processEvents()
    check("选中后自动填 ID",
          dlg3.ensp_sandbox_spin.value() > 0 and dlg3.ensp_node_spin.value() > 0,
          f"sandbox={dlg3.ensp_sandbox_spin.value()} node={dlg3.ensp_node_spin.value()}")

print(f"\n结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）",
      flush=True)
sys.exit(1 if FAIL else 0)
