# -*- coding: utf-8 -*-
"""控制通道「后台预热知识库」的探针。

背景（实测）：27,297 篇 / 782 MB 索引的库，冷启动 `/knowledge/stats` 要 **72.7 秒**
（现建索引），而随包 MCP 桥的普通请求超时 30 秒 ⇒ 接管方第一次查资料必失败。
修法：控制通道一开就在后台把索引建好（`MainWindow.warm_knowledge_for_control`）。

隔离：control.json / 审计日志指到临时目录（同 tests/test_control_ui.py），
      `_ensure_knowledge` 换成记录器，不真建 782 MB 索引。
跑法：python -u tools\\probe_control_warm.py
"""
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

HOME_TMP = tempfile.mkdtemp(prefix="ait-warm-")
os.environ["USERPROFILE"] = HOME_TMP
os.environ["HOME"] = HOME_TMP

import core.control_api as control_api                                # noqa: E402

control_api.CONTROL_FILE = os.path.join(HOME_TMP, ".aiterminal", "control.json")
control_api.AUDIT_FILE = os.path.join(HOME_TMP, ".aiterminal", "control-audit.log")

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

import ui.main_window as mw                                         # noqa: E402
from ui.main_window import MainWindow                               # noqa: E402

real_sleep = time.sleep
time.sleep = lambda seconds: None            # 预热线程里的 8 秒等待不用真等
mw.time.sleep = time.sleep

print("=== 默认：控制通道开启也**不**预热（省内存） ===")
win = MainWindow()
called = threading.Event()
calls = []


def fake_ensure():
    calls.append(1)
    called.set()
    return "fake-kb"


win.session_manager._ensure_knowledge = fake_ensure
ok = win.set_control_enabled(True, remember=False)
check("控制通道起来了", ok is True and win.control_server is not None,
      win.control_server.url if win.control_server else "-")
check("发现文件写在临时目录（没碰用户配置）",
      os.path.exists(control_api.CONTROL_FILE), control_api.CONTROL_FILE)
check("默认不预热（不占约 0.9GB 内存）", not called.wait(1.5), f"调用 {len(calls)} 次")

print("\n=== 打开 knowledge_autobuild 才预热 ===")
win.settings.set("knowledge_autobuild", True)
win.warm_knowledge_for_control()
check("预热线程确实去建索引了", called.wait(5.0), f"调用 {len(calls)} 次")
check("预热是后台线程（不阻塞调用方）", len(calls) <= 2)
check("control_api 会复用这个实例（同一个 manager）",
      win.control_server.manager is win.session_manager)

print("\n=== 预热失败不影响功能 ===")
def boom():
    raise RuntimeError("假装索引坏了")


win2 = MainWindow()
win2.settings.set("knowledge_autobuild", True)   # 让预热真跑起来，才测得到"建库失败"这条路
win2.session_manager._ensure_knowledge = boom
try:
    win2.set_control_enabled(True, remember=False)
    win2.warm_knowledge_for_control()
    ok2 = True
except Exception as exc:                      # noqa: BLE001
    ok2 = False
    print("   异常:", exc)
check("索引建不起来也不抛到界面", ok2 is True)
real_sleep(0.6)                               # 给预热线程时间真的抛一次

win.set_control_enabled(False, remember=False)
win2.set_control_enabled(False, remember=False)
check("关闭后删掉发现文件", not os.path.exists(control_api.CONTROL_FILE))

time.sleep = real_sleep
print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
