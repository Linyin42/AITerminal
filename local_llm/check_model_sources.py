# -*- coding: utf-8 -*-
"""核实下载源可用性与文件名（只读探测，不下载）。

用途：在写死任何下载脚本之前，先确认 hf-mirror 上这些仓库真实存在、
以及里面到底有哪些 .gguf 文件、多大 —— 避免"猜仓库名"式失败。

用法: python check_model_sources.py
"""
import json
import urllib.error
import urllib.request

HF = "https://hf-mirror.com"

REPOS = [
    "Qwen/Qwen3-1.7B-GGUF",
    "Qwen/Qwen3-4B-GGUF",
    "Qwen/Qwen3-8B-GGUF",
    "Qwen/Qwen3-14B-GGUF",
    "gpustack/bge-m3-GGUF",
    "CompendiumLabs/bge-m3-gguf",
    "Qwen/Qwen3-Embedding-0.6B-GGUF",
]


def api(path: str, timeout: int = 20):
    req = urllib.request.Request(HF + path, headers={"User-Agent": "ait-probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


for repo in REPOS:
    try:
        info = api(f"/api/models/{repo}")
    except urllib.error.HTTPError as e:
        print(f"[{e.code}] {repo}  —— 仓库不可用")
        continue
    except Exception as e:
        print(f"[ERR] {repo}  —— {type(e).__name__}: {e}")
        continue
    sibs = info.get("siblings") or []
    ggufs = [s.get("rfilename") for s in sibs if str(s.get("rfilename", "")).lower().endswith(".gguf")]
    print(f"[OK] {repo}  gguf={len(ggufs)}")
    for name in sorted(ggufs):
        size = ""
        try:
            head = urllib.request.Request(
                f"{HF}/{repo}/resolve/main/{name}",
                method="HEAD", headers={"User-Agent": "ait-probe/1.0"})
            with urllib.request.urlopen(head, timeout=20) as r:
                n = int(r.headers.get("Content-Length") or 0)
                size = f"{n / 1024**3:.2f} GB" if n else "?"
        except Exception as e:  # noqa: BLE001
            size = f"(HEAD 失败 {type(e).__name__})"
        print(f"      {name}   {size}")
