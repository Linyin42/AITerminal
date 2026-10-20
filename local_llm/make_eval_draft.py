#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从语料里抽样「命令参考类」切片，供**人工**据此编写评测题。

为什么要人工写题（而不是从语料里自动出题）：
    自动出题的"标准答案"就是语料本身 ⇒ 评测必然偏向"记住语料"，
    无法区分「模型真懂」与「复述训练样本」，也无法发现幻觉。
    人工题面写成工程师的真实问法、标准答案只记**关键事实点**（可 grep 复核），
    这样四组对比（基线 / 基线+RAG / 微调 / 微调+RAG）才有意义。

用法：
    python local_llm/make_eval_draft.py --per-vendor 12
输出：D:\\ait_llm\\train\\eval_draft.md（人工读它写 qa_set.jsonl）
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

CMD_RE = re.compile(
    r"\b(display|show|undo|interface|configure|config|set|reset|delete|create)\s+"
    r"([a-z][a-z0-9\-]{1,24}(?:\s+[a-z0-9\-\[\]\|/]{1,20}){0,2})")
TOPIC = re.compile(r"命令|command|cli|reference|参考|回显|显示", re.I)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index-dir", default=r"D:\ait_llm\index")
    ap.add_argument("--out", default=r"D:\ait_llm\train\eval_draft.md")
    ap.add_argument("--per-vendor", type=int, default=12)
    ap.add_argument("--seed", type=int, default=777)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    lines = ["# 评测题候选（人工据此写 qa_set.jsonl；标准答案只记关键事实点）", ""]
    for vendor in ("huawei", "h3c", "ruijie", "juniper", "cisco", "tplink"):
        path = os.path.join(a.index_dir, f"{vendor}_chunks.jsonl")
        if not os.path.exists(path):
            continue
        picked: list[dict] = []
        seen = set()
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if rng.random() > 0.02:          # 先粗筛，避免全文件 json 解析
                    continue
                try:
                    rec = json.loads(line)
                except Exception:                # noqa: BLE001
                    continue
                t = (rec.get("text") or "").strip()
                sec = (rec.get("section") or "").strip()
                if not (150 <= len(t) <= 1800) or not sec:
                    continue
                if not CMD_RE.search(t) or not (TOPIC.search(sec) or TOPIC.search(t[:200])):
                    continue
                key = t[:60]
                if key in seen:
                    continue
                seen.add(key)
                picked.append(rec)
                if len(picked) >= a.per_vendor * 3:
                    break
        rng.shuffle(picked)
        lines.append(f"## {vendor}（{len(picked)} 条候选，挑 {a.per_vendor} 条写题）")
        lines.append("")
        for rec in picked[: a.per_vendor]:
            txt = re.sub(r"\s+", " ", rec["text"])[:460]
            lines.append(f"- **{rec.get('section')}** ｜文件 `{rec.get('file')}` ｜源 `{rec.get('source')}`")
            lines.append(f"  - {txt}")
            lines.append("")

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"✓ 落盘：{a.out}（{len(lines)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
