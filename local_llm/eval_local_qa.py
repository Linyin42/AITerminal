#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四组对比评测：**基线 / 基线+RAG / 微调 / 微调+RAG**。

判分：关键事实命中（`build_eval_set.py` 产的题集里每题带 2-3 条从源切片抽出来的事实）。
规范化 = NFKC + 去全部空白 + 大写（型号常被写成 `QSFP 100G CWDM4`，空白不算差异）。

两组用法（模型换档要重启 llama-server，所以分两次跑、最后合并报告）：
    python local_llm/eval_local_qa.py run --model base  --out D:\\ait_llm\\train\\eval_base.jsonl
    python local_llm/eval_local_qa.py run --model tuned --tier ait --out D:\\ait_llm\\train\\eval_tuned.jsonl
    python local_llm/eval_local_qa.py report --inputs D:\\ait_llm\\train\\eval_base.jsonl D:\\ait_llm\\train\\eval_tuned.jsonl

每个组都同时记录：
  · 关键事实命中率（分 common / longtail 两类）
  · RAG 是否**召回**了源切片（失败是"没检索到"还是"检索到了没答对"，必须分开）
  · 回答长度 + 是否有"未找到/不确定"字样（过长且零命中 = 编造倾向）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths      # noqa: E402
import retrieve   # noqa: E402  （RAG 走的就是应用那条路径，不另写一份）

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CHAT_PORT = retrieve.CHAT_PORT
#: 拒答/对冲词：资料就在上下文里却回答"未涉及"——记录里的 `hedge` 字段据此置位。
#: ⚠ 曾漏了最高频的「未涉及」（话术是"厂商资料未涉及"），导致 base+rag 的拒答被记成 0 次；
#: 现在与 `analyze_eval_stats.HEDGES` 保持一致（两处都改时别忘了对方）。
HEDGES = ["未涉及", "未找到", "不确定", "无法确定", "没有相关", "未提及", "无法回答",
          "没有找到", "文档中未", "资料中未"]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").upper()
    return re.sub(r"\s+", "", s)


def chat_plain(question: str, max_tokens: int = 320) -> str:
    """不带检索上下文，直接问模型本身（= 微调到底学进去多少）。"""
    data = json.dumps({"model": "local-llm",
                       "messages": [{"role": "user", "content": question}],
                       "max_tokens": max_tokens, "temperature": 0.2, "stream": False},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{CHAT_PORT}/v1/chat/completions", data=data,
                                 method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "Authorization": f"Bearer {retrieve.API_KEY}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        res = json.loads(r.read().decode("utf-8", "replace"))
    return ((res.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()


def ask_rag(vendor: str, question: str, topk: int = 3) -> dict:
    """带检索：直接复用 retrieve.ask（应用用的同一条路）。"""
    return retrieve.ask(vendor, question, topk=topk, max_tokens=320)


def restart_server(tier: str) -> int:
    """换档要重启：先 --stop，再按档起。"""
    for args in (["--stop"], ["--stop-all"]):
        subprocess.run(paths.script_cmd(os.path.join(HERE, "run_server.py"), *args),
                       capture_output=True, text=True, timeout=180)
    c = paths.script_cmd(os.path.join(HERE, "run_server.py"), "--tier", tier)
    subprocess.run(c, capture_output=True, text=True, timeout=600)
    t0 = time.time()
    while time.time() - t0 < 240:
        if retrieve.chat_up():
            return 0
        time.sleep(5)
    print("!! 对话服务未起来（看 local_llm\\_llama_server.log）")
    return 3


def grade(row: dict, answer: str) -> dict:
    """判分：关键事实的**子串命中**（规范形）。

    同时给两个口径（报告里并列，不改主指标）：
      · `recall`（严格）= 命中的关键事实数 / 全部关键事实数。多事实题（一句里列了多条命令、
        小节里既有 `display version` 又有 `display startup`）只要漏一条就算没满分 —— 所以它是**下界**。
      · `hit_any`（宽松）= 命中**任一**关键事实即算答对。题面本身可两解时（同一小句覆盖多条
        命令），答出任一条其实是对的，却被严格口径判错 —— 两者的差就是这种"题面歧义"的量，
        不是模型能力差。
    """
    na = norm(answer)
    hits = [f for f in row["key_facts"] if norm(f) in na]
    return {"hit_facts": hits, "recall": len(hits) / max(1, len(row["key_facts"])),
            "hit_any": 1.0 if hits else 0.0,
            "answer_len": len(answer), "hedge": any(h in answer for h in HEDGES)}


def do_run(a) -> int:
    rows = [json.loads(l) for l in open(a.eval_set, encoding="utf-8") if l.strip()]
    if a.limit:
        rows = rows[: a.limit]
    if a.tier:
        print(f"切到档位 {a.tier} …")
        if restart_server(a.tier):
            return 3
    else:
        retrieve.ensure_chat_server()

    out = open(a.out, "w", encoding="utf-8")
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        # 组1：不带上下文（模型自己会不会）
        ans = chat_plain(row["question"], a.max_tokens)
        rec = {"id": row["id"], "vendor": row["vendor"], "class": row["class"],
               "qtype": row["qtype"], "group": f"{a.model}", "question": row["question"],
               "key_facts": row["key_facts"], "source": row["source"], "section": row["section"],
               "answer": ans, "rag_hits": []}
        rec.update(grade(row, ans))
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        print(f"[{i}/{len(rows)}] {a.model:<5} {row['id']:<14} 命中 {rec['recall']:.0%} "
              f"({len(rec['hit_facts'])}/{len(row['key_facts'])})", flush=True)

        # 组2：带检索
        r = ask_rag(row["vendor"], row["question"], topk=a.topk)
        ans2 = r["answer"]
        hits = [{"file": h["file"], "section": h["section"], "source": h["source"]}
                for h in r["hits"]]
        ctx = norm(r.get("context") or "")
        facts_ctx = [f for f in row["key_facts"] if norm(f) in ctx]
        rec2 = {"id": row["id"], "vendor": row["vendor"], "class": row["class"],
                "qtype": row["qtype"], "group": f"{a.model}+rag", "question": row["question"],
                "key_facts": row["key_facts"], "source": row["source"], "section": row["section"],
                "answer": ans2, "rag_hits": hits,
                # ⚠ 只看 section 会误判：不少切片的 section 其实是**文件名**，源在 source 里。
                "rag_recalled_source": any(
                    (row.get("section") and row["section"] in (h["section"] or ""))
                    or (row.get("source") and (
                        row["source"] in (h["source"] or "") or (h["source"] or "") in row["source"]))
                    for h in hits),
                # 资料到底有没有进上下文 —— 把"检索没答对"和"模型没读进去"分开，
                # 不然 RAG 组 0 分时说不清是谁的问题。
                "rag_facts_in_ctx": len(facts_ctx),
                "rag_facts_total": len(row["key_facts"])}
        rec2.update(grade(row, ans2))
        out.write(json.dumps(rec2, ensure_ascii=False) + "\n")
        out.flush()
        print(f"           {a.model}+rag 命中 {rec2['recall']:.0%}  "
              f"命中源切片={rec2['rag_recalled_source']}  "
              f"上下文含事实={len(facts_ctx)}/{len(row['key_facts'])}  用时 {time.time()-t0:.0f}s",
              flush=True)
    out.close()
    print(f"✓ {a.out}（{len(rows)*2} 条记录，用时 {(time.time()-t0)/60:.1f} 分钟）")
    return 0


def do_report(a) -> int:
    recs = []
    for p in a.inputs:
        recs.extend(json.loads(l) for l in open(p, encoding="utf-8") if l.strip())
    groups = sorted({r["group"] for r in recs})
    classes = ["common", "longtail"]
    lines = ["# 四组对比：本地模型自己会不会厂商产品文档", "",
             "命中率给两个口径：**严格** = 该题全部关键事实都在答案里（漏一条即 0，标点/多解会误伤，是下界）；",
             "**宽松** = 命中任一关键事实即算（题面可两解时的上界）。两者之差 = 题面歧义，不是模型能力。", "",
             "| 组 | 题数 | common 命中（严格/宽松） | longtail 命中（严格/宽松） | 平均回答字数 | 检索命中源切片 | 资料进上下文 |",
             "|---|---|---|---|---|---|---|"]
    for g in groups:
        sub = [r for r in recs if r["group"] == g]
        if not sub:
            continue
        row = [g, str(len(sub))]
        for c in classes:
            cs = [r for r in sub if r["class"] == c]
            if cs:
                strict = sum(r["recall"] for r in cs) / len(cs)
                # 旧记录（宽松口径上线前跑的）没有 hit_any：宁可不显示，也不当 0 报
                if all("hit_any" in r for r in cs):
                    loose = sum(r["hit_any"] for r in cs) / len(cs)
                    row.append(f"{strict:.0%} / {loose:.0%}（{len(cs)} 题）")
                else:
                    row.append(f"{strict:.0%}（{len(cs)} 题，无宽松口径）")
            else:
                row.append("-")
        row.append(f"{sum(r['answer_len'] for r in sub)/len(sub):.0f} 字")
        rr = [r for r in sub if r.get("rag_recalled_source") is not None]
        row.append(f"{sum(1 for r in rr if r['rag_recalled_source'])/len(rr):.0%}" if rr else "-")
        rc = [r for r in sub if r.get("rag_facts_total")]
        row.append(f"{sum(r['rag_facts_in_ctx'] for r in rc)/sum(r['rag_facts_total'] for r in rc):.0%}"
                   if rc else "-")
        lines.append("| " + " | ".join(row) + " |")
    # 判分口径分层：同一批记录换三种口径 —— 子串判分的下界到底低在哪，要看得见。
    #   · 严格：该题全部关键事实都出现
    #   · 主干：命令主干对、只缺参数（`display trunkfwdtbl eth-trunk` vs `… TRUNK-ID`）
    #   · 任一：命中任一关键事实（题面可两解）
    def _stem_hit(r: dict) -> bool:
        if r.get("qtype") != "cmd":
            return False
        na = norm(r.get("answer") or "")
        return len(na) >= 8 and any(norm(f).startswith(na) for f in r.get("key_facts") or [])

    lines += ["", "## 判分口径分层（同一批记录换口径，看下界低在哪）", "",
              "| 组 | 严格命中（全部事实） | 其中：命令主干对、只缺参数 | 宽松命中（任一事实） |",
              "|---|---|---|---|"]
    for g in groups:
        sub = [r for r in recs if r["group"] == g]
        if not sub:
            continue
        strict = sum(1 for r in sub if r["recall"] >= 1.0)
        stem = sum(1 for r in sub if r["recall"] < 1.0 and _stem_hit(r))
        if all("hit_any" in r for r in sub):
            loose = f"{sum(1 for r in sub if r['hit_any'])}/{len(sub)}"
        else:
            loose = "-"
        lines.append(f"| {g} | {strict}/{len(sub)} | {stem} | {loose} |")
    lines += ["", "## 逐题明细", "",
              "| 题 | 类别 | 组 | 命中率(严格) | 宽松 | 长度 | 检索命中源 | 上下文含事实 |",
              "|---|---|---|---|---|---|---|---|"]
    for r in sorted(recs, key=lambda r: (r["id"], r["group"])):
        frac = (f"{r['rag_facts_in_ctx']}/{r['rag_facts_total']}"
                if r.get("rag_facts_total") else "-")
        ha = f"{r['hit_any']:.0%}" if "hit_any" in r else "-"
        lines.append(f"| {r['id']} | {r['class']} | {r['group']} | {r['recall']:.0%} | "
                     f"{ha} | {r['answer_len']} | {r.get('rag_recalled_source', '-')} | {frac} |")
    # ⚠ 默认文件名固定 ⇒ 跑第二套题集（英文集）会把主集的明细报告覆盖掉（实测踩过）。
    md = getattr(a, "out", "") or os.path.join(os.path.dirname(a.inputs[0]), "eval_report.md")
    open(md, "w", encoding="utf-8").write("\n".join(lines))
    print("\n".join(lines[:lines.index("## 逐题明细") - 1]))
    print(f"\n✓ 报告：{md}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("run")
    r.add_argument("--eval-set", default=r"D:\ait_llm\train\eval_set.jsonl")
    r.add_argument("--model", default="base", choices=["base", "tuned"])
    r.add_argument("--tier", default="", help="非空则先重启 llama-server 到这个档位")
    r.add_argument("--out", required=True)
    r.add_argument("--topk", type=int, default=3)
    r.add_argument("--max-tokens", type=int, default=320)
    r.add_argument("--limit", type=int, default=0)
    p = sub.add_parser("report")
    p.add_argument("--inputs", nargs="+", required=True)
    p.add_argument("--out", default="", help="markdown 输出路径（默认 <inputs 目录>/eval_report.md）")
    a = ap.parse_args()
    return do_run(a) if a.mode == "run" else do_report(a)


if __name__ == "__main__":
    sys.exit(main())
