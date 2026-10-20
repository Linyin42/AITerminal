# -*- coding: utf-8 -*-
r"""评测显著性检验：把多套样本合成**配对问题**，判断"两个模型的差"是不是噪声。

背景（为什么需要它）：单套题集只有 56 题，换一批题（同厂商、同类别、只换抽样）就能让
"严格命中差"在 +2% 与 +23% 之间跳 —— 那是**换题抽样**的波动，不是模型差异。所以：
  · 只在**同一道题**上比较两个模型（配对），不同题之间不做跨样本相减；
  · 多套题集合并后算 McNemar 精确检验 + bootstrap 置信区间。

三层口径：
  · 严格 strict —— 该题全部关键事实都出现在答案里（漏一条即 0，标点/多解会误伤 ⇒ 下界）
  · 可用 usable —— 严格 ∪ "命令主干对、只缺参数"（子串判分对后者不给分，但它其实可用）
  · 拒答 hedge  —— 答案里出现"未涉及/未找到/不确定"等对冲词

用法：
    python local_llm/analyze_eval_stats.py
    python local_llm/analyze_eval_stats.py --train-dir D:\ait_llm\train \
        --pair eval_base.jsonl eval_tuned.jsonl --pair eval_base_s1234.jsonl eval_tuned_s1234.jsonl
"""
import argparse
import json
import math
import os
import random
import re
import unicodedata

#: 对冲词：资料就在上下文里，却回答"未涉及"—— 这是**拒答**，不是知识不足
HEDGES = ["未涉及", "未找到", "不确定", "无法确定", "没有相关", "未提及", "无法回答",
          "没有找到", "文档中未", "资料中未"]
#: 默认三套（互不重叠：A∩B 只有 2 题，C 换了种子）
DEFAULT_PAIRS = [("eval_base_v2new.jsonl", "eval_tuned_v2new.jsonl"),
                 ("eval_base.jsonl", "eval_tuned.jsonl"),
                 ("eval_base_s1234.jsonl", "eval_tuned_s1234.jsonl")]


def norm(t: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", t or "").upper())


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def stem_only(r: dict) -> bool:
    """命令主干对、只缺参数（`display trunkfwdtbl eth-trunk` vs `… TRUNK-ID`）。"""
    if r.get("qtype") != "cmd":
        return False
    na = norm(r.get("answer"))
    return len(na) >= 8 and any(norm(f).startswith(na) for f in r.get("key_facts") or [])


def flags(r: dict) -> dict:
    strict = r["recall"] >= 1.0
    return {"strict": strict, "usable": strict or stem_only(r),
            "hedge": any(h in (r.get("answer") or "") for h in HEDGES)}


def mcnemar(b: int, c: int) -> float:
    """双侧精确 McNemar（b/c = 不一致对的两种方向）。"""
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2.0 * sum(math.comb(n, i) for i in range(min(b, c) + 1)) / (2.0 ** n))


def boot_ci(diffs: list[int], rng: random.Random, iters: int = 10000):
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-dir", default=r"D:\ait_llm\train")
    ap.add_argument("--pair", nargs=2, action="append", default=None,
                    help="base 记录文件 + tuned 记录文件（可给多次）")
    ap.add_argument("--dedup-question", action="store_true",
                    help="跨样本按题面去重（多套抽样含同一道题时必须开，否则那道题被算两遍）")
    a = ap.parse_args()
    pairs = a.pair or DEFAULT_PAIRS
    rng = random.Random(42)
    pooled = {k: {"b": 0, "c": 0, "d": []} for k in ("strict", "usable", "hedge")}
    #: 按类别（common = 训练语料里有 / longtail = 一次没出现）分开累计。这两类问的是**不同能力**：
    #: common 可以靠"背"，longtail 只能靠"读上下文" —— 一个模型若 common 不差、longtail 明显差，
    #: 说明它在靠记忆答而不是在读眼前的资料。
    by_class: dict[tuple[str, str], dict] = {}

    # ---- 收集配对（先收集再统计：`--dedup-question` 要跨样本按题面去重）----
    groups: dict[str, list[tuple[dict, dict]]] = {}
    used = 0
    for bf, tf in pairs:
        p1, p2 = os.path.join(a.train_dir, bf), os.path.join(a.train_dir, tf)
        if not (os.path.exists(p1) and os.path.exists(p2)):
            print(f"（缺 {bf} 或 {tf}，跳过）")
            continue
        used += 1
        base = {r["id"]: r for r in load(p1) if r["group"] == "base+rag"}
        tuned = {r["id"]: r for r in load(p2) if r["group"] == "tuned+rag"}
        groups.setdefault(bf.split("_", 1)[-1], []).extend(
            (base[i], tuned[i]) for i in base if i in tuned)

    # ⚠ 两套抽样常含同一道题：不去重会把它算两遍（等于给这道题加权）。
    # 把多套题并起来提高 n 时必须开 --dedup-question，否则 p 值是假的。
    if a.dedup_question:
        seen_q: set[str] = set()
        for lab, items in groups.items():
            keep = []
            for br, tr in items:
                k = norm(br.get("question") or "")
                if k in seen_q:
                    continue
                seen_q.add(k)
                keep.append((br, tr))
            groups[lab] = keep

    print("| 样本 | 配对题数 | 口径 | 基线 | 微调 | 差 | 不一致对(基线有/微调有) | McNemar p | 95% CI |")
    print("|---|---|---|---|---|---|---|---|---|")
    for lab, items in groups.items():
        for tier in ("strict", "usable", "hedge"):
            fb = [flags(br)[tier] for br, _ in items]
            ft = [flags(tr)[tier] for _, tr in items]
            n = len(items)
            b = sum(1 for x, y in zip(fb, ft) if x and not y)
            c = sum(1 for x, y in zip(fb, ft) if y and not x)
            diffs = [int(y) - int(x) for x, y in zip(fb, ft)]
            lo, hi = boot_ci(diffs, rng)
            print(f"| {lab} | {n} | {tier} | {sum(fb)}/{n} | {sum(ft)}/{n} | "
                  f"{(sum(ft) - sum(fb)) / n:+.0%} | {b}/{c} | {mcnemar(b, c):.4f} | "
                  f"[{lo:+.0%}, {hi:+.0%}] |")
            pooled[tier]["b"] += b
            pooled[tier]["c"] += c
            pooled[tier]["d"].extend(diffs)
        for cls in ("common", "longtail"):
            sel = [(br, tr) for br, tr in items if br.get("class") == cls]
            if not sel:
                continue
            for tier in ("strict", "usable"):
                fb = [flags(br)[tier] for br, _ in sel]
                ft = [flags(tr)[tier] for _, tr in sel]
                acc = by_class.setdefault((cls, tier), {"b": 0, "c": 0, "d": [], "n": 0,
                                                        "base": 0, "tuned": 0})
                acc["b"] += sum(1 for x, y in zip(fb, ft) if x and not y)
                acc["c"] += sum(1 for x, y in zip(fb, ft) if y and not x)
                acc["n"] += len(sel)
                acc["base"] += sum(fb)
                acc["tuned"] += sum(ft)
                acc["d"].extend(int(y) - int(x) for x, y in zip(fb, ft))

    if by_class:
        print("\n按类别拆（common = 训练语料里有，可背；longtail = 语料里没有，只能读上下文）：")
        print("| 类别 | 口径 | 基线 | 微调 | 差 | 不一致对(基线有/微调有) | McNemar p |")
        print("|---|---|---|---|---|---|---|")
        for cls in ("common", "longtail"):
            for tier in ("strict", "usable"):
                v = by_class.get((cls, tier))
                if not v or not v["n"]:
                    continue
                print(f"| {cls} | {tier} | {v['base']}/{v['n']} | {v['tuned']}/{v['n']} | "
                      f"{(v['tuned'] - v['base']) / v['n']:+.0%} | {v['b']}/{v['c']} | "
                      f"{mcnemar(v['b'], v['c']):.4f} |")

    print(f"\n**合并 {used} 套样本**")
    for tier in ("strict", "usable", "hedge"):
        d = pooled[tier]["d"]
        if not d:
            continue
        n = len(d)
        lo, hi = boot_ci(d, rng)
        print(f"- {tier}: n={n}，微调−基线 {sum(d) / n:+.1%}"
              f"（95% CI [{lo:+.1%}, {hi:+.1%}]），不一致对 {pooled[tier]['b']}/{pooled[tier]['c']}，"
              f"McNemar p={mcnemar(pooled[tier]['b'], pooled[tier]['c']):.2e}")

    print("\n按厂商拆「可用率」（合并全部样本；看增益是否集中在某一家）：")
    for vendor in ("huawei", "h3c", "ruijie", "tplink"):
        d = []
        for bf, tf in pairs:
            p1, p2 = os.path.join(a.train_dir, bf), os.path.join(a.train_dir, tf)
            if not (os.path.exists(p1) and os.path.exists(p2)):
                continue
            base = {r["id"]: r for r in load(p1) if r["group"] == "base+rag"}
            tuned = {r["id"]: r for r in load(p2) if r["group"] == "tuned+rag"}
            for i in base:
                if i in tuned and i.startswith(vendor):
                    d.append(int(flags(tuned[i])["usable"]) - int(flags(base[i])["usable"]))
        if d:
            lo, hi = boot_ci(d, rng)
            print(f"  {vendor:8s} n={len(d):3d} 差 {sum(d) / len(d):+.0%}  95% CI [{lo:+.0%}, {hi:+.0%}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
