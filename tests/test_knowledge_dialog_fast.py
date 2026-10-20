"""打开资料管理不能卡（用户 2026-10-17 报过连接对话框同类问题）。

背景（2026-10-17 实测）：本机 ~/.aiterminal/knowledge 有 27297 篇资料。
  · `refresh_table()` 逐行调 `document_category/document_tags` → `find_note()`
    → `load_notes_cached()` 全库解析笔记 ⇒ 填一次表 200 秒都没完（真凶）。
  · 表格逐行插 8 列 ⇒ 2.7 万行本身就慢。
本用例守两件事：
  ① 列表首屏封顶 MAX_TABLE_ROWS 行，被截断时状态行明说「已显示前 N 篇 / 共 M 篇」；
  ② 填一次表不再全库解析笔记（正常应在数秒内完成，这里给 15s 的宽松上限做回归护栏）。

注意：`KnowledgeDialog()` 构造本身仍要载入 KB 索引（本机 ~10s），那是另一件事，
      不在本用例覆盖范围（见 tools/probe_kb_dialog_open.py 的分步计时）。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication                    # noqa: E402

app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""), flush=True)


from ui.knowledge_dialog import KnowledgeDialog, MAX_TABLE_ROWS   # noqa: E402

FILL_LIMIT = 15.0

print("=== ① 填表不再全库解析笔记，构造也不再阻塞 ===")
t0 = time.monotonic()
dialog = KnowledgeDialog()          # 2026-10-17 起：立即返回，载入在后台线程
construct = time.monotonic() - t0
t0 = time.monotonic()
loaded = dialog.wait_loaded()       # 等后台载完（GUI 里不用调，测试没有事件循环）
load = time.monotonic() - t0
check("构造立刻返回（载入已挪到后台线程；修复前 13.9~16.4s 全耗在构造里）",
      construct < 2.0, f"{construct:.2f}s")
check("后台载入能完成且填了表", loaded and dialog.table.rowCount() > 0,
      f"载入 {load:.2f}s / 画了 {dialog.table.rowCount()} 行")
t0 = time.monotonic()
dialog.refresh_table()
fill = time.monotonic() - t0
rows = dialog.table.rowCount()
docs = len(dialog.kb.list_documents())
print(f"      （真实资料 {docs} 篇：构造 {construct:.2f}s，后台载入 {load:.2f}s，"
      f"填表 {fill:.2f}s，画了 {rows} 行）", flush=True)
check(f"填一次表在 {FILL_LIMIT:.0f}s 内（修复前 200s 没跑完）", fill < FILL_LIMIT,
      f"{fill:.2f}s")
check("按需解析：没有把全部资料都解析一遍（缓存远小于总篇数）",
      len(dialog._note_cache) < max(2000, docs // 5) if docs > MAX_TABLE_ROWS else True,
      f"cache={len(dialog._note_cache)} / docs={docs}")

print("=== ② 首屏封顶 + 明说 ===")
check(f"首屏不超过 {MAX_TABLE_ROWS} 行", rows <= MAX_TABLE_ROWS, f"rows={rows}")
if docs > MAX_TABLE_ROWS:
    check('被截断时状态行说明「已显示前 N 篇 / 共 M 篇」',
          "已显示前" in dialog.stats_label.text() and str(docs) in dialog.stats_label.text(),
          dialog.stats_label.text())
else:
    print(f"      （本机资料 {docs} 篇 ≤ {MAX_TABLE_ROWS}，截断分支不适用，跳过）", flush=True)

print("=== ③ 入口没被砍 ===")
check("检索测试页签还在", dialog.tabs.count() >= 2, f"tabs={dialog.tabs.count()}")
dialog.refresh_stats()
check("统计信息能刷新出来（不是载入占位）", "篇资料" in dialog.stats_label.text(),
      dialog.stats_label.text())

print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项: " + "；".join(FAIL))
sys.exit(1 if FAIL else 0)
