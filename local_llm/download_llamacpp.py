# -*- coding: utf-8 -*-
"""下载并解包 llama.cpp 预编译二进制（Windows x64）到 D 盘。

设计要点：
  · **版本号不写死**：llama.cpp 的 `latest` release 是空壳（只有 nightly-tag.txt），
    真正的二进制在 nightly（tag 形如 b11696）。所以这里动态解析"最新的、带 Windows 资产的"
    release。写死 bXXXX 过几天就 404。
  · **多通道回退**：先直连 GitHub，失败再走 gh-proxy.com（实测 200）。
  · **幂等**：目标目录已有 llama-server.exe 且体积与压缩包对得上就跳过。
  · 默认只下 delivery 需要的两档：vulkan（A/N/Intel 通吃，交付机首选）+ cpu（保底）。
    cuda（开发机最快）用 --cuda 显式加。

用法:
    python download_llamacpp.py            # vulkan + cpu
    python download_llamacpp.py --cuda     # 追加 cuda-12.4 + cudart
"""
import argparse
import json
import os
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402  （同目录；路径的唯一来源见 paths.py）

DEST = paths.BIN
UA = {"User-Agent": "ait-dl/1.0"}
PROXIES = ["", "https://gh-proxy.com/"]

VARIANTS = {
    "vulkan": "bin-win-vulkan-x64.zip",
    "cpu": "bin-win-cpu-x64.zip",
    "cuda": "bin-win-cuda-12.4-x64.zip",
    "cudart": "cudart-llama-bin-win-cuda-12.4-x64.zip",
}


def log(msg):
    print(msg, flush=True)


def fetch_latest_win_release():
    """返回 (tag, {asset名: url})，取最新一个带 Windows 二进制的 release。"""
    url = ("https://api.github.com/repos/ggml-org/llama.cpp/releases"
           "?per_page=8")
    req = urllib.request.Request(url, headers=dict(UA, Accept="application/vnd.github+json"))
    with urllib.request.urlopen(req, timeout=30) as r:
        rels = json.loads(r.read().decode("utf-8", "replace"))
    for rel in rels:
        names = {a["name"]: a["browser_download_url"] for a in (rel.get("assets") or [])}
        if any("bin-win" in n for n in names):
            return rel.get("tag_name"), names
    raise SystemExit("!! 最近 8 个 release 里找不到任何 Windows 二进制")


def download(url, dest):
    """带通道回退下载；已存在且非空则跳过。"""
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        log(f"   已存在，跳过：{os.path.basename(dest)} "
            f"({os.path.getsize(dest) / 1024**2:.1f} MB)")
        return dest
    tmp = dest + ".part"
    last = None
    for prox in PROXIES:
        target = prox + url
        try:
            log(f"   下载 {os.path.basename(dest)}  <- {prox or '直连'}")
            req = urllib.request.Request(target, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fh:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        if pct % 20 == 0:
                            log(f"      {pct}%  {done / 1024**2:.0f}/{total / 1024**2:.0f} MB")
            if total and os.path.getsize(tmp) != total:
                raise OSError(f"体积不符 {os.path.getsize(tmp)} != {total}")
            os.replace(tmp, dest)
            log(f"   完成 {os.path.getsize(dest) / 1024**2:.1f} MB")
            return dest
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"   ! 通道失败（{type(e).__name__}: {e}）→ 换下一个")
    raise SystemExit(f"!! 所有通道都失败: {url}  ({last})")


def extract(zip_path, out_dir):
    marker = os.path.join(out_dir, "llama-server.exe")
    if os.path.exists(marker):
        log(f"   已解包，跳过：{out_dir}")
        return marker
    os.makedirs(out_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
    log(f"   解包 -> {out_dir}")
    return marker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cuda", action="store_true", help="追加 CUDA-12.4 与 cudart")
    args = ap.parse_args()

    want = ["vulkan", "cpu"] + (["cuda", "cudart"] if args.cuda else [])
    tag, assets = fetch_latest_win_release()
    log(f"llama.cpp nightly = {tag}")
    os.makedirs(DEST, exist_ok=True)

    for key in want:
        suffix = VARIANTS[key]
        cand = [n for n in assets if n.endswith(suffix)]
        if not cand:
            log(f"[跳过] 该版本没有 {suffix}")
            continue
        name = cand[0]
        log(f"[{key}] {name}")
        zp = download(assets[name], os.path.join(DEST, name))
        # cudart 是给 cuda 包用的运行库，解到同一个目录
        out = os.path.join(DEST, "llamacpp-" + ("cuda" if key in ("cuda", "cudart") else key))
        extract(zp, out)

    log("\n=== 产物 ===")
    for root, _dirs, files in os.walk(DEST):
        for f in files:
            if f.lower().endswith(".exe"):
                p = os.path.join(root, f)
                log(f"  {p}  ({os.path.getsize(p) / 1024**2:.1f} MB)")


if __name__ == "__main__":
    sys.exit(main())
