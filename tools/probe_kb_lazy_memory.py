# -*- coding: utf-8 -*-
"""资料库「惰性加载 + 可释放 + 单实例」探针。

背景（2026-10-17 实测）：`.index.json` 本机 676 MB（27,297 篇 / 518,512 段），
以前在 `KnowledgeBase.__init__` 里无条件加载 ⇒ 应用一启动就常驻好几 GB 内存；
再加上 `SessionManager._ensure_knowledge` 没锁，后台预热线程与检索请求各建一份，
主进程实测顶到 7.9 GB —— 用户要跑 EVE-NG / eNSP 模拟器时内存不够。

量的是**用户能看到的量**：进程 RSS（MB）与实例个数。
  ① new 一个 KnowledgeBase 不加载索引（RSS 不涨）
  ② 第一次真正用到（list_documents）才加载，RSS 明显上涨
  ③ release() 之后 RSS 明显回落，且再检索能自动重新加载（功能没坏）
  ④ 多线程同时首次用到 ⇒ 只建一个实例（防重复占内存）

跑法：python -u tools\\probe_kb_lazy_memory.py   （真实资料库 676 MB，约 1~3 分钟）
"""
import ctypes
import os
import sys
import tempfile
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 用**真实**资料库目录（要的就是它 676 MB 的真实体量）；全程只读，不写、不改。
REAL_KB = os.path.join(os.path.expanduser("~"), ".aiterminal", "knowledge")

FAIL, TOTAL = [], [0]


def check(name, cond, detail=""):
    TOTAL[0] += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAIL.append(name)


def rss_mb():
    """当前进程 RSS（MB）—— 直接复用产品代码里的量尺（它自己被踩过坑）"""
    return knowledge.process_rss_mb()


from core import knowledge                                             # noqa: E402
from core.knowledge import KnowledgeBase                               # noqa: E402

idx = os.path.join(REAL_KB, ".index.json")
print(f"资料库: {REAL_KB}")
print(f"  索引文件: {os.path.getsize(idx)/1048576:.1f} MB" if os.path.exists(idx)
      else "  !! 索引文件不存在，后面的体量断言会失真")

print("\n=== ① 只 new，不加载 ===")
base_rss = rss_mb()
kb = KnowledgeBase(root_dir=REAL_KB)
after_new = rss_mb()
check("new 之后没加载索引", kb._index == {} and kb._index_loaded is False,
      f"_index_loaded={kb._index_loaded}")
check("RSS 没因为 new 而暴涨", (after_new - base_rss) < 200,
      f"{base_rss:.0f} → {after_new:.0f} MB（+{after_new - base_rss:.0f}）")

print("\n=== ② 真正用到才加载 ===")
docs = kb.list_documents()
loaded_rss = rss_mb()
check("列目录触发了加载", kb._index_loaded is True and len(kb._index) > 0,
      f"{len(kb._index)} 篇 / 文件 {len(docs)} 个")
check("RSS 明显上涨（说明索引真的进来了）", (loaded_rss - after_new) > 500,
      f"{after_new:.0f} → {loaded_rss:.0f} MB（+{loaded_rss - after_new:.0f}）")
print(f"  索引自身估算占用: {kb.memory_mb():.0f} MB")

print("\n=== ③ 释放后回落，且还能用 ===")
freed = kb.release()
released_rss = rss_mb()
check("release 后索引被丢掉", kb._index == {} and kb._index_loaded is False)
check("RSS 明显回落", (loaded_rss - released_rss) > 500,
      f"{loaded_rss:.0f} → {released_rss:.0f} MB（-{loaded_rss - released_rss:.0f}，自报释放 {freed:.0f}）")
again = kb.list_documents()
check("释放后还能自动重新加载（功能没坏）",
      kb._index_loaded is True and len(again) == len(docs),
      f"重新列出 {len(again)} 个")
kb.release()

print("\n=== ④ 多线程同时首次用到 ⇒ 只建一个实例 ===")
import core.session as session_mod                                    # noqa: E402
from config.settings import Settings                                  # noqa: E402

tmp_cfg = os.path.join(tempfile.mkdtemp(prefix="ait-kb-lazy-"), "settings.json")
built, got = [], []
real_cls = session_mod.KnowledgeBase


class CountingKB(real_cls):
    def __init__(self, *a, **kw):
        built.append(1)
        super().__init__(*a, **kw)


session_mod.KnowledgeBase = CountingKB
sm = session_mod.SessionManager()
sm.settings = Settings(config_file=tmp_cfg)
sm.settings.set("knowledge_dir", REAL_KB)
sm.settings.set("knowledge_enabled", True)
check("临时配置里的资料库目录生效", os.path.abspath(sm.settings.knowledge_dir) == os.path.abspath(REAL_KB),
      sm.settings.knowledge_dir)


def use():
    got.append(sm._ensure_knowledge())


threads = [threading.Thread(target=use) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("8 个线程并发首次用到只建 1 个实例", len(built) == 1, f"实际建了 {len(built)} 个")
check("8 个线程拿到同一个对象", len({id(x) for x in got}) == 1, f"{len({id(x) for x in got})} 个不同对象")
sm.release_knowledge()
check("release_knowledge 把实例置空", sm._knowledge is None)
session_mod.KnowledgeBase = real_cls

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
