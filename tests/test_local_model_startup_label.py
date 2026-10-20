# -*- coding: utf-8 -*-
"""验证 P2 的「应用启动时判档 + 标注」这一处（`ui/ai_panel.py`）。

为什么单独一个用例：P2 原来的界面验证只在 **AI 设置对话框**里（test_local_model_ui.py），
而启动路径（主窗口建出 AIPanel → load_settings → 标注）没有任何用例覆盖。
本脚本用 offscreen Qt 起真面板，覆盖：

  1. 面板一建出来就有 `local_model_label`（不会因为漏建控件而在启动时报 AttributeError）
  2. 启动即发起异步探测，且**先**给出"探测中…"（证明没在 UI 线程里同步等子进程）
  3. 探测结果最终真的回填到标注（真机跑一次 `core.local_model.decide()`）
  4. 三种模式的文案正确；MODE_STARTABLE 额外给出"去哪一键启动"的指引
  5. **只标注、不自动启动**：面板模块内不存在任何拉起模型的代码路径（无 run_server）

跑法：python tests\\test_local_model_startup_label.py
"""
import inspect
import os
import sys
import time

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                    # noqa: E402

from core import local_model as lm                          # noqa: E402
from ui import ai_panel as panel_mod                        # noqa: E402

app = QApplication.instance() or QApplication([])
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def wait_until(pred, timeout=60.0):
    """转事件循环直到条件成立（Qt 信号槽要回主线程才生效）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    return False


def main():
    print("=== P2 启动判档 + 标注 判据测试（ui/ai_panel.py）===")

    panel = panel_mod.AIPanel()

    # 1. 控件存在
    check("面板有 local_model_label", hasattr(panel, "local_model_label"))
    check("面板有 ai_config_label（原有控件没被破坏）", hasattr(panel, "ai_config_label"))

    # 2. 启动瞬间是"探测中…"（证明探测是异步的，没堵住启动）
    first = panel.local_model_label.text()
    check("启动即发起探测且先显示『探测中…』",
          first.startswith("本地模型：") and "探测中" in first, repr(first))

    # 3. 真机探测结果最终回填（不再停在"探测中…"）
    real_ok = wait_until(lambda: "探测中" not in panel.local_model_label.text(), timeout=90)
    check("档位探测结果回填到标注（真机 decide()）", real_ok,
          repr(panel.local_model_label.text()[:110]))

    # 4. 三种模式文案（用注入的 decision 驱动，不依赖本机档位）
    panel._on_local_status_ready({
        "mode": lm.MODE_LOCAL, "label": "本地模型 · 已就绪（Qwen3-4B）",
        "detail": "Vulkan0 · 端口 8080", "base_url": "", "tier": "mid",
        "model": "Qwen3-4B", "device": "Vulkan0", "reason": ""})
    t_local = panel.local_model_label.text()
    check("MODE_LOCAL → 标注写「已就绪」", "已就绪" in t_local, repr(t_local[:90]))

    panel._on_local_status_ready({
        "mode": lm.MODE_STARTABLE, "label": "本地模型 · 可启动（Qwen3-4B）",
        "detail": "将加载到 Vulkan0", "base_url": "", "tier": "mid",
        "model": "Qwen3-4B", "device": "Vulkan0", "reason": ""})
    t_start = panel.local_model_label.text()
    check("MODE_STARTABLE → 标注写「可启动」", "可启动" in t_start, repr(t_start[:90]))
    check("MODE_STARTABLE → 给出『去 AI 设置里启动』指引，而不是替用户启动",
          "AI 设置" in t_start and "一键启动" in t_start, repr(t_start[-40:]))

    panel._on_local_status_ready({
        "mode": lm.MODE_RETRIEVAL, "label": "仅本地检索（不满足本地 LLM 档位）",
        "detail": "可用内存不足；本地检索照常可用", "base_url": "", "tier": "cpu",
        "model": "", "device": "", "reason": "可用内存不足"})
    t_ret = panel.local_model_label.text()
    check("MODE_RETRIEVAL → 标注如实写「仅本地检索」", "仅本地检索" in t_ret, repr(t_ret[:90]))
    check("MODE_RETRIEVAL → 不出现『一键启动』指引（不能给起不来的机器误导）",
          "一键启动" not in t_ret, repr(t_ret[-40:]))

    # 5. 结构判据：面板里不能有任何"替你起模型"的代码路径
    src = inspect.getsource(panel_mod)
    check("面板源码内无 run_server（即『不自动启动』是结构保证，不是靠自觉）",
          "run_server" not in src)
    check("异步探测走独立线程（threading.Thread 且 daemon）",
          "threading.Thread" in src and "daemon=True" in src)

    print(f"\n{'=' * 70}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n in FAIL:
            print("  FAIL: " + n)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
