# -*- coding: utf-8 -*-
r"""批量嵌入 tech 切片（RAG 第 2 步），产出可续跑的 float16 向量库。

设计要点
  · **嵌入服务自管**：bge-m3 用 llama-server 的 `--embedding` 模式跑在 :8081；
    本脚本若发现端口不通就自己拉起来（避免"忘了起服务"这种低级返工）。
  · **归一化存储**：写入前做 L2 归一化 ⇒ 检索时余弦相似度 = 点积，省一次除法。
  · **float16**：20-25 万片段 × 1024 维，fp16 约 0.5GB（fp32 翻倍），显存/内存都省一半。
  · **可续跑**：写 `<vendor>_emb.f16` 前先看 meta.json 的 done；中断后重跑从断点接。
  · 先不引 FAISS：这个量级 numpy 暴力余弦（单查询 1-2 GFLOP）足够，零新依赖。

用法：
    python embed_chunks.py --probe                    # 自检：维度 + 相似度有序性
    python embed_chunks.py --vendor h3c               # 全量嵌入 h3c
    python embed_chunks.py --vendor h3c --limit 200   # 小样本试跑
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402  （同目录；路径的唯一来源见 paths.py）

INDEX_DIR = paths.INDEX
EMBED_MODEL = os.path.join(paths.MODELS, "bge-m3-Q8_0.gguf")
EMBED_PORT = 8081
API_KEY = "local-llm"
EMBED_URL = f"http://127.0.0.1:{EMBED_PORT}/v1/embeddings"
HEALTH_URL = f"http://127.0.0.1:{EMBED_PORT}/health"


def log(msg):
    print(msg, flush=True)


def _checkpoint(mm, meta_path, vendor, n, dim, done, failed):
    """把当前进度落盘成 meta.json（断点）。

    ⚠ 必须**周期性**调用，不能只在收尾写。本机实测：中断一次就让
    6465/93629 的进度全部作废（重跑从 0 开始，与第 9 行"可续跑"的承诺不符）。
    被 `taskkill /F` 硬杀时 Python 的 finally 根本跑不到，唯一能保命的就是周期落盘。
    先写 .tmp 再 os.replace：写一半被打断不会留下半截 JSON（半截 JSON 会被上层
    当成"没有断点"，等于白写）。
    """
    mm.flush()
    tmp = meta_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"vendor": vendor, "n": n, "dim": dim, "done": int(done),
                   "failed": failed, "dtype": "float16", "normalized": True,
                   "model": "bge-m3-Q8_0",
                   "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                  fh, ensure_ascii=False, indent=2)
    os.replace(tmp, meta_path)


def _get(url, timeout=5):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def embed_up():
    try:
        return '"ok"' in _get(HEALTH_URL, timeout=3)
    except Exception:  # noqa: BLE001
        return False


def ensure_embed_server():
    """嵌入服务不在就拉起来（bge-m3 用 CLS pooling —— 该模型的规定）。"""
    if embed_up():
        log(f"嵌入服务已在 :{EMBED_PORT}")
        return
    if not os.path.exists(EMBED_MODEL):
        raise SystemExit(f"!! 缺嵌入模型 {EMBED_MODEL}（python download_models.py core）")
    exe = paths.server_exe()
    if not exe:
        raise SystemExit(f"!! 找不到 llama-server.exe（找过 {paths.BIN}\\llamacpp-*）")
    # ⚠ `-ub`（物理批大小）默认只有 512，会**直接拒掉**长片段。实测服务端原话：
    #     input (789 tokens) is too large to process. increase the physical batch size
    #   我们的片段平均 ~600 token、最长 ~2100 token，所以必须把 -b/-ub 抬到与 -c 同量级。
    cmd = [exe, "-m", EMBED_MODEL, "--host", "127.0.0.1", "--port", str(EMBED_PORT),
           "--embedding", "--pooling", "cls", "-c", "8192", "-b", "8192", "-ub", "8192",
           "-ngl", "99", "--api-key", API_KEY]
    log("拉起嵌入服务: " + " ".join(cmd))
    logf = open(os.path.join(HERE, "_llama_embed.log"), "ab", buffering=0)
    subprocess.Popen(cmd, cwd=os.path.dirname(exe), stdout=logf, stderr=logf,
                     creationflags=0x00000008 | 0x00000200)
    for _ in range(120):
        if embed_up():
            log("嵌入服务就绪")
            return
        time.sleep(1)
    raise SystemExit("!! 嵌入服务 120s 内未就绪，看 local_llm\\_llama_embed.log")


def embed_batch(texts, timeout=180):
    """一次请求嵌一批，返回 [[float], ...]（原样，未归一化）。"""
    data = json.dumps({"input": texts, "model": "bge-m3"},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(EMBED_URL, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {API_KEY}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read().decode("utf-8", "replace"))
    items = res.get("data") or []
    items = sorted(items, key=lambda d: d.get("index", 0))
    return [d.get("embedding") or [] for d in items]


def _embed_one_with_fallback(text):
    """单条嵌入；过长就按字符逐步截断。

    这是 `-ub` 之外的**兜底**：无论服务端怎么配，都不该因为一条超长片段
    让整批失败、更不该让整个厂商的嵌入跑不下去。
    """
    for cut in (len(text), 3000, 2000, 1000, 500):
        try:
            out = embed_batch([text[:cut]])
            if out and out[0]:
                return out[0]
        except Exception:  # noqa: BLE001
            continue
    return []


def normalize(vec):
    s = sum(v * v for v in vec) ** 0.5
    return [v / s for v in vec] if s > 0 else vec


def probe():
    """自检：维度对不对、相似度是否有序（同主题 > 跨主题）。"""
    ensure_embed_server()
    a = "H3C 上配置 IPv4 基本 ACL，使用 acl number 2000 并添加 rule"
    b = "H3C 交换机上如何创建基本 ACL 编号 2000 并配置规则"
    c = "今天天气不错，适合出门散步"
    va, vb, vc = (normalize(v) for v in embed_batch([a, b, c]))
    dim = len(va)
    dot = lambda x, y: sum(p * q for p, q in zip(x, y))
    log(f"维度 = {dim}（bge-m3 应为 1024）")
    log(f"cos(ACL问题, ACL同义) = {dot(va, vb):.4f}   ← 应明显偏高")
    log(f"cos(ACL问题, 无关句) = {dot(va, vc):.4f}   ← 应明显偏低")
    ok = dim == 1024 and dot(va, vb) > dot(va, vc)
    log(("PASS" if ok else "FAIL") + " 嵌入链路自检")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

    if args.probe:
        return probe()
    if not args.vendor:
        raise SystemExit("需要 --vendor 或 --probe")

    src = os.path.join(INDEX_DIR, f"{args.vendor}_chunks.jsonl")
    if not os.path.exists(src):
        raise SystemExit(f"!! 缺切片文件 {src}（先跑 python build_index.py --vendor {args.vendor} --chunk）")

    texts = []
    with open(src, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                texts.append(json.loads(line)["text"])
            except (ValueError, KeyError):
                continue
    if args.limit:
        texts = texts[:args.limit]
    n = len(texts)

    ensure_embed_server()
    first_vec = _embed_one_with_fallback(texts[0])
    dim = len(first_vec)
    if dim != 1024:
        raise SystemExit(f"!! 维度异常 {dim}，预期 1024（检查 --pooling cls 与 -ub 是否够大）")

    out = os.path.join(INDEX_DIR, f"{args.vendor}_emb.f16")
    meta_path = os.path.join(INDEX_DIR, f"{args.vendor}_emb.meta.json")
    done = 0
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
            if meta.get("dim") == dim and meta.get("n") == n and os.path.exists(out):
                done = min(int(meta.get("done") or 0), n)
        except (OSError, ValueError):
            done = 0

    import numpy as np
    log(f"{args.vendor}: {n} 片段 / dim={dim} / 已完成 {done} / 目标文件 {out}")
    mm = np.lib.format.open_memmap(out, mode="r+" if done else "w+",
                                   dtype=np.float16, shape=(n, dim))
    t0 = time.time()
    idx = done
    start = done          # 速率/ETA 按"本次进程新增了多少"算：用绝对下标 idx 会在
                          # 续跑时虚高（如 done=10 万、跑了 10 秒 ⇒ 报 1 万片/s），
                          # 2026-10-17 修。
    failed = 0
    # 首片已算过，直接落盘，避免重复请求
    if done == 0:
        mm[0] = np.asarray(normalize(first_vec), dtype=np.float16)
        idx = 1
    try:
        while idx < n:
            chunk = texts[idx:idx + args.batch]
            vecs = []
            try:
                vecs = embed_batch(chunk)
                if len(vecs) != len(chunk):
                    raise ValueError(f"返回 {len(vecs)} 条 != 请求 {len(chunk)} 条")
            except Exception as e:  # noqa: BLE001
                # 整批失败（或条数不齐）→ 逐条兜底，保证"行号 ↔ 片段"严格对齐
                log(f"   ! 第 {idx} 批走逐条兜底（{type(e).__name__}: {e}）")
                vecs = [_embed_one_with_fallback(t) for t in chunk]
            for j, v in enumerate(vecs):
                if v:
                    mm[idx + j] = np.asarray(normalize(v), dtype=np.float16)
                else:
                    failed += 1
            idx += len(chunk)
            if idx % (args.batch * 20) < args.batch:
                _checkpoint(mm, meta_path, args.vendor, n, dim, idx, failed)
                rate = (idx - start) / max(time.time() - t0, 0.1)
                eta = (n - idx) / rate / 60 if rate > 0 else 0
                log(f"   {idx}/{n}  {rate:.1f} 片/s  预计还需 {eta:.1f} 分钟  失败 {failed}")
    finally:
        # Ctrl+C / 异常退出时留下断点。⚠ 被 taskkill /F 硬杀时这段跑不到 ——
        # 那种情况只能靠上面每 20 批一次的周期断点兜底。
        _checkpoint(mm, meta_path, args.vendor, n, dim, idx, failed)
    log(f"完成 {idx}/{n}，用时 {(time.time() - t0) / 60:.1f} 分钟 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
