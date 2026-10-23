# -*- coding: utf-8 -*-
"""找出「已被消化」的文档目录并删除（用户指令：用过的文档就删了）。

判定「用过」= 满足任一条：
  1. `_vendor_cmd/<vendor>/commands.jsonl` 里某条记录的 source 指向该目录；
  2. `knowledge/<vendor>/tech/**/*.md` 里有 `<!-- source: <目录>/... -->` 注释（说明已被写成知识卡片，
     并且这些卡片已经切进 chunks / 训练语料）。

用法：
    python docs_purge_used.py --dry-run        # 只统计（默认）
    python docs_purge_used.py --purge          # 真删

安全：
  · 只删 `_vendor_docs/<vendor>_*` 下的目录，且路径的父目录必须是 _vendor_docs；
  · 删除前把清单（目录名 + 文件数 + 体积）写到 %TEMP%\docs_purged_<时间>.tsv；
  · 不在两份清单里的目录一律不动。
"""
import argparse
import io
import json
import os
import re
import shutil
import sys
import time

CMD_ROOT = r"C:\Users\linyin\deepseek_harness\_vendor_cmd"
DOCS_ROOT = r"C:\Users\linyin\deepseek_harness\_vendor_docs"
KB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge")

SRC_RE = re.compile(r"<!--\s*source:\s*([^/\s>]+)/")


def doc_names_from_commands(vendor):
    path = os.path.join(CMD_ROOT, vendor, "commands.jsonl")
    out = set()
    if not os.path.isfile(path):
        return out
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            src = str(obj.get("source") or "")
            head = src.split("/", 1)[0].split("#", 1)[0].strip()
            if head:
                out.add(head)
    return out


def doc_names_from_knowledge(vendor):
    root = os.path.join(KB_ROOT, vendor, "tech")
    out = set()
    if not os.path.isdir(root):
        return out
    for base, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            try:
                with io.open(os.path.join(base, fn), encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = SRC_RE.search(line)
                        if m:
                            out.add(m.group(1).strip())
            except OSError:
                continue
    return out


def _long(path):
    """Windows 长路径前缀：这些文档目录嵌套很深，超过 260 字符时普通 API 直接报"找不到"。"""
    path = os.path.abspath(path)
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        return "\\\\?\\" + path
    return path


def dir_size(path):
    n = 0
    total = 0
    for base, _dirs, files in os.walk(_long(path)):
        for fn in files:
            try:
                n += os.path.getsize(os.path.join(base, fn))
            except OSError:
                continue          # 长路径/权限问题：体积算不出来就不算，别中断统计
            total += 1
    return n, total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--purge", action="store_true", help="真删（默认只统计）")
    a = ap.parse_args()

    vendors = sorted(d for d in os.listdir(CMD_ROOT) if os.path.isdir(os.path.join(CMD_ROOT, d)))
    used, per_vendor = set(), {}
    for v in vendors:
        c = doc_names_from_commands(v)
        k = doc_names_from_knowledge(v)
        per_vendor[v] = (len(c), len(k), len(c | k))
        used |= c | k

    dirs = [d for d in os.listdir(DOCS_ROOT) if os.path.isdir(os.path.join(DOCS_ROOT, d))]
    targets = [d for d in dirs if d in used]
    keep = [d for d in dirs if d not in used]
    print("厂商引用统计（commands / knowledge / 合计）:")
    for v, (c, k, u) in sorted(per_vendor.items()):
        print("  %-8s %5d / %5d / %5d" % (v, c, k, u))
    print("\n_vendor_docs 目录 %d 个：可删 %d，保留 %d" % (len(dirs), len(targets), len(keep)))

    total_bytes = total_files = 0
    rows = []
    t0 = time.time()
    for d in sorted(targets):
        full = os.path.join(DOCS_ROOT, d)
        parent = os.path.dirname(os.path.abspath(full))
        if parent.rstrip("\\") != DOCS_ROOT:
            print("  ! 跳过（父目录不对）", full)
            continue
        n, cnt = dir_size(full)
        total_bytes += n
        total_files += cnt
        rows.append((d, cnt, n))
        if time.time() - t0 > 20:
            print("  ... 已统计 %d 个" % len(rows))
            t0 = time.time()
    print("可删体积 %.2f GB / %d 文件" % (total_bytes / 1024 ** 3, total_files))

    stamp = time.strftime("%Y%m%d_%H%M")
    manifest = os.path.join(os.environ["TEMP"], "docs_purged_%s.tsv" % stamp)
    with io.open(manifest, "w", encoding="utf-8") as f:
        f.write("dir\tfiles\tbytes\n")
        for d, cnt, n in rows:
            f.write("%s\t%d\t%d\n" % (d, cnt, n))
    print("清单:", manifest)

    if not a.purge:
        print("\n（未加 --purge，只统计）")
        return 0

    freed = errors = 0
    for i, (d, _cnt, n) in enumerate(rows, 1):
        full = os.path.join(DOCS_ROOT, d)
        try:
            shutil.rmtree(_long(full))
            freed += n
        except OSError as exc:
            errors += 1
            print("  删除失败 %s: %s" % (d, exc))
        if i % 100 == 0:
            print("  ... %d/%d，已释放 %.1f GB" % (i, len(rows), freed / 1024 ** 3))
    print("删除完成：%d 个目录，释放 %.2f GB，失败 %d" % (len(rows) - errors, freed / 1024 ** 3, errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
