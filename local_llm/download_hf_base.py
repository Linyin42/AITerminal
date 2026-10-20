#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 **HF 原始权重**（微调用）—— curl 通道 + ModelScope 兜底 + Range 续传。

为什么不用 huggingface_hub / 也不用 urllib
========================================
1) `snapshot_download('Qwen/Qwen3-4B')` 走 HF 的 **Xet CDN** 并失败：
       RuntimeError: Task error: File reconstruction error: CAS Client Error:
       error sending request for url (https://us.aws.cdn.hf.co/xorbs/…)
   （`HF_HUB_DISABLE_XET=1` 对该调用不生效，故不复用 hub。）
2) 本仓库自带的 `download_models.py`（urllib）也**不再可用**——实测本机 python-urllib
   打开 hf-mirror 的 `/resolve/` 会直接：
       URLError <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol>
   而**同一个 URL 用 curl.exe 跟重定向是好的**（HTTP 206 + Range 都正常）：
       hf-mirror → 302 → https://cas-bridge.xethub.hf.co/…  ✔ 可达
   所以这里显式走 curl（`-L` 跟跳转、`-C -` 断点续传），并把 ModelScope 作为第二通道：
       https://modelscope.cn/api/v1/models/Qwen/Qwen3-4B/repo?Revision=master&FilePath=<名>
       （同样是 302 → cdn-lfs-cn-1.modelscope.cn，206 正常）

用法：
    python local_llm/download_hf_base.py                 # 默认 Qwen/Qwen3-4B
    python local_llm/download_hf_base.py --repo Qwen/Qwen3-1.7B --dest D:\\ait_llm\\models_hf\\Qwen3-1.7B
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

MIRROR = "https://hf-mirror.com"
MS_API = "https://modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={f}"
SMALL_FILES = ["config.json", "generation_config.json", "tokenizer.json",
               "tokenizer_config.json", "vocab.json", "merges.txt",
               "model.safetensors.index.json"]
CURL = "curl.exe"


def run_curl(args: list[str], timeout=None):
    return subprocess.run([CURL, "-sS", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def remote_sizes(repo: str) -> dict[str, int]:
    """从镜像 API 取每个文件的字节数（?blobs=true 才带 size）。"""
    r = run_curl([f"{MIRROR}/api/models/{repo}?blobs=true"])
    try:
        obj = json.loads(r.stdout)
    except Exception:                                          # noqa: BLE001
        return {}
    return {s.get("rfilename"): int(s.get("size") or 0)
            for s in (obj.get("siblings") or []) if s.get("rfilename")}


def fetch(url: str, dest: str, expect: int, tries: int = 6) -> bool:
    for i in range(1, tries + 1):
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        if expect and have == expect:
            print(f"   [完成] {os.path.basename(dest)} {have/2**30:.2f} GB")
            return True
        if expect and have > expect:
            print(f"   ! 本地比远端大（{have}>{expect}）→ 重下")
            os.remove(dest)
            have = 0
        print(f"   [下 {i}/{tries}] {os.path.basename(dest)} 已有 {have/2**20:.0f} MB"
              f"{'/' + format(expect/2**20, '.0f') + ' MB' if expect else ''}")
        args = ["-L", "-C", "-", "--retry", "5", "--retry-delay", "3",
                "--retry-all-errors", "--connect-timeout", "30",
                "--max-time", "3600", "-o", dest, url]
        p = subprocess.run([CURL, *args])
        if p.returncode != 0:
            print(f"   ! curl 退出码 {p.returncode}（-C - 在服务端不支持续传时为 33）")
            if p.returncode == 33 and os.path.exists(dest):
                os.remove(dest)
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    return bool(expect) and have == expect


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="Qwen/Qwen3-4B")
    ap.add_argument("--dest", default=r"D:\ait_llm\models_hf\Qwen3-4B")
    ap.add_argument("--source", default="ms", choices=["ms", "hf"],
                    help="优先通道。实测（本机 2026-10-18）：ModelScope 33.7MB/s，"
                         "hf-mirror→Xet 桥仅 4.4MB/s 且易断 ⇒ 默认 ms")
    a = ap.parse_args()
    os.makedirs(a.dest, exist_ok=True)

    sizes = remote_sizes(a.repo)
    print(f"镜像可见 {len(sizes)} 个文件；拉取小文件 + 全部分片")
    idx_local = os.path.join(a.dest, "model.safetensors.index.json")

    def need(name: str) -> bool:
        """是否需要下载。

        ⚠ 不能只看"文件非空"：上一轮被中断的分片（实测 shard1 只有 1.23GB/3.69GB）
          会被当成已完成而跳过 —— 这就是"下完了但少一半"的由来。已知远端大小时必须精确相等。
        """
        p = os.path.join(a.dest, name)
        if not (os.path.exists(p) and os.path.getsize(p) > 0):
            return True
        exp = sizes.get(name, 0)
        return bool(exp) and os.path.getsize(p) != exp

    # 先保证 index.json 在手（分片名从它读）
    if need("model.safetensors.index.json"):
        fetch(f"{MIRROR}/{a.repo}/resolve/main/model.safetensors.index.json",
              idx_local, sizes.get("model.safetensors.index.json", 0))
    if not os.path.exists(idx_local):
        print("!! 取不到 model.safetensors.index.json")
        return 2
    with open(idx_local, "r", encoding="utf-8") as fh:
        shards = sorted({v for v in (json.load(fh).get("weight_map") or {}).values()})
    print(f"分片：{shards}")

    failed = []
    ms_url = lambda n: MS_API.format(repo=a.repo, f=n)          # noqa: E731
    hf_url = lambda n: f"{MIRROR}/{a.repo}/resolve/main/{n}"     # noqa: E731
    channels = [(ms_url, "ModelScope"), (hf_url, "hf-mirror→Xet")]
    if a.source == "hf":
        channels.reverse()
    for name in SMALL_FILES + shards:
        if not need(name):
            print(f"[跳过] {name} 已存在 {os.path.getsize(os.path.join(a.dest, name))/2**20:.1f} MB")
            continue
        expect = sizes.get(name, 0)
        # 小文件的体积可能取不到（API 未给 size）⇒ 用参照 GGUF/索引对不上时以远端为准
        dest = os.path.join(a.dest, name)
        ok = False
        for urlf, label in channels:
            print(f"   通道 {label} 下载 {name}")
            if fetch(urlf(name), dest, expect):
                ok = True
                break
        if not ok:
            failed.append(name)

    print("\n=== 结果 ===")
    total = 0
    for f in sorted(os.listdir(a.dest)):
        p = os.path.join(a.dest, f)
        if os.path.isfile(p):
            total += os.path.getsize(p)
            print(f"  {f:<42} {os.path.getsize(p)/2**30:6.2f} GB")
    print(f"  合计 {total/2**30:.2f} GB ⇒ {a.dest}")
    if failed:
        print(f"!! 未完成：{failed}（重跑本脚本可续传）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
