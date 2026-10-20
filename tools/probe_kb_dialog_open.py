"""量一下"打开资料管理"到底要多久 —— 用真实资料库目录（默认 KB），分步计时。

背景：2026-10-17 写 test_import_tip.py 时发现 `KnowledgeDialog()` 用默认 KB 构造
      180 秒都没返回；本机 `~/.aiterminal/knowledge` 有 27298 个文件。
      用户之前抱怨过"打开连接卡一下"，同类问题必须在资料管理上查清。

跑法（离屏，不动用户配置）：python tools\probe_kb_dialog_open.py
输出：每步耗时 + 总共多少文档；结论看最后一行。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication                    # noqa: E402

app = QApplication.instance() or QApplication([])


def step(name, fn):
    t0 = time.monotonic()
    try:
        out = fn()
    except Exception as e:                     # 探针不替被测代码背异常
        print(f"  [{time.monotonic() - t0:7.2f}s] {name}  !! {type(e).__name__}: {e}",
              flush=True)
        return None
    print(f"  [{time.monotonic() - t0:7.2f}s] {name}", flush=True)
    return out


from config.settings import Settings                        # noqa: E402

print("=== 分步计时（真实资料库）===", flush=True)
st = step("Settings()", lambda: Settings())

from core.knowledge import KnowledgeBase                    # noqa: E402

kb = step("KnowledgeBase()", lambda: KnowledgeBase())
if kb is not None:
    print(f"      root = {kb.root}", flush=True)
    step("kb.load_index()", kb.load_index)
    docs = step("kb.list_documents()", lambda: kb.list_documents())
    if docs is not None:
        print(f"      文档数 = {len(docs)}", flush=True)

from ui.knowledge_dialog import KnowledgeDialog             # noqa: E402

def _dialog_probe():
    t0 = time.monotonic()
    dialog = KnowledgeDialog()
    print(f"      构造返回 = {time.monotonic() - t0:.2f}s（后台载入已启动）", flush=True)
    t0 = time.monotonic()
    dialog.wait_loaded()
    print(f"      后台载入完成 = {time.monotonic() - t0:.2f}s"
          f"（表格 {dialog.table.rowCount()} 行）", flush=True)
    return dialog


_t0 = time.monotonic()
step("KnowledgeDialog() 构造 + 载入（合计）", _dialog_probe)
print(f"  [{time.monotonic() - _t0:7.2f}s] 合计（含构造与载入）", flush=True)
print("\n结论：构造应在 1 秒内返回（点开就能看到界面）；载入是后台的事。"
      "任何**同步**超过 3 秒的阻塞都是用户能感觉到的卡（同类问题见连接对话框那次）。",
      flush=True)
