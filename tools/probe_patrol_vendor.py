# -*- coding: utf-8 -*-
"""巡检「按厂家自动套命令」的探针（章八）。

用户反馈："（巡检要）自动的调用相关的模板"。

覆盖：
  ① 按厂家取只读巡检命令（**剔除** system-view / save 这类改配置的命令）
  ② 打开巡检窗口就自动套上（设备带厂家时）；认不出厂家就保留默认命令
  ③ 主窗口把 session.vendor 带进巡检候选（连接后识别出的厂家最准）
  ④ 手动「按厂家自动」按钮

跑法：python -u tools\\probe_patrol_vendor.py
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


from PyQt6.QtWidgets import QApplication                            # noqa: E402

app = QApplication.instance() or QApplication([])

from config.presets import (get_patrol_commands, get_patrol_commands_for,   # noqa: E402
                            get_vendor_commands)

print("=== ① 按厂家取只读巡检命令 ===")
h3c = get_patrol_commands("h3c")
huawei = get_patrol_commands("huawei")
cisco = get_patrol_commands("cisco")
check("h3c 有 display version", "display version" in h3c, str(h3c[:2]))
check("huawei 有 display version", "display version" in huawei)
check("cisco 是 show 语法", "show version" in cisco and "display version" not in cisco)
check("没有 system-view（会改配置）",
      not any("system-view" in c for c in h3c + huawei + cisco))
check("没有 save（会改配置）",
      not any(c.strip().lower().startswith(("save", "write")) for c in h3c + huawei + cisco))
check("改配置的两条确实存在于常用命令里（说明是被剔除而非本来没有）",
      "进入配置视图" in get_vendor_commands("h3c") and "保存配置" in get_vendor_commands("h3c"))
check("认不出厂家 ⇒ 空（不猜）", get_patrol_commands("unknown") == [])
merged = get_patrol_commands_for(["h3c", "cisco"])
check("混合厂家合并且去重",
      len(merged) == len(set(merged)) and "display version" in merged and "show version" in merged,
      f"{len(merged)} 条")

print("\n=== ② 打开巡检窗口自动套模板 ===")
from ui.patrol_dialog import PatrolDialog, DEFAULT_COMMANDS          # noqa: E402

cands = [{"name": "R1", "protocol": "SSH", "host": "1.1.1.1", "username": "u",
          "vendor": "h3c"},
         {"name": "SW2", "protocol": "SSH", "host": "1.1.1.2", "username": "u",
          "vendor": "h3c"}]
dlg = PatrolDialog(None, candidates=cands, sessions=[])
dlg.show()
app.processEvents()
text = dlg.command_edit.toPlainText()
check("命令框已按厂家填好", "display version" in text and "display current-configuration" in text,
      text.splitlines()[:2])
check("没有把 system-view 填进去", "system-view" not in text)
check("日志里说明了来源", any("按厂家" in line for line in
                              dlg.log_view.toPlainText().splitlines()),
      dlg.log_view.toPlainText().strip().splitlines()[-1:])

print("\n=== ③ 认不出厂家 ⇒ 保留默认命令 ===")
dlg2 = PatrolDialog(None, candidates=[{"name": "X", "protocol": "SSH", "host": "2.2.2.2",
                                       "username": "u"}], sessions=[])
dlg2.show()
app.processEvents()
check("命令框还是默认命令", dlg2.command_edit.toPlainText().strip() == DEFAULT_COMMANDS.strip())
check("返回值是空（明确告诉调用方没套上）", dlg2.apply_vendor_template(silent=True) == "")

print("\n=== ④ 主窗口把 session.vendor 带进巡检候选 ===")
import ui.main_window as mw                                          # noqa: E402
from ui.main_window import MainWindow                                # noqa: E402

seen = {}


class RecordingPatrol:
    def __init__(self, parent=None, candidates=None, sessions=None):
        seen["sessions"] = list(sessions or [])
        seen["candidates"] = list(candidates or [])

    def exec(self):
        return 0


mw.PatrolDialog = RecordingPatrol
win = MainWindow()


class FakeSession:
    name = "R1"
    vendor = "h3c"
    config = {"protocol": "SSH", "host": "10.0.0.1", "username": "root"}


win.session_manager.get_all_sessions = lambda: {1: FakeSession()}
win.open_patrol_dialog()
check("巡检候选带上了厂家", seen.get("sessions") and
      seen["sessions"][0].get("vendor") == "h3c", str(seen.get("sessions")))
check("主机名也还在", seen["sessions"][0].get("host") == "10.0.0.1")

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
