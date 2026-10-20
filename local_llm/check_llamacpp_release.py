# -*- coding: utf-8 -*-
"""探测 llama.cpp 预编译包（GitHub release 资产名 + 体积）与本机 Vulkan 运行时。

为什么必须先探：llama.cpp 的资产名带版本号（llama-bXXXX-bin-win-...），
写死会过期；且交付机独显可能是 A 卡/Intel，需要确认 Vulkan 包是否可用。

用法: python check_llamacpp_release.py
"""
import json
import os
import urllib.error
import urllib.request

APIS = [
    "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest",
    "https://gh-proxy.com/https://api.github.com/repos/ggml-org/llama.cpp/releases/latest",
]

data = None
used = None
for url in APIS:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ait-probe/1.0", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        used = url
        break
    except Exception as e:  # noqa: BLE001
        print(f"[miss] {url} -> {type(e).__name__}: {e}")

if not data:
    print("!! 两个 GitHub API 通道都不可达 —— 需要换镜像或手工下载")
else:
    print(f"通道: {used}")
    print(f"版本: {data.get('tag_name')}   发布时间: {data.get('published_at')}")
    want = ("win", "cuda", "vulkan", "cpu", "avx2", "cudart", "hip", "sycl")
    print("--- Windows 相关资产 ---")
    for a in data.get("assets") or []:
        name = a.get("name", "")
        if "win" not in name.lower():
            continue
        if not any(w in name.lower() for w in want):
            continue
        print(f"  {name}   {a.get('size', 0) / 1024**2:.1f} MB")
        print(f"      {a.get('browser_download_url')}")

print()
print("--- 本机图形运行时 ---")
for dll in ("vulkan-1.dll", "nvml.dll", "cudart64_12.dll"):
    for root in (os.environ.get("SystemRoot", r"C:\Windows"),):
        p = os.path.join(root, "System32", dll)
        print(f"  {p} : {os.path.exists(p)}")
