# -*- coding: utf-8 -*-
"""AI 设置里的「外部接管（DSH / 智能体）」开关：验它接的是主窗口的真服务。

跑法：python -u tests\\test_control_ui.py

隔离：把 ui.ai_settings_dialog 里的 Settings 换成指向临时配置文件的版本，
      跑用例**不会**动用户真实的 ~/.aiterminal/settings.json。
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


from PyQt6.QtWidgets import QApplication                          # noqa: E402

app = QApplication.instance() or QApplication([])

import ui.ai_settings_dialog as mod                               # noqa: E402
from config.settings import Settings                              # noqa: E402

CFG = os.path.join(tempfile.mkdtemp(prefix="ait-ui-cfg-"), "settings.json")
mod.Settings = lambda: Settings(config_file=CFG)                  # 隔离配置


class FakeControl:
    """替身：主窗口那份接口（control_state / set_control_enabled / reset_control_token）"""

    def __init__(self, accept: bool = True):
        self.calls = []
        self.enabled = False
        self.accept = accept
        self.token = "abcdef1234567890abcdef1234567890"

    def control_state(self):
        return {"enabled": self.enabled,
                "url": "http://127.0.0.1:49321" if self.enabled else "",
                "token": self.token if self.enabled else ""}

    def set_control_enabled(self, enabled, remember=True):
        self.calls.append(bool(enabled))
        if self.accept:
            self.enabled = bool(enabled)
        return self.accept

    def reset_control_token(self):
        self.token = "f" * 32
        return self.token


print("=== ① 默认关闭 ===")
fake = FakeControl()
dlg = mod.AISettingsDialog(None, control=fake)
check("对话框里有「外部接管」开关", hasattr(dlg, "chk_control"))
check("默认是关的", dlg.chk_control.isChecked() is False)
check("状态行说明未开启", "未开启" in dlg.control_status.text(), dlg.control_status.text())
check("关着的时候复制/重置按钮禁用",
      not dlg.btn_copy_token.isEnabled() and not dlg.btn_reset_token.isEnabled())

print("\n=== ② 打开 → 真的让主窗口起服务 ===")
dlg.chk_control.setChecked(True)
check("调了 set_control_enabled(True)", fake.calls == [True], str(fake.calls))
check("状态行给出地址与令牌前缀",
      "127.0.0.1" in dlg.control_status.text() and "abcdef12" in dlg.control_status.text(),
      dlg.control_status.text().replace("\n", " / "))
check("配置落盘（control_api_enabled=True）",
      Settings(config_file=CFG).get("control_api_enabled") is True)
check("按钮启用", dlg.btn_copy_token.isEnabled() and dlg.btn_reset_token.isEnabled())

print("\n=== ③ 令牌：复制与重置 ===")
old = fake.token
dlg.on_reset_token()
check("重置换了新令牌并提示旧令牌失效",
      fake.token != old and "已重置" in dlg.control_status.text(), fake.token[:8])
dlg.on_copy_token()
check("复制到剪贴板的是「地址 + 令牌」",
      app.clipboard().text().endswith(fake.token) and "127.0.0.1" in app.clipboard().text(),
      app.clipboard().text().replace("\n", " / ")[:60])

print("\n=== ④ 关回去 / 起不来 ===")
dlg.chk_control.setChecked(False)
check("关掉时调了 set_control_enabled(False)", fake.calls == [True, False], str(fake.calls))
dead = FakeControl(accept=False)
dlg2 = mod.AISettingsDialog(None, control=dead)
dlg2.chk_control.setChecked(True)
check("服务起不来时开关自动回滚", dlg2.chk_control.isChecked() is False)
check("回滚后配置也是 False",
      Settings(config_file=CFG).get("control_api_enabled") is False)
check("回滚后状态行说未开启", "未开启" in dlg2.control_status.text(), dlg2.control_status.text())

print("\n=== ⑤ 没有 control（旧用法）也不炸 ===")
dlg3 = mod.AISettingsDialog(None)
check("只给 parent 也能构造", dlg3.chk_control.isChecked() is False)
dlg3.chk_control.setChecked(True)
check("开关仍可切换（只是没人真起服务）", dlg3.chk_control.isChecked() is True)
check("状态行不报错", dlg3.control_status.text() != "")

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
