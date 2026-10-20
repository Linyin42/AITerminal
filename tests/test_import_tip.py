"""资料管理「导入要求」提示 + 随包规范文档（用户 2026-10-17 要求）。

跑法：python tests\test_import_tip.py
判据：① 规范文档找得到（开发环境 <仓库>/docs/；随包那份由 make_portable 复制到包根）
      ② 没勾"不再显示"时该弹（should_show_import_tip()==True）
      ③ 勾过之后落盘 import_tip_dismissed=True，且从此不再弹
      ④ 资料库窗口上有常驻的「📖 导入规范」按钮
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication                    # noqa: E402

app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""), flush=True)


from ui.knowledge_dialog import (KnowledgeDialog, import_spec_path,   # noqa: E402
                                 IMPORT_TIP_KEY, SPEC_FILENAME)
from core.knowledge import KnowledgeBase                    # noqa: E402
from config.settings import Settings                        # noqa: E402

#: 隔离目录：默认 KnowledgeBase 去扫 ~/.aiterminal/knowledge（本机 27298 个文件），
#: 构造一次要很久 —— 用例必须换成空目录，测的是提示逻辑不是索引性能。
TMPDIR = tempfile.mkdtemp(prefix="ait-import-tip-")


def make_dialog():
    kb = KnowledgeBase()
    kb.set_root(TMPDIR)
    dialog = KnowledgeDialog(None, kb)
    dialog.wait_loaded()     # 构造是异步的（2026-10-17 起），提示逻辑要等载完
    return dialog

print("=== ① 规范文档位置 ===")
spec = import_spec_path()
check(f"找得到《{SPEC_FILENAME}》", bool(spec), spec)
if spec:
    size = os.path.getsize(spec)
    check(f"不是空占位（{size} B > 20 KB）", size > 20_000)
    with open(spec, encoding="utf-8") as fh:
        head = fh.read(60)
    check("开头就是规范正文", "资料导入规范" in head, head.splitlines()[0] if head else "")

print("=== ② / ③ 弹窗开关：不勾每次弹，勾了不再弹 ===")
st = Settings()
old = st.get(IMPORT_TIP_KEY, None)
try:
    st.set(IMPORT_TIP_KEY, False)
    st.save()
    d = make_dialog()
    check("没勾「不再显示」时该弹", d.should_show_import_tip() is True)
    check("提示在第一次显示时才挂（构造时不弹）", not hasattr(d, "_tip_shown"))
    d.dismiss_import_tip()
    check("勾了之后落盘为 True",
          Settings().get(IMPORT_TIP_KEY) is True,
          f"{IMPORT_TIP_KEY}={Settings().get(IMPORT_TIP_KEY)!r}")
    check("勾了之后不再弹", d.should_show_import_tip() is False)
    d3 = make_dialog()
    check("重开资料管理仍不弹（配置已落盘）", d3.should_show_import_tip() is False)
finally:
    st.set(IMPORT_TIP_KEY, old if old is not None else False)
    st.save()
    check("用例跑完把配置还原（不污染用户设置）",
          Settings().get(IMPORT_TIP_KEY) == (old if old is not None else False))

print("=== ④ 常驻入口 ===")
d4 = make_dialog()
btn = getattr(d4, "btn_import_spec", None)
check("资料库目录行有「📖 导入规范」按钮",
      btn is not None and "导入规范" in btn.text(),
      btn.text() if btn is not None else "（没有这个按钮）")

shutil.rmtree(TMPDIR, ignore_errors=True)

print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项: " + "；".join(FAIL))
sys.exit(1 if FAIL else 0)
