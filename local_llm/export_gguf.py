#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 LoRA 合并进基座并导出 **GGUF**（Q4_K_M），供 probe_tier/run_server 直接加载。

为什么自己写转换器（而不是用官方 `convert_hf_to_gguf.py`）
====================================================
本机 **拿不到官方脚本**，三条路都试过并留证：
  · `raw.githubusercontent.com`            → Recv failure: Connection was reset
  · `github.com/.../raw/...`                → 302 后被 reset
  · `hf-mirror.com/ggml-org/llama.cpp/...`  → 404
  · PyPI `convert-hf-to-gguf` 0.1.1         → 仓库是**随机用户名**（homepage 却挂 ggml.ai）⇒ 来源不可信，不装
于是自己写，并**以本地官方 `Qwen3-4B-Q4_K_M.gguf` 为唯一权威参照**：元数据、张量名与维度
全部按它对齐（写前比对张量清单、写后再比一次），最后用 llama-server 实测能不能正常生成。

三个关键实现点（都是先读 `gguf` 包源码确认过的，不靠猜）
====================================================
1. **维度方向**：writer 在 `write_ti_data_to_file` 里对 dims 做 `shape[n-1-j]` 反转，
   reader 读回时 `np_dims = reversed(dims)` 再反转 ⇒ **按 HF 布局 [out,in] 把 numpy 数组
   交给 `add_tensor` 就是对的**，不要自己转置（转了才是错的）。
2. **内存**：`GGUFWriter(use_temp_file=True)` 把逐张量数据溢写到临时文件，避免 398 个张量
   全堆在内存里（4B fp16 ≈ 8GB）；且**不整加载模型**，只逐张量读基座 safetensors，
   对 LoRA 覆盖的矩阵算 `W' = W + (B @ A) * (alpha/r)`（fp32 累加后转 fp16）。
3. **元数据的数组字段**要带上元素类型（`sub_type`），否则 tokenizer 数组会被写成错类型。

用法：
    python local_llm/export_gguf.py --adapter D:\\ait_llm\\train\\lora\\sft\\final
    python local_llm/export_gguf.py --verify-only --out-q4 <已存在的 gguf>
"""
from __future__ import annotations

import argparse
import io
import json
import mmap
import os
import struct
import subprocess
import sys

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LAYER_TAIL = {
    "self_attn.q_proj.weight": "attn_q.weight",
    "self_attn.k_proj.weight": "attn_k.weight",
    "self_attn.v_proj.weight": "attn_v.weight",
    "self_attn.o_proj.weight": "attn_output.weight",
    "self_attn.q_norm.weight": "attn_q_norm.weight",
    "self_attn.k_norm.weight": "attn_k_norm.weight",
    "input_layernorm.weight": "attn_norm.weight",
    "post_attention_layernorm.weight": "ffn_norm.weight",
    "mlp.gate_proj.weight": "ffn_gate.weight",
    "mlp.up_proj.weight": "ffn_up.weight",
    "mlp.down_proj.weight": "ffn_down.weight",
}
TOP_LEVEL = {
    "model.embed_tokens.weight": "token_embd.weight",
    "model.norm.weight": "output_norm.weight",
    "lm_head.weight": "output.weight",
}


def gguf_name(hf_key: str) -> str | None:
    """HF 张量名 → GGUF 张量名。**逐个列名**，映射不到就返回 None（由调用方报错，不静默跳过）。"""
    if hf_key in TOP_LEVEL:
        return TOP_LEVEL[hf_key]
    if not hf_key.startswith("model.layers."):
        return None
    parts = hf_key.split(".")
    tail = LAYER_TAIL.get(".".join(parts[3:]))
    return f"blk.{parts[2]}.{tail}" if tail else None


def base_files(base: str) -> list[str]:
    fs = sorted(f for f in os.listdir(base)
                if f.endswith(".safetensors") and not f.startswith("."))
    if not fs:
        raise SystemExit(f"!! {base} 下没有 safetensors")
    return [os.path.join(base, f) for f in fs]


#: safetensors 头里的 dtype 名 → numpy（BF16 单列处理：numpy 没有 bf16）
_ST_DTYPES = {"F64": np.float64, "F32": np.float32, "F16": np.float16,
              "I64": np.int64, "I32": np.int32, "I16": np.int16, "I8": np.int8,
              "U64": np.uint64, "U32": np.uint32, "U16": np.uint16, "U8": np.uint8,
              "BOOL": np.bool_}


def _bf16_to_f16(u16: np.ndarray, chunk: int = 1 << 22) -> np.ndarray:
    """bf16(uint16 位型) → float16。分块做，峰值内存约 chunk×4 字节。"""
    out = np.empty(u16.shape, dtype=np.float16)
    fin, fout = u16.reshape(-1), out.reshape(-1)
    for i in range(0, fin.size, chunk):
        seg = fin[i:i + chunk].astype(np.uint32) << 16        # bf16 就是 fp32 的高 16 位
        fout[i:i + chunk] = seg.view(np.float32).astype(np.float16)
    return out


class STFile:
    """手动读 safetensors（mmap + 头解析），**不走 safetensors 的 torch 后端**。

    为什么：实跑 `safe_open(...).get_tensor()` 在本机大分片上**稳定 access violation**
    （faulthandler 栈：`torch/storage.py __getitem__` ← safetensors 0.8.0 torch 后端；同一份
    代码读 95MB 小分片却正常），属于 mmap 生命周期/ABI 层问题，不是数据问题。自己读字节
    可复现，还顺手解决 numpy 不认 bf16 的问题（bf16 按 uint16 读，左移 16 位 view 成 fp32）。
    """

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "rb")
        n = struct.unpack("<Q", self._fh.read(8))[0]
        self.header = json.loads(self._fh.read(n).decode("utf-8"))
        self._start = 8 + n
        self._mm = None

    def keys(self) -> list[str]:
        return [k for k in self.header if k != "__metadata__"]

    def tensor(self, key: str, out_dtype=np.float16) -> np.ndarray:
        info = self.header[key]
        a, b = info["data_offsets"]
        if self._mm is None:
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        raw = self._mm[self._start + a: self._start + b]          # mmap 切片返回**自己拥有**的 bytes
        dt = info["dtype"]
        shape = tuple(info["shape"])
        if dt == "BF16":
            u16 = np.frombuffer(raw, dtype=np.uint16).reshape(shape)
            return _bf16_to_f16(u16).astype(out_dtype, copy=False)
        arr = np.frombuffer(raw, dtype=_ST_DTYPES[dt]).reshape(shape)
        return arr.astype(out_dtype, copy=False)

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False



def load_adapter(adapter_dir: str):
    """读 PEFT 适配器 → {模块路径: (A, B, scaling)}。走 STFile（不碰 torch 后端）。"""
    path = os.path.join(adapter_dir, "adapter_model.safetensors")
    if not os.path.exists(path):
        raise SystemExit(f"!! 缺 {path}")
    cfg_p = os.path.join(adapter_dir, "adapter_config.json")
    cfg = json.load(open(cfg_p, encoding="utf-8")) if os.path.exists(cfg_p) else {}
    r = int(cfg.get("r") or 16)
    alpha = float(cfg.get("lora_alpha") or 2 * r)
    scaling = alpha / r
    pairs: dict[str, dict] = {}
    with STFile(path) as fh:
        # ⚠ 查文件必须用**原始 key**（`base_model.model.model.layers...`），只把剥前缀后的名字
        #   当"模块路径"用。原来拿剥完的 k 去 `fh.tensor(k, …)` → KeyError（实测：
        #   `KeyError: 'model.layers.0.mlp.down_proj.lora_A.weight'`），因为文件 header 里存的是原名。
        for k0 in fh.keys():
            k = k0.replace("base_model.model.", "", 1)
            for tag, which in ((".lora_A.weight", "A"), (".lora_B.weight", "B")):
                if k.endswith(tag):
                    pairs.setdefault(k[: -len(tag)], {})[which] = fh.tensor(k0, np.float32)
    keep = {m: (d["A"], d["B"], scaling) for m, d in pairs.items() if "A" in d and "B" in d}
    print(f"适配器 {adapter_dir}\n  r={r} alpha={alpha:g} scaling={scaling:g} 覆盖模块 {len(keep)} 个")
    return keep


def clone_metadata(writer, ref_gguf: str, drop_name: bool = True) -> int:
    """把参照 GGUF 的元数据整块搬到新文件（跳过 GGUF.* 与量化键、可选跳过 general.name）。"""
    from gguf import GGUFReader, GGUFValueType
    ref = GGUFReader(ref_gguf)
    skip = {"general.file_type", "general.quantization_version"}
    n = 0
    for key, field in ref.fields.items():
        if key.startswith("GGUF.") or key in skip:
            continue
        if drop_name and key == "general.name":
            continue
        types = list(field.types) if isinstance(field.types, (list, tuple)) else [field.types]
        t = types[0]
        val = field.contents()
        if hasattr(val, "tolist"):
            val = val.tolist()
        if t == GGUFValueType.ARRAY:
            normal = [x.decode("utf-8", "replace") if isinstance(x, bytes) else x for x in val]
            sub = types[1] if len(types) > 1 else None
            # 元素类型按参照显式带上：字符串数组 vs uint32 数组不能靠猜
            if sub == GGUFValueType.STRING:
                normal = [str(x) for x in normal]
            elif sub in (GGUFValueType.UINT32, GGUFValueType.INT32):
                normal = [int(x) for x in normal]
            writer.add_key_value(key, normal, GGUFValueType.ARRAY, sub_type=sub)
        else:
            if hasattr(val, "item"):
                val = val.item()
            writer.add_key_value(key, val, t)
        n += 1
    print(f"  已搬元数据 {n} 项（参照 {os.path.basename(ref_gguf)}）")
    return n


def ref_tensor_names(ref_gguf: str) -> dict[str, tuple]:
    from gguf import GGUFReader
    return {t.name: tuple(int(x) for x in t.shape) for t in GGUFReader(ref_gguf).tensors}


def do_export(a) -> int:
    from gguf import GGUFWriter, GGMLQuantizationType

    ref_shapes = ref_tensor_names(a.ref_gguf)
    adapter = load_adapter(a.adapter) if a.adapter else {}
    files = base_files(a.base)

    # ---- 写前先核对张量清单：少一个/多一个都在这里暴露 ----
    mine: list[tuple[str, str]] = []      # (文件, hf key)
    unmapped: list[str] = []
    for fp in files:
        with STFile(fp) as fh:
            for key in fh.keys():
                if gguf_name(key) is None:
                    unmapped.append(key)
                else:
                    mine.append((fp, key))
    mine_names = {gguf_name(k) for _f, k in mine}
    if unmapped:
        print(f"!! {len(unmapped)} 个张量没有 GGUF 映射，前 5 个：{unmapped[:5]}")
        return 2
    missing = sorted(set(ref_shapes) - mine_names)
    extra = sorted(mine_names - set(ref_shapes))
    if missing or extra:
        print(f"!! 与官方清单不一致：缺 {missing[:5]}  多 {extra[:5]}")
        return 2
    print(f"张量清单核对通过：{len(mine_names)} 个，与官方一致")

    os.makedirs(os.path.dirname(a.out_f16), exist_ok=True)
    w = GGUFWriter(a.out_f16, "qwen3", use_temp_file=True)
    clone_metadata(w, a.ref_gguf)
    w.add_name(a.name)

    merged_n = written = 0
    by_file: dict[str, list[str]] = {}
    for fp, key in mine:
        by_file.setdefault(fp, []).append(key)
    for fp, keys in by_file.items():
        with STFile(fp) as fh:
            for key in keys:
                # 1 维张量（`output_norm` 与每层的 attn/ffn/q/k_norm）在官方文件里是 **F32**。
                # 给 F16 会出事：llama-quantize 只量化 2 维矩阵，1 维权重**按输入类型原样保留**，
                # 于是量化后的文件里 norm 是 f16，推理期撞 ggml-cpu
                # `binary_op: unsupported types: dst f32, src0 f32, src1 f16` 直接退出（实测踩过）。
                want_f32 = len(fh.header[key]["shape"]) == 1
                arr = fh.tensor(key, np.float32 if want_f32 else np.float16)
                mod = key.rsplit(".weight", 1)[0]
                if mod in adapter and not want_f32:
                    A, B, sc = adapter[mod]
                    merged = arr.astype(np.float32)
                    merged += (B @ A) * np.float32(sc)
                    arr = merged.astype(np.float16)
                    del merged
                    merged_n += 1
                w.add_tensor(gguf_name(key), np.ascontiguousarray(arr),
                             raw_dtype=(GGMLQuantizationType.F32 if want_f32
                                        else GGMLQuantizationType.F16))
                written += 1
        print(f"  {os.path.basename(fp)}: 已写 {written} 个张量（合并 LoRA {merged_n} 个）", flush=True)

    w.write_header_to_file()
    w.write_kv_data_to_file()
    # ⚠ 不要自己调 write_ti_data_to_file()：write_tensors_to_file() 内部会调它
    #   （gguf_writer.py:439）。多调一次会把状态推成 TI_DATA，内部再调就撞
    #   "Expected output file to contain KV data, got WriterState.TI_DATA"（实测踩过）。
    w.write_tensors_to_file(progress=True)
    w.close()
    print(f"✓ f16 GGUF：{a.out_f16}  {os.path.getsize(a.out_f16)/2**30:.2f} GB（合并了 {merged_n} 个矩阵）")
    return 0


def verify(q4: str, ref: str) -> int:
    from gguf import GGUFReader
    A, B = GGUFReader(q4), GGUFReader(ref)
    na = {t.name: tuple(int(x) for x in t.shape) for t in A.tensors}
    nb = {t.name: tuple(int(x) for x in t.shape) for t in B.tensors}
    print(f"新文件 {len(na)} 张量 / 参照 {len(nb)} 张量")
    only_a, only_b = sorted(set(na) - set(nb)), sorted(set(nb) - set(na))
    diff = [(k, na[k], nb[k]) for k in na if k in nb and na[k] != nb[k]]
    print(f"  多出：{only_a[:5] or '无'}   缺失：{only_b[:5] or '无'}   维度不同：{diff[:5] or '无'}")
    # 必须比**类型**：只比名字+维度会漏掉 F16/F32 不匹配 —— 那正是推理期
    # `binary_op: unsupported types: dst f32, src0 f32, src1 f16` 的来源（实测踩过）。
    tya = {t.name: t.tensor_type for t in A.tensors}
    tyb = {t.name: t.tensor_type for t in B.tensors}
    tdiff = [(k, str(tya[k]).split(".")[-1], str(tyb[k]).split(".")[-1])
             for k in tya if k in tyb and tya[k] != tyb[k]]
    print(f"  类型不同：{len(tdiff)} 个" + (f"  例：{tdiff[:3]}" if tdiff else ""))
    # 注意：这版 gguf 的 `fields` 是 **dict**（key → field），直接迭代得到的是字符串 key，
    # 写 `{f.name for f in A.fields}` 会 AttributeError（实测踩过）。
    fa = set(A.fields) - {"GGUF.tensor_count", "GGUF.kv_count"}
    fb = set(B.fields) - {"GGUF.tensor_count", "GGUF.kv_count"}
    print(f"  元数据：新 {len(fa)} 项 / 参照 {len(fb)} 项；缺 {sorted(fb-fa)[:6] or '无'}")
    ok = not only_a and not only_b and not diff and not tdiff and not (fb - fa)
    print("✓ 张量清单/维度/元数据与官方一致" if ok else "!! 与官方不一致（上面已列出）")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=r"D:\ait_llm\models_hf\Qwen3-4B")
    ap.add_argument("--adapter", default="")
    ap.add_argument("--ref-gguf", default=r"D:\ait_llm\models\Qwen3-4B-Q4_K_M.gguf")
    ap.add_argument("--out-f16", default=r"D:\ait_llm\train\gguf\Qwen3-4B-AIT-f16.gguf")
    ap.add_argument("--out-q4", default=r"D:\ait_llm\models\Qwen3-4B-AIT-Q4_K_M.gguf")
    ap.add_argument("--name", default="Qwen3 4B AIT-Tuned")
    ap.add_argument("--quantize", default="")
    ap.add_argument("--skip-quantize", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args()

    if a.verify_only:
        return verify(a.out_q4, a.ref_gguf)
    rc = do_export(a)
    if rc or a.skip_quantize:
        return rc
    if not a.quantize:
        import glob
        cands = glob.glob(os.path.join(os.path.dirname(os.path.dirname(a.ref_gguf)),
                                       "..", "bin", "llamacpp-*", "llama-quantize.exe"))
        cands = [os.path.normpath(c) for c in cands] or \
            glob.glob(r"D:\ait_llm\bin\llamacpp-*\llama-quantize.exe")
        a.quantize = cands[0] if cands else ""
    if not a.quantize:
        print("!! 找不到 llama-quantize.exe（用 --quantize 指定）")
        return 2
    print(f"\n量化：{a.quantize} → Q4_K_M")
    p = subprocess.run([a.quantize, a.out_f16, a.out_q4, "Q4_K_M"], text=True,
                       encoding="utf-8", errors="replace")
    print(f"quantize 退出码 {p.returncode}")
    if p.returncode != 0 or not os.path.exists(a.out_q4):
        return 3
    print(f"✓ Q4_K_M：{a.out_q4}  {os.path.getsize(a.out_q4)/2**30:.2f} GB")
    return verify(a.out_q4, a.ref_gguf)


if __name__ == "__main__":
    sys.exit(main())
