# -*- coding: utf-8 -*-
"""资料管理「厂家」维度的探针（章四）。

用户反馈："资料库里确实有，但界面列表没显示出来" / "分类需求分为哪个厂家的设备即可"。

覆盖：
  ① guess_vendor 纯函数：文件名认厂家
  ② 文件名认不出 ⇒ 读正文开头认（临时资料库，不碰真实库）
  ③ 缓存：第二次识别不再读正文；文件改了会重认
  ④ 真实资料库：厂家分布 + 识别耗时（会写入 .vendors.json 缓存，跟 .index.json 同级）

跑法：python -u tools\\probe_kb_vendor.py
"""
import json
import os
import sys
import time
import tempfile

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


from core.knowledge import KnowledgeBase, guess_vendor, vendor_label   # noqa: E402

print("=== ① 文件名认厂家（纯函数，不读盘）===")
cases = [
    ("华为坤灵数通解决方案_V100_04_zh_AZQ07277_part2.txt", "huawei"),
    ("CloudEngine S16700_V600R024C00_04_zh_AZN1013P_part10.txt", "huawei"),
    ("H3C S5560 命令参考.txt", "h3c"),
    ("S6850 配置手册.md", "h3c"),
    ("锐捷 RG-S5750 开局指导.md", "ruijie"),
    ("TP-LINK TL-SG1024D 说明书.txt", "tplink"),
    ("cisco catalyst 2960 config.md", "cisco"),
    ("随便一个文件.txt", "unknown"),
]
for text, want in cases:
    got = guess_vendor(text)
    check(f"{want:8s} ← {text[:38]}", got == want, f"得到 {got}")
check("中文名可读", vendor_label("huawei") == "华为" and vendor_label("unknown") == "未识别")

print("\n=== ② 文件名认不出 ⇒ 读正文开头（临时库）===")
tmp = tempfile.mkdtemp(prefix="ait-kb-")
def put(name, text):
    with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
        f.write(text)

put("100ge_qsfp28_optical_modules.html", "<title>华为 CloudEngine 光模块</title>")
put("notes_unknown.txt", "这是一份 H3C 新华三 Comware 的配置笔记")
put("plain.txt", "no vendor here at all")
kb = KnowledgeBase(root_dir=tmp)
docs = kb.list_documents()
by_name = {d["name"]: d for d in docs}
kb.identify_vendors(docs)
check("正文里认到华为", by_name["100ge_qsfp28_optical_modules.html"]["vendor"] == "huawei",
      by_name["100ge_qsfp28_optical_modules.html"]["vendor"])
check("正文里认到新华三", by_name["notes_unknown.txt"]["vendor"] == "h3c",
      by_name["notes_unknown.txt"]["vendor"])
check("真的认不出 ⇒ unknown", by_name["plain.txt"]["vendor"] == "unknown",
      by_name["plain.txt"]["vendor"])
cache_file = os.path.join(tmp, ".vendors.json")
check("写了缓存文件", os.path.exists(cache_file), cache_file)

print("\n=== ③ 缓存生效 + 内容变了会重认 ===")
with open(os.path.join(tmp, "plain.txt"), "w", encoding="utf-8") as f:
    f.write("现在写锐捷 Ruijie RGOS 的内容")
# 缓存按修改时间失效，粒度 0.5s；这里把时间戳推后 10s，确保真的失效
now = time.time() + 10
os.utime(os.path.join(tmp, "plain.txt"), (now, now))
t0 = time.time()
docs2 = kb.list_documents()
kb.identify_vendors(docs2)
by_name2 = {d["name"]: d for d in docs2}
check("改过的文件被重新识别", by_name2["plain.txt"]["vendor"] == "ruijie",
      by_name2["plain.txt"]["vendor"])
check("识别很快（走了缓存，未读全文）", (time.time() - t0) < 2.0,
      f"{time.time() - t0:.3f}s")
try:
    cached = json.load(open(cache_file, encoding="utf-8"))
    check("缓存是 {文件名: [mtime, 厂家]}", isinstance(cached.get("plain.txt"), list)
          and cached["plain.txt"][1] == "ruijie", str(cached.get("plain.txt")))
except Exception as e:
    check("缓存能读", False, f"{type(e).__name__}: {e}")

print("\n=== ④ 真实资料库：厂家分布 + 耗时 ===")
real = os.environ.get("AIT_KNOWLEDGE_DIR") or os.path.join(
    os.path.expanduser("~"), ".aiterminal", "knowledge")
if not os.path.isdir(real):
    print("  （真实资料库不在，跳过）")
else:
    kb2 = KnowledgeBase(root_dir=real)
    t0 = time.time()
    docs3 = kb2.list_documents()
    t_list = time.time() - t0
    t0 = time.time()
    kb2.identify_vendors(docs3)
    t_scan = time.time() - t0
    dist = {}
    for d in docs3:
        dist[d.get("vendor", "unknown")] = dist.get(d.get("vendor", "unknown"), 0) + 1
    print(f"  共 {len(docs3)} 篇；列文件 {t_list:.1f}s；认厂家 {t_scan:.1f}s")
    for vendor, count in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"    {vendor_label(vendor):12s} {count}")
    check("每篇都有厂家字段", all("vendor" in d for d in docs3))
    check("至少认出一家的资料", any(v != "unknown" for v in dist))
    t0 = time.time()
    kb2.identify_vendors(docs3)                        # 第二次应全走缓存
    t_again = time.time() - t0
    check("第二次（全缓存）明显更快", t_again < max(2.0, t_scan * 0.5),
          f"第一次 {t_scan:.1f}s → 第二次 {t_again:.1f}s")

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
