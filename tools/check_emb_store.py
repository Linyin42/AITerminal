# -*- coding: utf-8 -*-
"""向量库交付级自检：核对 `<vendor>_emb.f16` 与它的元数据、切片是否严丝合缝。

为什么不能只看"文件在"：嵌入是**可续跑**的（断点后接着写），所以"文件存在且不小"
完全可能是"只写了一半、后面全是 0"的假完成 —— 交付到客户机上才会发现检索答非所问。
本脚本按四件事逐条核对：

  ① 规模：`.f16` 的形状 == (meta.n, 1024)，且 == 切片 jsonl 的行数（行号↔片段严格对齐）
  ② 字节：文件大小 == npy 头（128B）+ n×1024×2（多一字节少一字节都说明写过别的东西）
  ③ 数值：抽样向量的 L2 范数 ≈ 1.0000（写入前做了 normalize；`local_rag.search` 靠
     "已归一化 ⇒ 分数即余弦"来做跨厂商合并排序，范数不对则跨厂商结果不可比）
  ④ 健康：无 NaN/Inf；若未跑完，报出断点位置与尾部状态，而不是当成通过

用法：
    python tools\\check_emb_store.py                 # 检查 index 目录下全部厂商
    python tools\\check_emb_store.py ruijie tplink   # 只检查指定厂商
    python tools\\check_emb_store.py --sample 32     # 抽样条数（默认 24）
退出码：0 = 无 FAIL（未跑完的记为 WIP）；1 = 有厂商 FAIL
"""
import argparse
import json
import os
import sys

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
sys.path.insert(0, os.path.join(P, "local_llm"))

import numpy as np                      # noqa: E402
import paths                            # noqa: E402  （local_llm/paths.py）

DIM = 1024


def count_lines(path):
    n = 0
    with open(path, "rb") as fh:
        for _ in fh:
            n += 1
    return n


def check_vendor(vendor, sample):
    """返回 (状态, [行…])，状态 ∈ PASS/WIP/FAIL。"""
    lines, fails = [], []
    f16 = os.path.join(paths.INDEX, f"{vendor}_emb.f16")
    meta_path = os.path.join(paths.INDEX, f"{vendor}_emb.meta.json")
    ck = os.path.join(paths.INDEX, f"{vendor}_chunks.jsonl")

    if not os.path.exists(f16):
        return "FAIL", [f"  缺少 {os.path.basename(f16)}"]
    if not os.path.exists(meta_path):
        return "FAIL", [f"  缺少 {os.path.basename(meta_path)}"]
    meta = json.load(open(meta_path, encoding="utf-8"))
    n = int(meta.get("n") or 0)
    done = int(meta.get("done") or 0)
    failed = int(meta.get("failed") or 0)

    try:
        arr = np.load(f16, mmap_mode="r")
    except Exception as e:                                      # noqa: BLE001
        return "FAIL", [f"  np.load 失败（不是合法 npy，或写入中断）: {type(e).__name__}: {e}"]

    # ① 规模
    if tuple(arr.shape) != (n, DIM):
        fails.append(f"  形状 {tuple(arr.shape)} != (meta.n={n}, {DIM})")
    if os.path.exists(ck):
        nl = count_lines(ck)
        if nl != n:
            fails.append(f"  切片行数 {nl} != meta.n {n}（行号↔片段已错位）")
        else:
            lines.append(f"  切片行数 = {nl}  ✓ 行号↔片段对齐")
    else:
        fails.append("  缺少切片 jsonl")

    # ② 字节
    size = os.path.getsize(f16)
    header = size - int(arr.size) * 2
    if header != 128:
        fails.append(f"  npy 头 {header}B != 128B（文件可能被别的东西写过）")
    else:
        lines.append(f"  字节 = {size:,} = 头 128 + {n}×{DIM}×2  ✓")

    # ④ 先摸清"真实写入前沿"：meta.done 每 20 批才落一次盘、落后于真实进度，
    #    所以 WIP 时必须**只抽样已写入区间** —— 否则未写行的全零向量会被当成
    #    "范数 0" 误报成越界（本脚本第一版的真实缺陷）。
    frontier = n
    if done < n:
        frontier = done
        step = 0
        while frontier < n and step < 6000 and np.any(arr[frontier]):
            frontier += 1
            step += 1
        lines.append(f"  未跑完：meta.done={done}/{n}；真实写入前沿 ≈ {frontier}"
                     f"（checkpoint 落后 {frontier - done} 行，正常）；meta.failed={failed}")

    hi_row = max(1, min(frontier, n))
    take = sorted({0, hi_row - 1, hi_row // 2}
                  | {int(i) for i in np.linspace(0, hi_row - 1, sample)})

    # ③ 数值 + 健康：只在已写入的行里抽样
    norms, bad = [], []
    for i in take:
        v = arr[i].astype(np.float32)
        if not np.isfinite(v).all():
            bad.append(i)
            continue
        norms.append(float(np.linalg.norm(v)))
    if bad:
        fails.append(f"  抽样 {len(bad)} 条含 NaN/Inf（下标 {bad[:5]}）")
    if norms:
        lo, hi = min(norms), max(norms)
        mean = sum(norms) / len(norms)
        if not (0.999 <= lo and hi <= 1.001):
            fails.append(f"  范数越界（期望 ≈1.0000）：min={lo:.4f} max={hi:.4f}")
        else:
            lines.append(f"  已写入区间抽样 {len(norms)} 条：范数 min={lo:.4f} max={hi:.4f}"
                         f" 均值={mean:.4f}  ✓")
    if done < n:
        tail_zero = True if frontier >= n else not np.any(arr[frontier])
        lines.append(f"  前沿之后全零 = {tail_zero}（未写部分不会被当成有效向量）")
        return "WIP", lines + fails
    if failed:
        fails.append(f"  meta.failed={failed}（有片段没嵌入成功）")
    return ("FAIL" if fails else "PASS"), lines + fails


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:                                       # noqa: BLE001
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("vendors", nargs="*", help="留空 = index 目录下全部厂商")
    ap.add_argument("--sample", type=int, default=24)
    args = ap.parse_args()

    print(f"=== 向量库自检  index={paths.INDEX} ===")
    print(f"    {paths.describe()}")
    names = args.vendors or sorted(
        f[:-len("_emb.meta.json")] for f in os.listdir(paths.INDEX)
        if f.endswith("_emb.meta.json"))
    if not names:
        print("  ！没有找到任何 *_emb.meta.json")
        return 1

    tally = {"PASS": 0, "WIP": 0, "FAIL": 0}
    for v in names:
        status, lines = check_vendor(v, args.sample)
        tally[status] += 1
        print(f"\n[{status}] {v}")
        for ln in lines:
            print(ln)
    print(f"\n{'=' * 66}")
    print(f"共 {len(names)} 家：PASS={tally['PASS']}  WIP={tally['WIP']}  FAIL={tally['FAIL']}")
    return 1 if tally["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
