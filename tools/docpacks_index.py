#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按「产品文档包」汇总 _vendor_docs/ 下的原始文档，产出可核对的总表。

为什么要它
==========
用户给出的需求是「产品文档按**文档包 + 产品版本 + 文档版本**组织，产出命令与技术解释」
（对照华为文档包列表：文档包名称 / 产品版本 / 文档版本 / 日期 / 索引进度）。

现状：这些字段散落在三处，且**没有一处统一**
    _vendor_docs/<包名>/_fetch_manifest.json   抓取记录（url/字节/sha256）
    _vendor_docs/<包名>/toc.txt                章节 → 文件
    D:\\ait_llm\\index\\<厂商>_*.{jsonl,f16}      索引三件套（决定"索引进度"）

本工具**只读、只汇总**，不猜、不改任何原始文档：
    - 产品版本 / 文档版本：**只从已有的包名或书（章）名里提取**；
      提取不到就留空字符串，并在 notes 里写明"未提供"——绝不编一个版本号。
    - 索引进度：按"该厂商的索引三件套是否齐 + 该包的 md 是否已入库"给百分比，
      分母是**本包**的章数，分子是**能在 knowledge/ 里找到对应 md** 的章数。

用法
====
    python tools/docpacks_index.py                 # 打印总表
    python tools/docpacks_index.py --json PATH     # 落盘 JSON（供界面/后续步骤读）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

VENDOR_DOCS = os.path.expanduser("~/deepseek_harness/_vendor_docs")
VENDOR_CMD = os.path.expanduser("~/deepseek_harness/_vendor_cmd")
INDEX_DIR = os.environ.get("AIT_INDEX_DIR") or r"D:\ait_llm\index"
KNOWLEDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge")

#: 包名前缀 → 厂商。_vendor_docs 下的目录名形如 `h3c_cr16000-f_r795x` / `cisco_iosxe`。
VENDOR_PREFIXES = ("h3c", "huawei", "ruijie", "tplink", "cisco", "juniper")

#: 从字符串里提取「版本」的保守模式。只认显式版本号，避免把型号当版本。
VERSION_PATTERNS = [
    re.compile(r"\b(V\d{3}R\d{3}C\d{2}(?:SPC\d+)?)\b"),      # 华为 V600R024C00
    re.compile(r"\b(R\d{4,5}P\d{2,3})\b"),                    # H3C R795x 系列
    re.compile(r"\b(\d{2}\.\d{1,2}\.\d{1,2}[a-z]?)\b"),        # Cisco 26.x.x / 17.12.1
    re.compile(r"\b([Rr]\d{3,4}x)\b"),                        # h3c 目录里的 r795x
]


def vendor_of(pack: str) -> str:
    low = pack.lower()
    for v in VENDOR_PREFIXES:
        if low.startswith(v):
            return v
    return low.split("_")[0]


def extract_versions(*texts: str) -> list[str]:
    out: list[str] = []
    for t in texts:
        for pat in VERSION_PATTERNS:
            for m in pat.findall(t or ""):
                if m not in out:
                    out.append(m)
    return out


def pack_info(pack_dir: str) -> dict:
    pack = os.path.basename(pack_dir)
    vendor = vendor_of(pack)

    chapters: list[str] = []
    for name in sorted(os.listdir(pack_dir)):
        sub = os.path.join(pack_dir, name)
        if os.path.isdir(sub) and os.path.isdir(os.path.join(sub, "html")):
            chapters.append(name)

    # ⚠ 不能用 glob：文档名里含 `[810]` 这类方括号会被 glob 当**字符集**，
    # 返回一个并不存在的路径（实测 h3c_msr-xs_r6749 直接 FileNotFoundError）。
    # 契约是 `<章>/html/<节>.htm`，所以按章扫 html/ 目录即可，又快又准。
    htm: list[str] = []
    for chap in chapters:
        html_dir = os.path.join(pack_dir, chap, "html")
        try:
            with os.scandir(html_dir) as it:
                for e in it:
                    if e.is_file() and e.name.lower().endswith(".htm"):
                        htm.append(e.path)
        except OSError:
            continue
    total_bytes = sum(os.path.getsize(p) for p in htm)

    manifest = os.path.join(pack_dir, "_fetch_manifest.json")
    manifest_entries = 0
    if os.path.exists(manifest):
        try:
            with open(manifest, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for key in ("entries", "pages", "records", "items"):
                    if isinstance(data.get(key), list):
                        manifest_entries = len(data[key])
                        break
                if not manifest_entries:
                    manifest_entries = len([k for k in data if k not in ("vendor", "backend")])
        except (OSError, ValueError):
            manifest_entries = -1

    versions = extract_versions(pack, " ".join(chapters))

    # 索引进度：本包的章 → knowledge/<vendor>/tech/ 里是否有对应 md
    tech_dir = os.path.join(KNOWLEDGE, vendor, "tech")
    tech_names = []
    if os.path.isdir(tech_dir):
        tech_names = [n.lower() for n in os.listdir(tech_dir) if n.endswith(".md")]

    def chapter_landed(ch: str) -> bool:
        key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", ch.lower())[:18]
        return bool(key) and any(key[:12] in n.replace("-", "") for n in tech_names)

    landed = [c for c in chapters if chapter_landed(c)]
    progress = 0.0 if not chapters else round(100.0 * len(landed) / len(chapters))

    trio = all(
        os.path.exists(os.path.join(INDEX_DIR, f"{vendor}_{suffix}"))
        for suffix in ("chunks.jsonl", "emb.f16", "emb.meta.json")
    )

    return {
        "文档包名称": pack,
        "厂商": vendor,
        "产品版本": versions[0] if versions else "",
        "文档版本": versions[1] if len(versions) > 1 else "",
        "章数": len(chapters),
        "页数": len(htm),
        "字节": total_bytes,
        "抓取记录条数": manifest_entries,
        "索引进度": f"{progress:.0f}%",
        "索引三件套齐": trio,
        "notes": "" if versions else "包名/书名里未显式给出产品版本，按规范留空",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="把总表落盘成 JSON")
    a = ap.parse_args()

    if not os.path.isdir(VENDOR_DOCS):
        print(f"!! 目录不存在: {VENDOR_DOCS}")
        return 2

    rows = [pack_info(os.path.join(VENDOR_DOCS, d))
            for d in sorted(os.listdir(VENDOR_DOCS))
            if os.path.isdir(os.path.join(VENDOR_DOCS, d))]

    head = ("文档包名称", "厂商", "产品版本", "文档版本", "章数", "页数", "索引进度", "索引三件套齐")
    print("  ".join(f"{h:<12}" if i < 4 else f"{h:<8}" for i, h in enumerate(head)))
    print("-" * 110)
    for r in rows:
        print("  ".join([
            f"{r['文档包名称'][:30]:<32}", f"{r['厂商']:<8}",
            f"{(r['产品版本'] or '-'):<12}", f"{(r['文档版本'] or '-'):<10}",
            f"{r['章数']:<6}", f"{r['页数']:<6}", f"{r['索引进度']:<8}",
            "✓" if r["索引三件套齐"] else "✗",
        ]))
    print(f"\n合计 {len(rows)} 个文档包；无版本字段的 {sum(1 for r in rows if not r['产品版本'])} 个（已在 notes 说明，未编造）")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"packs": rows}, fh, ensure_ascii=False, indent=2)
        print(f"已落盘 → {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
