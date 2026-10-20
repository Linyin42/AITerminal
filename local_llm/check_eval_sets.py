#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验两套评测集的"训过 / 没训过"分类是否与**当前**训练语料一致。

为什么必须单独校验：题集的 common/longtail、seen/heldout 标签是在**构建时**按当时的
训练语料算出来的（`build_eval_set.py` / `build_cont_eval.py`）。之后语料被重建过
（剥掉索引前缀，见 `build_train_set.py`）—— 只要语料变了，标签就可能失效，而
"分类错了"会让整份对比报告失去语义（把训过的样本当泛化测，结论就反了）。

判据：
  · cont_set  seen    参考续写必须在语料里能整串找到
  · cont_set  heldout 一定找不到
  · eval_set  common  关键事实都在语料里
  · eval_set  longtail 关键事实都不在语料里

用法：python local_llm/check_eval_sets.py
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import unicodedata
import argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TRAIN = r"D:\ait_llm\train"


def norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "").upper())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="分类不符即返回 1（默认只在语料是重建件时告警，因为重建抽样不同会产生预期漂移）")
    a = ap.parse_args()
    txt = ""
    for name in ("cpt_corpus.txt", "sft_qa.jsonl"):
        p = os.path.join(TRAIN, name)
        txt += open(p, encoding="utf-8", errors="replace").read()
    corpus = norm(txt)
    print(f"训练语料规范后 {len(corpus)/1048576:.1f} MB")

    bad = 0
    cpath = os.path.join(TRAIN, "cont_set.jsonl")
    if os.path.exists(cpath):
        rows = [json.loads(l) for l in open(cpath, encoding="utf-8") if l.strip()]
        mism = []
        for r in rows:
            inn = norm(r["reference"]) in corpus
            want = (r["class"] == "seen")
            if inn != want:
                mism.append((r["id"], r["class"], "在语料里" if inn else "不在语料里"))
        n_seen = sum(1 for r in rows if r["class"] == "seen")
        print(f"cont_set：{len(rows)} 条（seen {n_seen} / heldout {len(rows)-n_seen}）；"
              f"分类不符 {len(mism)} 条" + (f"  例：{mism[:3]}" if mism else ""))
        bad += len(mism)

    epath = os.path.join(TRAIN, "eval_set.jsonl")
    if os.path.exists(epath):
        rows = [json.loads(l) for l in open(epath, encoding="utf-8") if l.strip()]
        mism = []
        for r in rows:
            inn = [norm(f) in corpus for f in r["key_facts"]]
            if r["class"] == "common" and not all(inn):
                mism.append((r["id"], "common 但有事实不在语料"))
            if r["class"] == "longtail" and any(inn):
                mism.append((r["id"], "longtail 但有事实在语料"))
        from collections import Counter
        print(f"eval_set：{len(rows)} 条 {dict(Counter(r['class'] for r in rows))} "
              f"| {dict(Counter(r['qtype'] for r in rows))}；分类不符 {len(mism)} 条"
              + (f"  例：{mism[:3]}" if mism else ""))
        bad += len(mism)

    # 英文题集（cisco/juniper）：common/longtail 与语料的对应关系同样要验 ——
    # 这两家的事实是"逐字字段值"（`Return (decimal 13)`、`protocols mld interface interface-name`），
    # 分类错了会让"训练语料里到底有没有"这个前提整个不成立。
    for extra in ("eval_set_en.jsonl", "eval_set_s1234.jsonl"):
        xp = os.path.join(TRAIN, extra)
        if not os.path.exists(xp):
            continue
        rows = [json.loads(l) for l in open(xp, encoding="utf-8") if l.strip()]
        mism = []
        for r in rows:
            inn = [norm(f) in corpus for f in r["key_facts"]]
            if r["class"] == "common" and not all(inn):
                mism.append((r["id"], "common 但有事实不在语料"))
            if r["class"] == "longtail" and any(inn):
                mism.append((r["id"], "longtail 但有事实在语料"))
        print(f"{extra}：{len(rows)} 条；分类不符 {len(mism)} 条"
              + (f"  例：{mism[:3]}" if mism else ""))
        bad += len(mism)

    if bad and not a.strict:
        print(f"⚠ 共 {bad} 条分类不符 —— 但语料记录文件可能是**重建件**（重建的 reservoir 抽样与标注时"
              f"那次不同，段落组成会差几 %）。标签本身是对**标注时**的语料判的，重建件判不过属预期漂移；"
              f"要按硬失败处理请加 --strict。")
    else:
        print("✓ 各套题集分类与当前语料一致" if not bad else f"!! 共 {bad} 条分类不符，需重建题集")
    return 1 if (bad and a.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
