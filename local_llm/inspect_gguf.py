#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检视 GGUF 文件（元数据键值 / 张量名与形状）—— 用于**对照官方文件**写自己的转换器。

为什么要它：本机拿不到 `convert_hf_to_gguf.py`（GitHub raw / codeload 都被重置，
hf-mirror 上的 `ggml-org/llama.cpp` 仓 404），而 PyPI 上的同名包 `convert-hf-to-gguf`
仓库是随机用户名、homepage 却挂着 ggml.ai —— 来源不可信，不装。
于是自己写转换器，并**以本地官方 `Qwen3-4B-Q4_K_M.gguf` 为唯一参照**核对元数据与张量名：
    python local_llm/inspect_gguf.py D:\\ait_llm\\models\\Qwen3-4B-Q4_K_M.gguf --keys
    python local_llm/inspect_gguf.py <文件> --tensors
    python local_llm/inspect_gguf.py <文件> --json out.json

⚠ 默认**不打印数组内容**（`tokenizer.ggml.tokens` 有 15 万条，一行就能把上下文吃光）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def dump(path: str, show_tensors: bool, show_keys: bool, show_arrays: bool) -> dict:
    from gguf import GGUFReader
    r = GGUFReader(path)
    fields = getattr(r, "fields", {}) or {}
    out = {"path": path, "version": getattr(r, "gguf_version", None),
           "arch": None, "n_fields": len(fields), "fields": {}, "tensors": []}
    for k, f in fields.items():
        v = f.contents()
        if k in ("general.architecture",):
            out["arch"] = v
        item = {"type": str(getattr(f, "types", None) or getattr(f, "type", "")), "value": None}
        if isinstance(v, (bytes, bytearray)):
            item["value"] = f"<bytes len={len(v)}>"
        elif hasattr(v, "__len__") and not isinstance(v, str):
            arr = list(v)
            item["len"] = len(arr)
            if show_arrays or len(arr) <= 8:
                item["value"] = arr[:16]
            else:
                item["value"] = f"<array len={len(arr)}>"
        else:
            item["value"] = v
        out["fields"][k] = item
    if show_tensors:
        out["tensors"] = [{"name": t.name, "shape": list(t.shape), "type": str(t.tensor_type)}
                          for t in r.tensors]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--keys", action="store_true", help="打印全部元数据键值")
    ap.add_argument("--tensors", action="store_true", help="打印张量名/形状/类型")
    ap.add_argument("--arrays", action="store_true", help="连数组内容也打印（很大，慎用）")
    ap.add_argument("--json", default="", help="另存完整 JSON 到文件")
    a = ap.parse_args()
    d = dump(a.path, a.tensors, a.keys, a.arrays)
    print(f"{a.path}\n  GGUF version={d['version']}  arch={d['arch']}  "
          f"元数据 {d['n_fields']} 项  张量 {len(d['tensors'])} 个")
    if a.keys:
        for k, v in d["fields"].items():
            val = json.dumps(v.get("value"), ensure_ascii=False)
            print(f"  {k:<48} {str(v['type'])[:28]:<30} {val[:150]}")
    if a.tensors:
        for t in d["tensors"]:
            print(f"  {t['name']:<40} {str(t['shape']):<22} {t['type']}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1, default=str)
        print(f"  → {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
