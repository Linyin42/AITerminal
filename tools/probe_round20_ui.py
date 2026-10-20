"""离屏验证本轮改动：巡检设备编辑页 + 导出可读 + 巡检关分屏 + 会话自动翻页

为什么不用手点界面：这些都是"有/没有"的机制，用 QPA offscreen 起一个巡检对话框
就能把表格列、密码列、设备增删改、保存时是否漏密码全部量出来，比截图快。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                        # noqa: E402

app = QApplication.instance() or QApplication([])
FAILS = []


def check(name, condition, detail=""):
    print(("  [PASS] " if condition else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not condition:
        FAILS.append(name)


print("[1] 巡检对话框：6 列 + 密码列 + 手动添加/编辑")
from ui.patrol_dialog import PatrolDialog                       # noqa: E402
from ui.device_edit_dialog import DeviceEditDialog              # noqa: E402

dialog = PatrolDialog(None, candidates=[], sessions=[])
headers = [dialog.table.horizontalHeaderItem(i).text()
           for i in range(dialog.table.columnCount())]
check("表头含密码列", headers == ["选择", "名称", "协议", "地址", "密码", "来源"], str(headers))
check("有手动添加按钮", any(b.text().startswith("手动添加设备")
                            for b in dialog.findChildren(type(dialog.btn_start))))

device = {"name": "核心S7503X", "protocol": "Telnet", "host": "10.2.0.1",
          "port": 23, "username": "admin", "password": "secret", "source": "手动填写"}
dialog.add_device_row(device, checked=True, source="手动填写")
check("密码列显示已填", dialog.table.item(0, 4).text() == "已填",
      dialog.table.item(0, 4).text())
dialog.add_device_row({**device, "name": "没密码的", "host": "10.9.9.9",
                       "password": ""}, checked=True, source="手动填写")
check("没密码显示未填", dialog.table.item(1, 4).text() == "未填")

print("[2] 去重与替换")
keys = dialog.existing_keys()
check("去重键含协议/地址/用户名", all(len(k) == 4 for k in keys))
dialog.replace_device((device["protocol"], device["host"], None, device["username"]),
                      {**device, "name": "改名了", "password": "newpass"})
check("替换后名字更新", dialog.table.item(0, 1).text() == "改名了",
      dialog.table.item(0, 1).text())

print("[3] 保存设置不落密码")
saved = {}

# set_patrol_devices 直接写 _data，不经过 set()，所以桩要打在它自己身上
dialog.settings.set_patrol_devices = lambda devices: saved.__setitem__("devices", devices)
dialog.settings.save = lambda: None
dialog.save_settings()
devices = saved.get("devices") or []
check("保存了设备列表", len(devices) == 2, f"{len(devices)} 台")
check("列表里两台都在", sorted(d.get("host", "") for d in devices) == ["10.2.0.1", "10.9.9.9"],
      str(sorted(d.get("host", "") for d in devices)))
check("保存时剥掉密码", all("password" not in d for d in devices),
      str([list(d) for d in devices]))
check("其余字段还在", all(d.get("host") for d in devices))

print("[4] 设备编辑对话框取数")
edit = DeviceEditDialog(None, device={"protocol": "Telnet", "host": "10.2.0.1",
                                      "port": 23, "username": "admin"})
edit.password_input.setText("p@ss")
got = edit.get_device()
check("协议保留", got["protocol"] == "Telnet", got["protocol"])
check("端口保留", got["port"] == 23, str(got["port"]))
check("密码带出来", got["password"] == "p@ss")
check("名称缺省用 IP", got["name"] == "10.2.0.1", got["name"])
empty = DeviceEditDialog(None)
empty.host_input.setText("1.2.3.4")
check("新增默认 SSH:22", empty.get_device()["port"] == 22,
      str(empty.get_device()["port"]))

print("[5] 巡检关分屏 + 页数不重复计数")
import inspect                                                   # noqa: E402
import core.patrol as patrol                                     # noqa: E402
src = inspect.getsource(patrol.patrol_device)
check("巡检里有关分屏调用", "disable_paging(" in src)
check("页数只取一次累计值", 'result["pages"] = waiter.pages' in src,
      "旧写法会累加")
check("厂商识别可用", patrol.guess_vendor({"name": "H3C-S7503X"}) == "h3c"
      and patrol.guess_vendor({"name": "华为-10.3.8.3"}) == "huawei",
      f"{patrol.guess_vendor({'name': 'H3C-S7503X'})}/{patrol.guess_vendor({'name': '华为-10.3.8.3'})}")

print("[6] 会话自动翻页接线")
import ui.main_window as mw                                      # noqa: E402
check("有 _maybe_auto_page", hasattr(mw.MainWindow, "_maybe_auto_page"))
check("update_output 里调用了它", "_maybe_auto_page" in inspect.getsource(mw.MainWindow.update_output))
from config.settings import Settings                             # noqa: E402
check("设置项 session_auto_page 默认开",
      Settings().get("session_auto_page", None) is not False)

print()
if FAILS:
    print(f"[FAIL] {len(FAILS)} 项未通过: {FAILS}")
    sys.exit(1)
print("[PASS] 全部通过")
