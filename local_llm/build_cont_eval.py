#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建**文档续写**评测集 —— 比问答题更灵敏的"知识是否进了权重"的测量。

为什么不只靠问答题
================
问答题（`build_eval_set.py`）测的是"能不能回答工程师问题"，受题面措辞、检索质量、
回答风格影响很大（基线实测 57 题里 base 只命中 1 题，两组几乎贴地，分辨率不足）。
续写测的是**逐字知识**：给一段厂商文档的开头，看模型能不能接出后面的原话。

两类样本，结论完全不同：
  · class=seen     参考续写**在训练语料里出现过** ⇒ 量"训练把文档灌进权重的效果"
  · class=heldout  参考续写**一次没出现**   ⇒ 量"泛化"（预期≈0，不能假装它会）

判分：模型续写是否包含参考串（NFKC + 去空白 + 大写后子串匹配）。

用法：`python local_llm/build_cont_eval.py`
输出：`D:\\ait_llm\\train\\cont_set.jsonl` + `cont_set.md`
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

VENDOR_LABEL = {"huawei": "华为", "h3c": "H3C", "ruijie": "锐捷",
                "juniper": "Juniper", "cisco": "Cisco", "tplink": "TP-Link"}
#: 参考续写的长度（字符）。太短会撞巧合，太长则模型几乎必然接不上。
REF_LEN = 32
#: 提示词给多少字的原文开头
PROMPT_LEN = 70


def norm(t: str) -> str:
    return re.sub(r"\s+", "", t or "").upper()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index-dir", default=r"D:\ait_llm\index")
    ap.add_argument("--train-dir", default=r"D:\ait_llm\train")
    ap.add_argument("--out", default=r"D:\ait_llm\train\cont_set.jsonl")
    ap.add_argument("--per-vendor-seen", type=int, default=8)
    ap.add_argument("--per-vendor-heldout", type=int, default=4)
    ap.add_argument("--seed", type=int, default=4242)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    print("载入训练语料（判定参考续写是否被训过）…")
    train_text = ""
    for name in ("cpt_corpus.txt", "sft_qa.jsonl"):
        p = os.path.join(a.train_dir, name)
        if os.path.exists(p):
            train_text += open(p, "r", encoding="utf-8", errors="replace").read()
    train_norm = norm(train_text)
    print(f"训练语料规范后 {len(train_norm)/1048576:.1f} MB")

    rows: list[dict] = []
    for vendor in ("huawei", "h3c", "ruijie", "juniper", "cisco", "tplink"):
        path = os.path.join(a.index_dir, f"{vendor}_chunks.jsonl")
        if not os.path.exists(path):
            continue
        est_lines = max(1.0, os.path.getsize(path) / 1024 / 1024 * 700)
        rate = min(0.15, max(0.004, 1500.0 / est_lines))
        seen_l, held_l = [], []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if rng.random() > rate:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:                                # noqa: BLE001
                    continue
                t = re.sub(r"^\[[^\]]{0,80}\]\s*", "", rec.get("text") or "").strip()
                if not (200 <= len(t) <= 1200):
                    continue
                # 在句号/分号处切，避免提示词正好断在词中间
                cut = t.rfind("。", PROMPT_LEN, PROMPT_LEN + 60)
                cut = cut + 1 if cut > 0 else PROMPT_LEN
                ref = t[cut:cut + REF_LEN].strip()
                if len(ref) < REF_LEN - 6:
                    continue
                item = {"vendor": vendor, "prompt_text": t[:cut].strip(), "reference": ref,
                        "source": rec.get("source") or rec.get("file") or ""}
                if norm(ref) in train_norm:
                    seen_l.append(item)
                else:
                    held_l.append(item)
        rng.shuffle(seen_l)
        rng.shuffle(held_l)
        for it in seen_l[: a.per_vendor_seen]:
            rows.append({**it, "class": "seen"})
        for it in held_l[: a.per_vendor_heldout]:
            rows.append({**it, "class": "heldout"})
        print(f"[{vendor:<7}] seen {min(len(seen_l), a.per_vendor_seen)} 条 / "
              f"heldout {min(len(held_l), a.per_vendor_heldout)} 条（候选 {len(seen_l)}/{len(held_l)}）")

    for n, r in enumerate(rows):
        r["id"] = f"{r['vendor']}-{r['class']}-{n:02d}"
        label = VENDOR_LABEL.get(r["vendor"], r["vendor"])
        r["prompt"] = (f"{label}产品文档片段（请直接续写下一段，不要解释）：\n{r['prompt_text']}")

    with open(a.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    md = os.path.splitext(a.out)[0] + ".md"
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(f"# 文档续写评测集（{len(rows)} 条）\n\n")
        for r in rows:
            fh.write(f"## [{r['class']}] {r['id']}\n**给**：{r['prompt_text']}\n\n"
                     f"**参考续写**：{r['reference']}\n\n- 源：`{r['source']}`\n\n")
    n_s = sum(1 for r in rows if r["class"] == "seen")
    print("-" * 60)
    print(f"共 {len(rows)} 条：seen {n_s}（训过）/ heldout {len(rows)-n_s}（没训过）")
    print(f"落盘：{a.out} 与 {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
