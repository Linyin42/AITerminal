# -*- coding: utf-8 -*-
"""向量库完整性抽查（**只读**）：精确字节数 + meta 一致性 + L2 范数抽样。

判据（这是"能不能拿去检索"的最低线）：
  · 文件字节必须 == n × dim × 2 + 128（.npy 头）。少一个字节都说明写坏了。
  · 已写行（< done）非零元素必须 == dim，且 L2 范数 ≈ 1.0（写入时做过归一化）。
  · done 那一行必须**全零**（还没写）；否则说明断点算错了、行号会对不上片段。

用法：
    python check_vectors.py              # 查 h3c / ruijie / tplink / huawei
    python check_vectors.py huawei       # 只查一个厂商
可在嵌入**进行中**安全运行（只读 memmap）。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402

import numpy as np


def check(vendor):
    meta_path = os.path.join(paths.INDEX, f"{vendor}_emb.meta.json")
    f16 = os.path.join(paths.INDEX, f"{vendor}_emb.f16")
    if not os.path.exists(meta_path):
        return f"[SKIP] {vendor}: 无 {meta_path}"
    meta = json.load(open(meta_path, encoding="utf-8"))
    n, dim, done = int(meta["n"]), int(meta["dim"]), int(meta["done"])
    exp = n * dim * 2 + 128
    got = os.path.getsize(f16) if os.path.exists(f16) else -1
    out = [f"--- {vendor} ---",
           f"  meta : n={n} dim={dim} done={done} failed={meta.get('failed')} @ {meta.get('updated')}",
           f"  字节 : {got} / 期望 {exp}  -> {'OK' if got == exp else '!! 不符'}"]
    if got != exp:
        return "\n".join(out)
    # ⚠ 必须用 open_memmap：这些文件是 .npy 格式，**开头有 128 字节头**。
    #   用 np.memmap(offset=0) 会把头当成数据 ⇒ 整体错位 64 个元素：
    #   首行读成 nan / 29750 这类巨大值，末行反而"看着正常"（错位窗口仍在有效数据内）。
    #   实测踩过：tplink 与 huawei 的"行0"数值完全相同 —— 因为两个文件头前缀一样。
    mm = np.lib.format.open_memmap(f16, mode="r")
    if mm.shape != (n, dim):
        return "\n".join(out + [f"  !! shape {mm.shape} != {(n, dim)}"])
    for i in sorted({0, 1, n // 3, n // 2, max(0, done - 1)}):
        if not 0 <= i < done:   # 只抽查**已写**的行；未写行本来就是零，报了是噪音
            continue
        v = np.asarray(mm[i], dtype=np.float32)
        nz, nrm = int(np.count_nonzero(v)), float(np.linalg.norm(v))
        ok = nz == dim and abs(nrm - 1.0) < 0.01
        out.append(f"  行{i:>6}: 非零 {nz}/{dim}  L2={nrm:.4f}  {'OK' if ok else '!! 异常'}")
    if done < n:
        # ⚠ 竞态：meta 每 20 批才落一次，这期间写入仍在前进 ⇒ 拿最新 meta 再判一次，
        #   否则会把"合法已写"的下一行误报成断点错位（实测踩过）。
        try:
            done2 = int(json.load(open(meta_path, encoding="utf-8"))["done"])
        except (OSError, ValueError, KeyError):
            done2 = done
        if done2 > done:
            out.append(f"  行{done:>6}(=快照 done) 已前进到 {done2}，跳过'应未写'判定（竞态，非异常）")
        else:
            v = np.asarray(mm[done], dtype=np.float32)
            out.append(f"  行{done:>6}(=done, 应未写): 非零 {int(np.count_nonzero(v))}  "
                       f"{'OK(空)' if not v.any() else '!! 已写(断点错位?)'}")
    out.append(f"  进度 : {done}/{n} = {100.0 * done / n:.1f}%"
               + ("  已写完" if done >= n else f"  还需约 {(n - done) / 65.0 / 60:.0f} 分钟(按 65 片/s)"))
    return "\n".join(out)


if __name__ == "__main__":
    for v in (sys.argv[1:] or ["h3c", "ruijie", "tplink", "huawei"]):
        print(check(v), flush=True)
