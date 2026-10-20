#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hdx → 技术资料层：**一步到位**（生成记录 → 合并 → 构建 index）。

为什么单独写这个脚本
====================
`hdx_to_tech.py` 负责"hdx → 记录"，`build_tech_docs.py` 负责"记录 → tech/*.md"。
正常情况下两步分开跑就行。但本机对 `_vendor_cmd/` 下**已存在**的文件禁止改写
（PermissionError；新建文件正常），而 `commands.jsonl` 已存在 —— 所以没法往里追加。

本脚本的做法：
    1. 用 `hdx_to_tech.process_one()` 生成全部 hdx 记录（复用已测过的解析逻辑）
    2. 把「原 commands.jsonl + hdx 记录」写进一个**全新的**数据根目录
       （`_vendor_cmd_hdx/`，需要时自动换 `_2`、`_3`…，保证永远是新建文件）
    3. 顺手把 hdx 记录也落到 `_vendor_cmd/huawei/commands_hdx.jsonl`（新文件，能写）
       —— 放在规范位置，将来人工重建时一眼能看到这批数据在哪
    4. 以新数据根目录跑 `build_tech_docs.build_vendor('huawei')`，产出
       `knowledge/huawei/tech/`（这一步只读数据、只写 knowledge/，不受限制）

用法:
    python tools/hdx_records_build.py
    python tools/hdx_records_build.py --dir <hdx目录> --no-build   # 只生成记录
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import shutil
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.join(PROJ, "tools"))

import hdx_to_tech as H          # noqa: E402
import build_tech_docs as B      # noqa: E402

DEFAULT_DIR = r"D:\ICSLite_enterprise-zh-25.2501.3.3\libraries"
HOME = os.path.expanduser("~")


def free_dir(base: str) -> str:
    """找一个尚不存在的目录名（既有目录里的文件改不了，只能换新的）"""
    if not os.path.exists(base):
        return base
    n = 1
    while True:
        n += 1
        cand = "%s_%d" % (base, n)
        if not os.path.exists(cand):
            return cand


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--cmd-root-base",
                    default=os.path.join(HOME, "deepseek_harness", "_vendor_cmd_hdx"))
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, "*.hdx")))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("没找到 .hdx")
        return 2

    print("=" * 96)
    print("第 1 步  生成记录：%d 个 hdx（html 已抽过，这里只解析不落盘）" % len(files))
    print("=" * 96)
    t0 = time.time()
    all_recs = []
    n_cmd = n_prose = 0
    for i, f in enumerate(files, 1):
        r = H.process_one(f, dry_run=True)
        if not r.get("ok"):
            print("[%2d/%d] ❌ %s" % (i, len(files), r.get("why")))
            continue
        all_recs.extend(r["records"])
        n_cmd += r["n_cliref"]
        n_prose += r["n_prose"]
        print("[%2d/%d] %-44s 命令 %-6d 正文 %-6d"
              % (i, len(files), r["doc_name"][:44], r["n_cliref"], r["n_prose"]))
    print("-" * 96)
    print("合计：记录 %d 条（命令 %d / 正文 %d），用时 %.1f 分钟"
          % (len(all_recs), n_cmd, n_prose, (time.time() - t0) / 60))

    # ---------------------------------------------------------------- 落数据
    base_cmd = os.path.join(H.CMD_ROOT, "huawei", "commands.jsonl")
    n_base = 0
    if os.path.exists(base_cmd):
        with io.open(base_cmd, encoding="utf-8", errors="replace") as fh:
            n_base = sum(1 for ln in fh if ln.strip())

    cmd_root = free_dir(args.cmd_root_base)
    out_dir = os.path.join(cmd_root, "huawei")
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, "commands.jsonl")
    with io.open(target, "w", encoding="utf-8") as w:
        if os.path.exists(base_cmd):
            with io.open(base_cmd, encoding="utf-8", errors="replace") as r:
                shutil.copyfileobj(r, w)
        for rec in all_recs:
            w.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("\n新数据根目录：%s" % cmd_root)
    print("  commands.jsonl = 原 %d 条 + hdx %d 条 = %d 条"
          % (n_base, len(all_recs), n_base + len(all_recs)))

    # 规范位置也留一份 hdx 记录（新文件，可写）
    canon_dir = os.path.join(H.CMD_ROOT, "huawei")
    canon = os.path.join(canon_dir, "commands_hdx.jsonl")
    n = 1
    while os.path.exists(canon):
        n += 1
        canon = os.path.join(canon_dir, "commands_hdx_%d.jsonl" % n)
    try:
        with io.open(canon, "w", encoding="utf-8") as w:
            for rec in all_recs:
                w.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("  hdx 记录另存：%s" % canon)
    except OSError as e:
        print("  ⚠ hdx 记录另存失败（忽略）：%s" % e)

    if args.no_build:
        print("\n--no-build：跳过构建")
        return 0

    # ---------------------------------------------------------------- 构建
    print("\n" + "=" * 96)
    print("第 2 步  构建技术资料层（build_tech_docs --vendor huawei）")
    print("=" * 96)
    B.CMD_ROOT = cmd_root
    os.environ["AIT_VENDOR_CMD_ROOT"] = cmd_root
    t1 = time.time()
    info = B.build_vendor("huawei")
    print("构建完成，用时 %.1f 分钟：%s"
          % ((time.time() - t1) / 60, json.dumps(
              {k: info.get(k) for k in
               ("vendor", "n_features", "n_commands", "truncated_features")
               if k in info}, ensure_ascii=False)[:300]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
