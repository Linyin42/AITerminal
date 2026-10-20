#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档续写评测：量"厂商文档本身进了权重多少"（基线 vs 微调，两组，不带检索）。

判分：模型续写是否**包含参考串**（NFKC + 去空白 + 大写后子串匹配）。
两类样本分开统计：
  · seen     参考串在训练语料里出现过 ⇒ 量吸收（CPT/SFT 到底学进去多少）
  · heldout  参考串一次没出现       ⇒ 量泛化（预期≈0，不假装）

用法：
    python local_llm/eval_cont.py run --model base  --tier mid --out D:\\ait_llm\\train\\cont_base.jsonl
    python local_llm/eval_cont.py run --model tuned --tier ait --out D:\\ait_llm\\train\\cont_tuned.jsonl
    python local_llm/eval_cont.py report --inputs D:\\ait_llm\\train\\cont_base.jsonl D:\\ait_llm\\train\\cont_tuned.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from eval_local_qa import chat_plain, norm, restart_server   # noqa: E402  复用同一套实现


def do_run(a) -> int:
    rows = [json.loads(l) for l in open(a.eval_set, encoding="utf-8") if l.strip()]
    if a.limit:
        rows = rows[: a.limit]
    if a.tier:
        print(f"切到档位 {a.tier} …")
        if restart_server(a.tier):
            return 3
    out = []
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        ans = chat_plain(r["prompt"], max_tokens=a.max_tokens)
        hit = norm(r["reference"]) in norm(ans)
        out.append({**r, "model": a.model, "answer": ans, "hit": hit,
                    "answer_len": len(ans)})
        print(f"[{i}/{len(rows)}] {r['id']:<22} {r['class']:<8} {'命中' if hit else '未命中'}"
              f"  用时 {time.time()-t0:.0f}s")
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"✓ {a.out}（{len(out)} 条，用时 {(time.time()-t0)/60:.1f} 分钟）")
    return 0


def do_report(a) -> int:
    groups: dict[str, list[dict]] = {}
    for p in a.inputs:
        rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
        if rows:
            groups[rows[0]["model"]] = rows
    lines = ["# 文档续写评测（参考串命中率）", ""]
    for model, rows in groups.items():
        for cls in ("seen", "heldout"):
            sub = [r for r in rows if r["class"] == cls]
            if sub:
                hit = sum(1 for r in sub if r["hit"])
                lines.append(f"- **{model}** / {cls:<8} {hit}/{len(sub)} = "
                             f"{100*hit/len(sub):.1f}%")
    lines += ["", "| 题 | 类 | " + " | ".join(groups) + " |",
              "|---|---|" + "---|" * len(groups)]
    for r0 in next(iter(groups.values())):
        cells = []
        for rows in groups.values():
            m = next((x for x in rows if x["id"] == r0["id"]), None)
            cells.append("-" if m is None else ("命中" if m["hit"] else "未命中"))
        lines.append(f"| {r0['id']} | {r0['class']} | " + " | ".join(cells) + " |")
    md = os.path.join(os.path.dirname(os.path.abspath(a.inputs[0])), "cont_report.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines[:12]))
    print(f"\n报告落盘：{md}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--model", required=True, help="base | tuned")
    r.add_argument("--tier", default="")
    r.add_argument("--eval-set", default=r"D:\ait_llm\train\cont_set.jsonl")
    r.add_argument("--out", required=True)
    r.add_argument("--max-tokens", type=int, default=96)
    r.add_argument("--limit", type=int, default=0)
    p = sub.add_parser("report")
    p.add_argument("--inputs", nargs="+", required=True)
    a = ap.parse_args()
    return do_run(a) if a.cmd == "run" else do_report(a)


if __name__ == "__main__":
    sys.exit(main())
