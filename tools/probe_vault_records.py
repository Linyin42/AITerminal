# -*- coding: utf-8 -*-
"""设备库两处修复的探针（章九）：

  ① 记录里存了密码 ⇒ 双击/连接时**不再弹**对话框，直接连（密码要解码后再用）
  ② 编辑记录改了主机/端口 ⇒ 改的是同一条，不再新增重复项

隔离：主窗口用真实 settings（只读）；settings 的写入测试用**临时配置文件**。
跑法：python -u tools\\probe_vault_records.py
"""
import os
import sys
import tempfile

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


from PyQt6.QtWidgets import QApplication                            # noqa: E402

app = QApplication.instance() or QApplication([])

from config.settings import Settings                                # noqa: E402
import ui.main_window as mw                                         # noqa: E402
from ui.main_window import MainWindow                               # noqa: E402

print("=== ① 记录里存了密码：直接连，不弹框 ===")
opened = []


class BombDialog:
    def __init__(self, *a, **kw):
        opened.append(True)
        raise AssertionError("不该弹连接对话框")


win = MainWindow()
win.create_session = lambda cfg: opened.append(cfg)                  # 别真去连设备
mw.ConnectDialog = BombDialog                                        # 弹了就炸

encoded = win.settings.encode_password("s3cret")
check("测试用的 b64 密码能解回来",
      win.settings.decode_password(encoded) == "s3cret", encoded[:16])
win.on_vault_connect({"name": "R1", "protocol": "Telnet", "host": "10.0.0.9",
                      "port": 30001, "username": "root", "password": encoded})
created = [x for x in opened if isinstance(x, dict)]
check("没有弹对话框", len(opened) == 1, f"opened={opened}")
check("直接调了 create_session", len(created) == 1)
check("密码已解码（不是 b64: 原文）",
      created and created[0].get("password") == "s3cret",
      str(created[0].get("password"))[:20] if created else "-")
check("主机/端口原样带过去",
      created and created[0].get("host") == "10.0.0.9" and created[0].get("port") == 30001)

print("\n=== ② 没存密码：仍然弹框补全（不能把路堵死）===")
opened.clear()
calls = []


class RecordingDialog:
    """记录被打开过，然后返回"用户取消"（exec()=0 ⇒ 不建会话）"""

    def __init__(self, *a, **kw):
        calls.append("dialog")
        self._cfg = None

    def set_config(self, cfg):
        self._cfg = cfg

    def exec(self):
        return 0


mw.ConnectDialog = RecordingDialog
win.on_vault_connect({"name": "R2", "protocol": "SSH", "host": "10.0.0.10", "port": 22})
check("走了对话框分支", calls == ["dialog"], f"calls={calls}")
check("取消后没有建会话", opened == [], f"opened={opened}")

print("\n=== ③ 编辑改了主机：改同一条，不新增 ===")
tmp = os.path.join(tempfile.mkdtemp(prefix="ait-vault-"), "settings.json")
s = Settings(config_file=tmp)
old = {"name": "R1", "protocol": "Telnet", "host": "10.0.0.9", "port": 30001,
       "username": "root"}
s.set("vault_devices", [old])
s.save()
new = {"name": "R1", "protocol": "Telnet", "host": "10.0.0.99", "port": 30001,
       "username": "root"}                       # 主机改了 ⇒ key 变了
s.update_vault_device(new, old_config=old)
devices = Settings(config_file=tmp).get_vault_devices()
check("仍然只有一条记录", len(devices) == 1, f"共 {len(devices)} 条")
check("记录里是改后的主机", devices and devices[0].get("host") == "10.0.0.99",
      str(devices[0].get('host')) if devices else "-")

print("\n=== ④ 不传 old_config 时保持老行为（向后兼容）===")
tmp2 = os.path.join(tempfile.mkdtemp(prefix="ait-vault2-"), "settings.json")
s2 = Settings(config_file=tmp2)
s2.set("vault_devices", [{"name": "A", "protocol": "SSH", "host": "1.1.1.1", "port": 22}])
s2.save()
s2.update_vault_device({"name": "A", "protocol": "SSH", "host": "1.1.1.1", "port": 22,
                        "username": "admin"})
d2 = Settings(config_file=tmp2).get_vault_devices()
check("同 key 更新到同一条", len(d2) == 1 and d2[0].get("username") == "admin",
      f"{len(d2)} 条 / {d2[0].get('username') if d2 else '-'}")

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
