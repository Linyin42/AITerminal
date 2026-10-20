# -*- coding: utf-8 -*-
"""下载本地推理用的 GGUF 模型到 D 盘（hf-mirror 直连 + 断点续传）。

为什么不用 huggingface_hub.hf_hub_download：
    实测 hf-mirror 的 `/resolve/` 响应缺少 ETag/Location，huggingface_hub 会抛
        FileMetadataError: Distant resource does not seem to be on huggingface.co
    但同一个 URL 用 HTTP **HEAD 取 Content-Length 是好的**（本文里的体积就是这么核实的）。
    ⇒ 自己实现：HEAD 取大小 + Range 断点续传 + 双通道回退。

分档依据（交付机独显 2-6GB + 内存 6-14GB，2026-10-16 用户确认）：
    显存 >=5GB  -> Qwen3-8B-Q4_K_M (4.68GB)
    显存 3-4GB  -> Qwen3-4B-Q4_K_M (2.33GB)   ← 交付主力档
    显存 ~2GB   -> Qwen3-1.7B-Q8_0  (1.71GB)  ← 该仓库只有 Q8_0
    嵌入模型     -> bge-m3-Q8_0 (0.59GB, 1024 维)

用法:
    python download_models.py core     # 4B + bge-m3（先跑通端到端）
    python download_models.py all      # 再加 8B + 1.7B
"""
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402  （同目录；路径的唯一来源见 paths.py）

DEST = paths.MODELS
MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]
UA = {"User-Agent": "ait-dl/1.0"}
MAX_RETRY = 8          # 实测 hf-mirror 会在中途 ConnectionReset(WinError 10054)，必须能续

MANIFEST = {
    "core": [
        ("Qwen/Qwen3-4B-GGUF", "Qwen3-4B-Q4_K_M.gguf", 2.33),
        ("gpustack/bge-m3-GGUF", "bge-m3-Q8_0.gguf", 0.59),
    ],
    "all": [
        ("Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", 4.68),
        ("Qwen/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q8_0.gguf", 1.71),
    ],
}


def log(msg):
    print(msg, flush=True)


def head_size(url, timeout=30):
    req = urllib.request.Request(url, method="HEAD", headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return int(r.headers.get("Content-Length") or 0)


def download(repo, fname, expect_gb, dest_dir=None):
    """下到 `dest_dir`（默认 `D:\\ait_llm\\models`）。

    加 `dest_dir` 是为了**复用**这套"HEAD 取体积 + Range 续传 + 双通道回退"去下
    HF 原始权重（微调要的不是 GGUF）——见 `download_hf_base.py`。默认值保证原行为不变。
    """
    dest = os.path.join(dest_dir or DEST, fname)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    expect = 0
    for base in MIRRORS:
        try:
            expect = head_size(f"{base}/{repo}/resolve/main/{fname}")
            if expect:
                log(f"   通道 {base} 可用，{expect / 1024**3:.2f} GB")
                break
        except Exception as e:  # noqa: BLE001
            log(f"   通道 {base} 不可用（{type(e).__name__}）")
    else:
        raise SystemExit(f"!! 所有通道都取不到体积: {repo}/{fname}")

    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    if have == expect:
        log(f"[跳过] {fname} 已完整 {have / 1024**3:.2f} GB")
        return dest
    if have > expect:
        log(f"[重下] {fname} 本地比远端大（{have} > {expect}）")
        have = 0

    url = f"{MIRRORS[0]}/{repo}/resolve/main/{fname}"
    for attempt in range(1, MAX_RETRY + 1):
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        if have == expect:
            break
        if have > expect:
            log(f"   本地比远端大（{have} > {expect}）→ 删除重下")
            os.remove(dest)
            have = 0
        headers = dict(UA)
        if have:
            headers["Range"] = f"bytes={have}-"
            log(f"[续传 {attempt}/{MAX_RETRY}] {fname} 从 {have / 1024**2:.0f} MB 继续")
        else:
            log(f"[下载 {attempt}/{MAX_RETRY}] {fname}  {expect / 1024**3:.2f} GB")
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:   # 服务端不认 Range -> 从头下
                    log(f"   ! 服务端未支持续传（HTTP {r.status}），从头开始")
                    have = 0
                mode = "ab" if have else "wb"
                got = have
                last_pct = -1
                with open(dest, mode) as fh:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        pct = got * 100 // max(expect, 1)
                        if pct >= last_pct + 10:
                            last_pct = pct
                            log(f"      {pct}%  {got / 1024**2:.0f}/{expect / 1024**2:.0f} MB")
        except Exception as e:  # noqa: BLE001
            log(f"   ! 第 {attempt} 次中断：{type(e).__name__}: {e}")
            time.sleep(4)
            continue

    got = os.path.getsize(dest) if os.path.exists(dest) else 0
    if got != expect:
        raise SystemExit(f"!! 重试 {MAX_RETRY} 次仍不完整：{got}/{expect} 字节（直接重跑可续传）")
    log(f"[完成] {dest}  {got / 1024**3:.2f} GB")
    return dest


def main():
    tier = (sys.argv[1] if len(sys.argv) > 1 else "core").strip()
    items = MANIFEST.get(tier)
    if not items:
        raise SystemExit(f"未知档位 {tier!r}，可选: {list(MANIFEST)}")
    os.makedirs(DEST, exist_ok=True)
    for repo, fname, expect_gb in items:
        download(repo, fname, expect_gb)

    log("\n=== D:\\ait_llm\\models ===")
    for f in sorted(os.listdir(DEST)):
        p = os.path.join(DEST, f)
        if os.path.isfile(p):
            log(f"  {f}  {os.path.getsize(p) / 1024**3:.2f} GB")


if __name__ == "__main__":
    main()
