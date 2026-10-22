# -*- coding: utf-8 -*-
"""「视图 → 显示/隐藏 AI 面板」必须能**打开**，不能只会收起。

现场（2026-10-21 `%TEMP%\\gui_drive_1.log`，G11/G12）：
    [FAIL] AI 未启用时点开关会被拦住并给提示   既没切换也没提示

真因：菜单项按 `right_tabs.isVisible()` 决定往哪边切，而 AI 面板收起用的是
"宽度归零"（`set_ai_panel_visible(False)`），`right_tabs` 一直 visible=True
⇒ 这一项永远只会调 `set_ai_panel_visible(False)`，第二次起就被 `_ai_open`
的早退挡掉 ⇒ 点一百次也打不开面板。

跑法：python tests\\test_ai_panel_toggle.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from PyQt6.QtWidgets import QApplication                     # noqa: E402

app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""),
          flush=True)


def pump(sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


from ui.main_window import MainWindow                          # noqa: E402

window = MainWindow()
window.show()
pump(0.8)


def ai_action():
    for act in window.menuBar().actions():
        menu = act.menu()
        if menu is None:
            continue
        for sub in menu.actions():
            if "AI 面板" in sub.text():
                return sub
    return None


act = ai_action()
check("菜单里有「显示/隐藏 AI 面板」", act is not None)

print("=== ① 收起态点一次 ⇒ 打开 ===")
check("初始是收起态", window._ai_open is False, f"_ai_open={window._ai_open}")
act.trigger()
pump(0.4)
width = window.side_splitter.sizes()[1]
check("点一次就打开了", window._ai_open is True, f"_ai_open={window._ai_open}")
check("面板宽度真的让出来了", width > 0, f"splitter[1]={width}px")
check("图标条状态跟着变", window.rail.is_checked("ai") is True
      if hasattr(window.rail, "is_checked") else True, "")

print("\n=== ② 再点一次 ⇒ 收起 ===")
act.trigger()
pump(0.4)
check("再点一次收起了", window._ai_open is False, f"_ai_open={window._ai_open}")
check("宽度归零", window.side_splitter.sizes()[1] == 0,
      f"splitter[1]={window.side_splitter.sizes()[1]}px")

print("\n=== ③ 再点第三次 ⇒ 还能打开（这就是原来挂掉的一步）===")
act.trigger()
pump(0.4)
check("第三次仍然能打开", window._ai_open is True, f"_ai_open={window._ai_open}")

window.close()
print("\n" + "-" * 60)
print(f"结果: {len(PASS)} 通过 / {len(FAIL)} 失败（共 {len(PASS) + len(FAIL)} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
