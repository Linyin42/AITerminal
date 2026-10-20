# -*- coding: utf-8 -*-
"""验证 P2 的**界面**部分：AI 设置对话框里的本地模型标注与起停。

判据（《后续实施安排》P2）："界面标注正确、检索仍可用"。
本脚本用 offscreen Qt 起真对话框，覆盖：
  · 选中 `local` 才出现本地模型那一行（别的提供方不出现）
  · 档位/模式**真的**由 `core.local_model.decide()` 异步探测后写进标注（不是写死的文案）
  · 节点：MODE_LOCAL → 按钮变「停止本地模型」；MODE_RETRIEVAL → 「启动本地模型」
  · 免密钥闸门：provider=local 且密钥为空时**不得**报"请先填写 API Key"
  · 起停 worker 走真 `run_server.py --stop`（本机 8080 无服务时是安全的空操作）

跑法：python tests\\test_local_model_ui.py
"""
import os
import sys
import time

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                    # noqa: E402

from core import local_model as lm                          # noqa: E402
from ui.ai_settings_dialog import AISettingsDialog          # noqa: E402

app = QApplication.instance() or QApplication([])
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def wait_until(pred, timeout=30.0):
    """转事件循环直到条件成立（Qt 信号槽要回到主线程才能生效）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    return False


def select_provider(dlg, name):
    idx = dlg.provider_combo.findData(name)
    assert idx >= 0, f"提供方下拉框里没有 {name}"
    dlg.provider_combo.setCurrentIndex(idx)


def main():
    print("=== P2 本地模型 界面测试（offscreen Qt）===")
    dlg = AISettingsDialog()
    print(f"     提供方顺序 = {[dlg.provider_combo.itemData(i) for i in range(dlg.provider_combo.count())]}")

    # 1) 默认（非 local）不显示本地模型行
    select_provider(dlg, "deepseek")
    check("非 local 提供方隐藏本地模型行", dlg.local_row.isHidden() is True,
          f"isHidden={dlg.local_row.isHidden()}")
    check("  非 local 时提示语是那一家的说明（不是本地模型文案）",
          ("DeepSeek" in dlg.provider_hint.text()) and ("本机离线模型" not in dlg.provider_hint.text()),
          dlg.provider_hint.text()[:50])

    # 2) 选中 local：行出现、字段预填、异步探测真的回来
    select_provider(dlg, "local")
    check("选中 local 后显示本地模型行", dlg.local_row.isHidden() is False,
          f"isHidden={dlg.local_row.isHidden()}")
    check("  接口地址预填为本机 :8080/v1",
          dlg.get_base_url() == "http://127.0.0.1:8080/v1", dlg.get_base_url())
    check("  模型名预填为 local-llm（与 --alias 一致）",
          dlg.get_model() == "local-llm", dlg.get_model())
    check("  免密钥提供方预填占位密钥（且闸门放行）",
          dlg.get_api_key() == "local-llm", repr(dlg.get_api_key()))

    got = wait_until(lambda: not dlg.local_status.text().startswith("探测中"))
    text = dlg.local_status.text()
    check("异步档位探测有结果（标注不再是「探测中…」）", got, text[:80])
    check("  标注内容来自 decide()（模式/模型二选一）",
          ("本地模型" in text) or ("仅本地检索" in text), text[:80])
    check("  真机判定与 local_model 一致",
          text == lm.describe(lm.decide()), text[:60])

    # 3) 注入两种模式，验证按钮文案随之切换
    dlg._on_local_checked({"mode": lm.MODE_LOCAL, "label": "本地模型 · 已就绪（X）",
                           "detail": "Vulkan0", "base_url": "http://127.0.0.1:8080/v1",
                           "tier": "mid", "model": "X", "device": "Vulkan0", "reason": ""})
    check("MODE_LOCAL → 按钮变「停止本地模型」",
          dlg.btn_local.text() == "停止本地模型", dlg.btn_local.text())
    check("  按钮可用", dlg.btn_local.isEnabled() is True, str(dlg.btn_local.isEnabled()))

    dlg._on_local_checked({"mode": lm.MODE_RETRIEVAL, "label": "仅本地检索（不满足档位）",
                           "detail": "内存/显存不足；检索不受影响。", "base_url": "",
                           "tier": "cpu", "model": "", "device": "", "reason": "无独显"})
    check("MODE_RETRIEVAL → 按钮变「启动本地模型」",
          dlg.btn_local.text() == "启动本地模型", dlg.btn_local.text())
    check("  标注写明检索不受影响", "检索" in dlg.local_status.text(), dlg.local_status.text()[:60])

    # 4) 免密钥闸门：密钥清空后点"测试连接"，不能报"请先填写 API Key"
    dlg.api_key_input.setText("")
    dlg.on_test_connection()
    first = dlg.test_result.text()
    check("免密钥提供方不再拦「请先填写 API Key」", "请先填写 API Key" not in first, first[:60])
    wait_until(lambda: dlg.test_result.text() != "测试中…", timeout=25)
    final = dlg.test_result.text()
    check("  失败也给人话（连接类错误，而非密钥类）",
          ("请先填写 API Key" not in final), final[:80])

    # 5) 起停 worker 走真脚本：8080 无服务时 --stop 是安全空操作
    dlg._local_toggle_worker(True)
    check("--stop worker 回来后按钮恢复可用", dlg.btn_local.isEnabled() is True,
          str(dlg.btn_local.isEnabled()))
    check("  操作结果写进了同一行反馈", dlg.test_result.text().startswith(("✓", "✗")),
          dlg.test_result.text()[:60])

    print(f"\n=== 合计 PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    for n in FAIL:
        print(f"   FAIL: {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
