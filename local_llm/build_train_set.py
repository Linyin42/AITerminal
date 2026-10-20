#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从已切片的厂商文档语料构建**本地微调训练集**。

输入：`D:\\ait_llm\\index\\<vendor>_chunks.jsonl`
      41 万条，schema = `vendor / file / section / part / source / text / chars / id`
      （`text` 已带 `[vendor · section]` 前缀，见 `docpacks_index.py` / `embed_chunks.py`）

输出：`D:\\ait_llm\\train\\`
      cpt_corpus.txt    领域继续预训练语料（纯文本；**分层配额 + 去重 + 长度过滤**）
      sft_qa.jsonl      指令微调样本 `{vendor, question, answer, source}`
      manifest.json     计数 / 字符数 / token 估算 / 每厂商配额（复现与评测对齐用）

设计取舍（每一条都是"6GB 显存 + 几小时能出结果"倒推出来的）
=========================================================
1. **分层配额而非全量喂**：语料共 ~640MB（40 万条）。全量一轮在 3060 Laptop(6GB) 上
   按 QLoRA 要几十小时，且边际收益递减。先做 ~40MB（≈12M token）一轮：
   CPT 几小时，量化/评测当天就能看到数字，再决定要不要加量。
2. **单遍流式 + 蓄水池抽样**：文件按厂商拼接，前几 MB 不代表全体（同一命令在不同型号
   手册里反复出现）⇒ 不能"读到配额就停"。用固定种子的蓄水池抽样，一遍扫完、覆盖全文件、
   内存只占配额本身。
3. **去重**：`text` 的 sha1 前 16 位做键；同一命令跨型号重复率很高（不去重会过拟合重复片段）。
4. **长度过滤**：< 80 字符多为标题残片；> 6000 字符多是整页表格，超出训练 seq len 后被截断。
5. **SFT 问答是模板生成**的：不联网、不花钱、可复现。后续可选让本地模型改写得更自然
   （那一步要单独记录，别把"模板题"和"模型生成题"混在一个评测口径里）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

VENDOR_LABEL = {
    "huawei": "华为", "h3c": "H3C", "ruijie": "锐捷",
    "juniper": "Juniper", "cisco": "Cisco", "tplink": "TP-Link",
}
#: 每厂商 CPT 配额（MB）。总 ~40MB。6GB 卡一轮 QLoRA 的现实体量。
DEFAULT_QUOTA_MB = {
    "huawei": 12, "h3c": 10, "ruijie": 8, "juniper": 5, "cisco": 3, "tplink": 2,
}
#: SFT 每厂商条数（模板问答）
DEFAULT_SFT_N = {
    "huawei": 6000, "h3c": 5000, "ruijie": 5000, "juniper": 2500, "cisco": 1200, "tplink": 800,
}

CHARS_PER_TOKEN = 1.6           # 中英混排经验值（评测口径用同一系数即可比较）

CMD_RE = re.compile(
    r"\b(display|show|undo|interface|configure|config|set|reset|delete|create|import|export)"
    r"\s+([a-z][a-z0-9\-]{1,24}(?:\s+[a-z0-9\-\[\]\|/]{1,20}){0,3})"
)
#: 优先做"命令参考"类切片的 SFT（这类问题最像用户的真实提问）
CMD_TOPIC = re.compile(r"命令|command|cli|reference|参考|回显|显示", re.I)

TEMPLATES_CMD = [
    "{v} 设备上 `{c}` 这条命令的作用是什么？回显字段怎么解读？",
    "{v} 的 `{c}` 命令怎么用？请说明作用和输出含义。",
    "在 {v} 设备上执行 `{c}` 会看到什么？字段分别是什么意思？",
]
TEMPLATES_SEC = [
    "{v} 文档里「{s}」这一节的要点是什么？",
    "{v}：请说明「{s}」相关内容。",
    "{v} 设备上「{s}」是怎么配置或查看的？",
]


def norm_text(t: str) -> str:
    t = re.sub(r"[ \t\u3000]+", " ", t or "")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def key_of(vendor: str, text: str) -> str:
    return hashlib.sha1(f"{vendor}|{text}".encode("utf-8", "ignore")).hexdigest()[:16]


def strip_index_prefix(t: str) -> str:
    """剥掉切片自带的索引前缀 `[vendor · section · part]`（见 docpacks_index.py）。

    它是**索引用元数据不是文档内容**，留着会让模型学到"答案以方括号元数据开头"。
    判据：最外层方括号块内含 `·` 分隔符才是前缀；文档里合法的 `[ slot slot-number ]`
    不含 `·`，不能误剥。必须按**括号深度**匹配：section 里会嵌方括号
    （如 `H3C SecPath F100[F1000]系列防火墙`），正则字符类跨不过去（实测漏 1,178 条）。
    """
    if not t.startswith("["):
        return t
    depth = 0
    for i, ch in enumerate(t):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return t[i + 1:].lstrip() if "·" in t[: i + 1] else t
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index-dir", default=r"D:\ait_llm\index")
    ap.add_argument("--out-dir", default=r"D:\ait_llm\train")
    ap.add_argument("--seed", type=int, default=20261018)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    os.makedirs(a.out_dir, exist_ok=True)

    manifest = {"seed": a.seed, "chars_per_token": CHARS_PER_TOKEN, "vendors": {}}
    sft_rows = []

    # ⚠ 两个产物在下面的厂商循环里是 **append** 写的（为了把 6 个厂拼进同一个文件），
    #   所以必须在循环**之前**清空一次；否则重跑一次就把语料翻一倍。
    #   实测踩过：cpt_corpus.txt 累到 268.7 MB（175,233 段里 32% 是重复），而训练实际只用了
    #   43.79 MB —— 更糟的是 build_eval_set.py 拿这个文件当"训练语料"判定 common/longtail，
    #   判定基准因此比模型真见过的文本大了 6 倍。
    for name in ("cpt_corpus.txt", "sft_qa.jsonl"):
        p = os.path.join(a.out_dir, name)
        if os.path.exists(p):
            os.remove(p)

    for vendor, quota_mb in DEFAULT_QUOTA_MB.items():
        path = os.path.join(a.index_dir, f"{vendor}_chunks.jsonl")
        if not os.path.exists(path):
            print(f"!! 缺语料文件，跳过：{path}")
            continue
        quota_bytes = quota_mb * 1024 * 1024
        kept_bytes, seen = 0, set()
        cpt_cache: list[str] = []                 # 蓄水池（按字符数上限）
        cpt_chars = 0
        sft_pool: list[tuple[str, str, str]] = []  # (question, answer, source)
        sft_wanted = DEFAULT_SFT_N.get(vendor, 2000)
        sft_seen = 0
        total = skipped_len = skipped_dup = 0

        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                total += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:                          # noqa: BLE001
                    continue
                text = strip_index_prefix(norm_text(rec.get("text") or ""))
                n = len(text)
                if n < 80 or n > 6000:
                    skipped_len += 1
                    continue
                k = key_of(vendor, text)
                if k in seen:
                    skipped_dup += 1
                    continue
                seen.add(k)

                # ---- CPT 蓄水池：按字符数配额，随机替换 ----
                if cpt_chars + n <= quota_bytes:
                    cpt_cache.append(text)
                    cpt_chars += n
                elif cpt_cache:
                    j = rng.randrange(len(cpt_cache) + 1)
                    if j < len(cpt_cache):
                        cpt_chars -= len(cpt_cache[j]) - n
                        cpt_cache[j] = text

                # ---- SFT 蓄水池 ----
                section = (rec.get("section") or "").strip()
                source = rec.get("source") or rec.get("file") or ""
                label = VENDOR_LABEL.get(vendor, vendor)
                m = CMD_RE.search(text)
                if m and 150 <= n <= 2500 and (CMD_TOPIC.search(section) or CMD_TOPIC.search(text[:200])):
                    cmd = f"{m.group(1)} {m.group(2)}"
                    q = rng.choice(TEMPLATES_CMD).format(v=label, c=cmd)
                elif section and n <= 2500:
                    q = rng.choice(TEMPLATES_SEC).format(v=label, s=section[:40])
                else:
                    continue
                row = (q, text, source)
                if sft_seen < sft_wanted:
                    sft_pool.append(row)
                else:
                    j = rng.randrange(sft_seen + 1)
                    if j < sft_wanted:
                        sft_pool[j] = row
                sft_seen += 1

        cpt_chars = sum(len(t) for t in cpt_cache)
        with open(os.path.join(a.out_dir, "cpt_corpus.txt"), "a", encoding="utf-8") as fh:
            for t in cpt_cache:
                fh.write(t + "\n\n")
        sft_rows.extend({"vendor": vendor, "question": q, "answer": ans, "source": src}
                        for q, ans, src in sft_pool)

        manifest["vendors"][vendor] = {
            "label": VENDOR_LABEL.get(vendor, vendor),
            "scanned": total, "kept": len(seen),
            "skipped_short_or_long": skipped_len, "skipped_dup": skipped_dup,
            "cpt_chunks": len(cpt_cache), "cpt_chars": cpt_chars,
            "cpt_mb": round(cpt_chars / 1048576, 2),
            "sft_pool": len(sft_pool), "sft_candidates": sft_seen,
        }
        print(f"[{vendor:<7}] 扫 {total:>7} 条 → 去重后 {len(seen):>7} 条 | "
              f"CPT {len(cpt_cache):>6} 段 / {cpt_chars/1048576:6.2f} MB | SFT {len(sft_pool):>5} 条")

    with open(os.path.join(a.out_dir, "sft_qa.jsonl"), "w", encoding="utf-8") as fh:
        for r in sft_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    tot_chars = sum(v["cpt_chars"] for v in manifest["vendors"].values())
    manifest["total"] = {
        "cpt_chars": tot_chars, "cpt_mb": round(tot_chars / 1048576, 2),
        "cpt_tokens_est": int(tot_chars / CHARS_PER_TOKEN),
        "sft_rows": len(sft_rows),
        "sft_answer_chars": sum(len(r["answer"]) for r in sft_rows),
    }
    with open(os.path.join(a.out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    print("-" * 70)
    print(f"CPT 语料 {manifest['total']['cpt_mb']} MB ≈ {manifest['total']['cpt_tokens_est']:,} token")
    print(f"SFT 样本 {len(sft_rows):,} 条（答案合计 {manifest['total']['sft_answer_chars']/1048576:.2f} MB）")
    print(f"落盘：{a.out_dir}\\cpt_corpus.txt / sft_qa.jsonl / manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
