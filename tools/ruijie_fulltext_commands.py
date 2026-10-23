#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从锐捷 `fulltext.txt` 类文档里抽命令条目（补 `_vendor_cmd/ruijie/commands.jsonl`）。

为什么需要它
============
`tools/doc_to_commands.py` 只认 `章/*.htm` 的文档结构；锐捷的「命令参考」是
**单文件 fulltext.txt**（几百份），它一份都读不了。结果：锐捷索引里只有 3,377 条命令，
其中 `show …` 只有 **2 条**（`show efd device` / `show license hostid`）——
用户最常用的 `show vlan` / `show version` 全部没有补全候选。
（2026-10-23 功能测试发现：`suggest('show vlan','ruijie')` 返回 0 条。）

抽取规则（**故意保守**，宁可少抽不要抽错）
==========================================
只认"整行就是一条命令"的行：
    show vlan [ id vlan-id ]        → command=show vlan, syntax=show vlan [ id vlan-id ]
    display interface brief         → command=display interface brief
- 行首不能有空格（缩进的是正文/参数表，不是命令标题）
- 动词只认 show / display（配置类命令已有 3,377 条，不动）
- 命令名最多 4 个词，后面只允许接 `[...]` 参数组或 `{ a | b }`
- 整行只含命令字符，且不含中文（中文行是说明文字）
- 同一个 (command, syntax) 只写一次

用法
====
    python tools/ruijie_fulltext_commands.py --doc "<_vendor_docs 下的目录名>"            # 只统计
    python tools/ruijie_fulltext_commands.py --doc "<...>" --append --model AC            # 写入（自动备份）
    ... --append --all      # 扫所有含 fulltext.txt 的锐捷「命令参考」目录

写之前一定备份：`--append` 会把原文件复制成 `commands.jsonl.bak_<时间戳>`。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import shutil
import sys
import time

DOCS_ROOT = r"C:\Users\linyin\deepseek_harness\_vendor_docs"
CMD_ROOT = r"C:\Users\linyin\deepseek_harness\_vendor_cmd"

#: 整行就是命令：动词 + 1~4 个词 + 可选 `[...]` / `{a|b}` 尾巴
LINE_RE = re.compile(
    r"^(?P<verb>show|display)\s+(?P<name>[a-z0-9][a-z0-9\-]*(?:\s+[a-z0-9][a-z0-9\-]*){0,3})"
    r"(?P<tail>(?:\s+(?:\[[^\[\]]*\]|\{[^{}]*\}))*)\s*$"
)
#: 允许出现在命令行里的字符（有别的字符就当它是正文）
LEGAL = re.compile(r"^[A-Za-z0-9 _\-/.,:\[\]{}|<>*#]+$")


def extract(path: str, model: str, doc_name: str):
    out, seen = [], set()
    with io.open(path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.rstrip("\n").rstrip()
            if not line or line[0].isspace():
                continue
            if len(line) > 120 or not LEGAL.match(line):
                continue
            m = LINE_RE.match(line)
            if not m:
                continue
            verb = m.group("verb")
            name = re.sub(r"\s+", " ", m.group("name")).strip().lower()
            command = (verb + " " + name).strip()
            syntax = re.sub(r"\s+", " ", line).strip()
            key = (command, syntax)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "vendor": "ruijie",
                "model": model,
                "command": command,
                "syntax": syntax,
                "view": "特权模式",
                "feature": "命令参考",
                "kind": "command",
                "source": "%s/fulltext.txt#L%d" % (doc_name, lineno),
            })
    return out


def doc_paths(pattern: str):
    base = os.path.join(DOCS_ROOT, pattern)
    if os.path.isdir(base):
        return [(os.path.basename(base), os.path.join(base, "fulltext.txt"))]
    hits = []
    for d in sorted(glob.glob(os.path.join(DOCS_ROOT, pattern))):
        ft = os.path.join(d, "fulltext.txt")
        if os.path.isdir(d) and os.path.isfile(ft):
            hits.append((os.path.basename(d), ft))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", required=True, help="_vendor_docs 下的目录名（可用通配符）")
    ap.add_argument("--model", default="RGOS", help="写进 model 字段，如 S2928G-E / AC")
    ap.add_argument("--append", action="store_true", help="追加进 commands.jsonl（自动备份）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show-new", type=int, default=0,
                    help="打印前 N 条新增命令（抽查质量用，不写盘）")
    ap.add_argument("--allow-dup", action="store_true",
                    help="允许重复写已存在的命令名（默认跳过：补全按命令名索引，重复的只有浪费）")
    a = ap.parse_args()

    docs = doc_paths(a.doc)
    if not docs:
        print("✗ 没找到匹配的目录（或目录里没有 fulltext.txt）")
        return 1
    print("匹配目录 %d 个" % len(docs))

    rows, per_doc = [], []
    for i, (name, path) in enumerate(docs, 1):
        got = extract(path, a.model, name)
        per_doc.append((name, len(got)))
        rows.extend(got)
        if len(docs) > 20 and i % 25 == 0:
            print("  ... %d/%d 份，累计 %d 条" % (i, len(docs), len(rows)))
    #: 同一轮里同一条命令只留第一份：补全索引只按命令名建键
    uniq, seen_cmd = [], set()
    for r in rows:
        if r["command"] in seen_cmd:
            continue
        seen_cmd.add(r["command"])
        uniq.append(r)
    print("抽出条目 %d 条 → 去重后命令名 %d 条" % (len(rows), len(uniq)))
    rows = uniq
    if a.limit:
        rows = rows[:a.limit]
    for name, n in per_doc[:10]:
        print("  %-70s %5d" % (name[:70], n))
    if not rows:
        return 0

    verbs = {}
    for r in rows:
        verbs[r["command"].split()[0]] = verbs.get(r["command"].split()[0], 0) + 1
    print("动词分布:", verbs)
    print("样例:", [r["syntax"] for r in rows[:5]])

    dest = os.path.join(CMD_ROOT, "ruijie", "commands.jsonl")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    #: 已经入过库的命令名（跨文档重复极多：312 份命令参考里 228,101 条命中只有 2,867 个命令名）
    existing = set()
    if os.path.isfile(dest):
        for line in io.open(dest, encoding="utf-8"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("kind") == "command":
                c = str(obj.get("command") or "").strip().lower()
                if c:
                    existing.add(c)
    if not a.allow_dup:
        keep = [r for r in rows if r["command"] not in existing]
        print("跳过已入库命令 %d 条，新增 %d 条" % (len(rows) - len(keep), len(keep)))
        rows = keep
    if a.show_new:
        print("新增样例（前 %d 条）:" % a.show_new)
        for r in rows[:a.show_new]:
            print("   %-46s %s" % (r["syntax"][:46], r["source"].split("/")[0][:52]))
    if not a.append:
        print("\n（未加 --append，只统计。目标: %s）" % dest)
        return 0
    if not rows:
        print("没有新命令要写。")
        return 0
    if os.path.isfile(dest):
        bak = dest + ".bak_" + time.strftime("%Y%m%d_%H%M")
        shutil.copy2(dest, bak)
        print("已备份 →", bak)
    before = sum(1 for _ in open(dest, encoding="utf-8")) if os.path.isfile(dest) else 0
    with io.open(dest, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    after = sum(1 for _ in open(dest, encoding="utf-8"))
    print("commands.jsonl: %d → %d 行（+%d）" % (before, after, after - before))
    return 0


if __name__ == "__main__":
    sys.exit(main())
