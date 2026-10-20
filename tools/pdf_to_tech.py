#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把厂商的整本 PDF 产品文档转成 `knowledge/<厂商>/tech/*.md`（标准卡片格式）。

为什么走 PDF
============
2026-10 实测：Juniper 文档站迁到 networkingdocs.hpe.com 后，
- 正文页是无头渲染拿不到内容的 SPA 空壳（`<div id="content"><!----></div>`）；
- 但**每本书都提供整本 PDF**（`<书目录>/<书目录>.pdf`），
  一本 CLI Reference = 95 MB / 一个文件，比"11,629 页逐页渲染"省 3 个数量级。

必须输出什么格式（这条踩过大坑）
============================
`local_llm/build_index.py:87-115 iter_sections()` 规定：
- 按 **`##` / `###`** 标题切段；**第一个标题之前的内容整体丢弃**；
- 每个小节里**第一条** `<!-- source: ... -->` 作为该片段的"原始出处"。

所以：
✗ 只写一个 `# 大标题` + 大段正文 ⇒ 切块器把**整份文件丢掉**
  （实测：35.55 MB 正文只切出 451 KB，而且那 451 KB 全来自另外两个合规文件）；
✓ 每本书按 level-2 书签分文件、文件内按更深的书签切成 `### 小节`
  + 紧跟 `<!-- source: <书>.pdf 第 N 页 -->` ⇒ 每个片段天然带出处。

用法
====
    python tools/pdf_to_tech.py --pdf <文件.pdf> --vendor juniper
    python tools/pdf_to_tech.py --pdf <文件.pdf> --vendor juniper --dry-run
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import fitz  # PyMuPDF


def safe_name(title: str, idx: int) -> str:
    """文件名：去非法字符、限长，保留中文。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:70] or f"chapter-{idx:03d}")


def clean(title: str) -> str:
    """小节标题：压掉换行/多余空格，去掉会破坏 markdown 的字符。"""
    return re.sub(r"\s+", " ", (title or "").replace("#", "")).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--vendor", required=True)
    ap.add_argument("--tech-dir", default="", help="默认 <仓库>/knowledge/<vendor>/tech")
    ap.add_argument("--file-level", type=int, default=2, help="按第几层书签分**文件**（默认 2）")
    ap.add_argument("--card-level", type=int, default=3, help="按第几层书签分**小节**（默认 3）")
    ap.add_argument("--min-chars", type=int, default=40, help="小节少于这么多字丢弃（同 MIN_CHARS）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tech_dir = a.tech_dir or os.path.join(repo, "knowledge", a.vendor, "tech")
    if not a.dry_run:
        os.makedirs(tech_dir, exist_ok=True)

    doc = fitz.open(a.pdf)
    n_pages = doc.page_count
    toc = doc.get_toc(simple=True) or []
    book = os.path.basename(a.pdf)

    def marks_at(level: int) -> list[tuple[str, int]]:
        """取指定层级的书签；该层太少时逐级放宽。"""
        for lv in (level, level + 1, level - 1, 1):
            ms = [(clean(t), p) for l, t, p in toc if l == lv]
            if len(ms) >= 2:
                return ms
        return []

    file_marks = marks_at(a.file_level)
    card_marks = marks_at(a.card_level)
    if not file_marks:
        print("!! 这份 PDF 没有可用书签，无法分章")
        return 2

    def text_of(p0: int, p1: int) -> str:
        return re.sub(r"\n{3,}", "\n\n",
                      "\n".join(doc.load_page(i).get_text("text")
                                for i in range(p0, min(p1, n_pages)))).strip()

    print(f"PDF: {a.pdf}")
    print(f"页数 {n_pages} / 书签 {len(toc)} 条 / 文件层 {len(file_marks)} 个 / 小节层 {len(card_marks)} 个")
    print(f"输出: {tech_dir}")
    print("-" * 70)

    n_files = n_cards = n_skipped = total_chars = 0
    for fi, (ftitle, fstart) in enumerate(file_marks):
        fend = file_marks[fi + 1][1] - 1 if fi + 1 < len(file_marks) else n_pages
        # 本文件范围内的小节书签
        inner = [(t, p) for t, p in card_marks if fstart <= p <= fend]
        parts = [f"# {ftitle}\n"]
        if not inner:
            inner = [(ftitle, fstart)]
        for ci, (ctitle, cstart) in enumerate(inner):
            cend = inner[ci + 1][1] - 1 if ci + 1 < len(inner) else fend
            body = text_of(cstart - 1, cend)
            if len(body) < a.min_chars:
                n_skipped += 1
                continue
            # ⚠ 出处行必须紧跟标题（切块器只认"标题后第一条"）
            parts.append(f"\n### {ctitle}\n<!-- source: {book} 第 {cstart}-{cend} 页 -->\n\n{body}\n")
            n_cards += 1
            total_chars += len(body)

        content = "".join(parts)
        if n_cards == 0:
            continue
        path = os.path.join(tech_dir, f"{safe_name(ftitle, fi)}.md")
        if not a.dry_run:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:   # UTF-8 无 BOM
                fh.write(content)
        n_files += 1
        print(f"  ✓ {safe_name(ftitle, fi)[:44]:<46} {fend - fstart + 1:>5} 页 / "
              f"{len(inner):>5} 小节 / {len(content):>9} 字")

    print("-" * 70)
    print(f"写出 {n_files} 个 md / {n_cards} 个小节 / 跳过 {n_skipped} 个（过短）/ 合计 {total_chars:,} 字")
    if a.dry_run:
        print("（dry-run，未落盘）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
