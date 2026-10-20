# -*- coding: utf-8 -*-
"""量「首次检索」到底吃多少内存（_ensure_ready 的 per-chunk 缓存）。

背景：应用实测 7.9 GB 的主因不是索引本身（676 MB → 约 865 MB），
而是第一次检索时 `_ensure_ready()` 为 518,512 个片段各建了一份
`_chunk_cache` 条目（含 text 副本 + name_tokens 集合）与 `_token_cache`（一个 Counter）。
本脚本把两段分别量出来，供决定要不要继续优化。

跑法：python -u tools\\probe_kb_search_memory.py   （约 2~4 分钟）
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.knowledge import KnowledgeBase, process_rss_mb              # noqa: E402

REAL_KB = os.path.join(os.path.expanduser("~"), ".aiterminal", "knowledge")

print(f"起始 RSS = {process_rss_mb():.0f} MB", flush=True)
kb = KnowledgeBase(root_dir=REAL_KB)
print(f"new 之后 RSS = {process_rss_mb():.0f} MB（惰性：不该涨）", flush=True)

t0 = time.time()
docs = kb.list_documents()
print(f"加载索引：{len(kb._index)} 篇 / 用时 {time.time()-t0:.1f}s "
      f"/ RSS = {process_rss_mb():.0f} MB", flush=True)

t0 = time.time()
kb._ensure_ready()
print(f"_ensure_ready：{len(kb._chunk_cache)} 片段 / 用时 {time.time()-t0:.1f}s "
      f"/ RSS = {process_rss_mb():.0f} MB", flush=True)
print(f"  _token_cache 条目 = {len(kb._token_cache)}  _df 词表 = {len(kb._df)}", flush=True)

before_search = process_rss_mb()
t0 = time.time()
hits = kb.search("display interface brief", top_k=3)
print(f"首次 search：{len(hits)} 条 / 用时 {time.time()-t0:.1f}s "
      f"/ RSS = {process_rss_mb():.0f} MB（较上一步 +{process_rss_mb()-before_search:.0f}）",
      flush=True)

after_search = process_rss_mb()
freed = kb.release()
print(f"release：RSS = {process_rss_mb():.0f} MB（释放 {freed:.0f} MB，"
      f"检索态 {after_search:.0f} → {process_rss_mb():.0f}）", flush=True)
print(f"\n汇总：索引态 {process_rss_mb():.0f} MB（释放后）/ 检索态峰值约 {after_search:.0f} MB",
      flush=True)
