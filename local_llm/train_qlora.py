#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地领域微调（QLoRA）—— 让模型"自己就会"厂商产品文档。

两阶段（都能单独跑，checkpoint 落 D:\\ait_llm\\train\\）：
  cpt  领域继续预训练：在 build_train_set.py 产出的 cpt_corpus.txt 上做 LoRA CPT
       （学**术语/命令语法/型号体系**，这一步不改问答行为，但让 SFT 更省样本）
  sft  指令微调：在 sft_qa.jsonl 上按 Qwen3 chat 模板训练，**只对答案算损失**
       （学"用户这样问 → 这样答"的形态）
  smoke 20 步冒烟：验证 4bit 加载 / LoRA 挂载 / Trainer API（transformers 5.x）/ 显存 / tokens/s

为什么不是"直接全参微调"
======================
本机 RTX 3060 Laptop **6GB**：4B 全参微调要 权重(8GB bf16)+梯度+AdamW 状态 ≈ 50GB+，
装不下；QLoRA（nf4 + LoRA + 梯度检查点）才有现实解。

⚠ 一句实话（评测会给出数字）：**微调不替代检索**。LoRA 能把术语、命令格式、"按文档口径
回答"的形态学进去，但"某型号某版本的某个字段含义"这类精确事实，召回仍以 RAG 为主。
所以本脚本只负责产出模型，最终结论以 evaluate 环节的四组对比为准。

用法
====
  python local_llm/train_qlora.py smoke
  python local_llm/train_qlora.py cpt --max-steps 3000
  python local_llm/train_qlora.py sft --epochs 1
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TRAIN_DIR = r"D:\ait_llm\train"
BASE_MODEL = r"D:\ait_llm\models_hf\Qwen3-4B"
OUT_ROOT = os.path.join(TRAIN_DIR, "lora")


def load_tokenizer(base: str):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(base, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    return tok


def build_model(base: str, r: int = 16, alpha: int = 32, dropout: float = 0.05,
                resume: str = ""):
    """4bit 量化 + LoRA。返回 (model, tokenizer)。

    `resume` 给上一阶段的 LoRA 目录（如 CPT 的 final）时，**接着它继续训**（SFT 接 CPT）。
    """
    import torch
    from transformers import AutoModelForCausalLM, BitsAndBytesConfig
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training, PeftModel

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    tok = load_tokenizer(base)
    model = AutoModelForCausalLM.from_pretrained(
        base, quantization_config=bnb, device_map={"": 0}, torch_dtype=torch.bfloat16,
    )
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model.config.use_cache = False
    if resume:
        print(f"接着上一阶段 LoRA 继续训：{resume}")
        model = PeftModel.from_pretrained(model, resume, is_trainable=True)
    else:
        lora = LoraConfig(
            r=r, lora_alpha=alpha, lora_dropout=dropout, bias="none", task_type="CAUSAL_LM",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                            "gate_proj", "up_proj", "down_proj"],
        )
        model = get_peft_model(model, lora)
    model.print_trainable_parameters()
    return model, tok


def _trainer_kwargs(api_tokenizer_kw: str = "processing_class"):
    """transformers 5.x 用 `processing_class`，4.x 用 `tokenizer` —— 这里做兼容。"""
    return api_tokenizer_kw


# --------------------------------------------------------------------------- CPT
def run_cpt(args) -> int:
    import torch
    from datasets import Dataset
    from transformers import DataCollatorForLanguageModeling, Trainer, TrainingArguments

    corpus = os.path.join(TRAIN_DIR, "cpt_corpus.txt")
    if not os.path.exists(corpus):
        print(f"!! 缺语料：{corpus}（先跑 build_train_set.py）")
        return 2
    model, tok = build_model(args.base, r=args.lora_r, alpha=args.lora_alpha)
    raw = open(corpus, "r", encoding="utf-8", errors="replace").read()
    print(f"语料 {len(raw)/1048576:.2f} MB；切块 seq_len={args.seq_len}")
    # ⚠ 不能把整个 44MB 语料一次交给 tokenizer：Rust 侧编码 29M token 的中间结构要 ~2.28GB
    #   连续内存，实测**稳定崩**（`memory allocation of 2281701392 bytes failed`，
    #   退出码 0xC0000409），且与显存无关。按行边界分块编码后拼接即可，峰值降到几十 MB。
    import numpy as np
    ids: list[int] = []
    CHUNK = 256 * 1024
    pos = 0
    while pos < len(raw):
        end = min(len(raw), pos + CHUNK)
        if end < len(raw):
            nl = raw.rfind("\n", pos, end)
            if nl > pos:
                end = nl + 1
        ids.extend(tok(raw[pos:end], add_special_tokens=False)["input_ids"])
        pos = end
    del raw
    L = args.seq_len
    flat = np.asarray(ids, dtype=np.int32)          # 29M int32 ≈ 116MB（Python list 要 ~800MB）
    del ids
    n_blocks = (len(flat) - L + 1) // L
    blocks = flat[: n_blocks * L].reshape(n_blocks, L)
    ds = Dataset.from_dict({"input_ids": blocks})
    print(f"切出 {len(ds):,} 个 block（{len(ds)*L/1e6:.1f}M token）")

    targs = TrainingArguments(
        output_dir=os.path.join(OUT_ROOT, "cpt"),
        per_device_train_batch_size=args.bs,
        gradient_accumulation_steps=args.accum,
        learning_rate=args.lr,
        max_steps=args.max_steps,
        warmup_steps=max(10, args.max_steps // 20),
        logging_steps=args.log_every,
        save_steps=args.save_every,
        save_total_limit=2,
        bf16=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        optim=args.optim,
        lr_scheduler_type="cosine",
        report_to=[],
        dataloader_pin_memory=False,
        seed=args.seed,
    )
    trainer = Trainer(
        model=model, args=targs, train_dataset=ds,
        data_collator=DataCollatorForLanguageModeling(tokenizer=tok, mlm=False),
        **{_trainer_kwargs(): tok},
    )
    trainer.train()
    trainer.save_model(os.path.join(OUT_ROOT, "cpt", "final"))
    tok.save_pretrained(os.path.join(OUT_ROOT, "cpt", "final"))
    print("✓ CPT LoRA 已存：", os.path.join(OUT_ROOT, "cpt", "final"))
    return 0


# --------------------------------------------------------------------------- SFT
def _ids(out) -> list[int]:
    """把 `apply_chat_template` 的返回**归一成 list[int]**。

    transformers 5 下它可能返回 dict（含 `input_ids`）/张量/嵌套 list；直接 `list(out)`
    会拿到字符串 key（`"input_ids"`），于是 labels 被整段掩成 -100、`_encode_sft` 返回 None，
    样本数静默变成 **0 条**（实测踩过：跑完发现 `SFT 样本 0 条`）。
    """
    if isinstance(out, dict):
        out = out["input_ids"]
    elif hasattr(out, "input_ids"):
        out = out.input_ids
    if hasattr(out, "tolist") and not isinstance(out, list):
        out = out.tolist()
    if len(out) and isinstance(out[0], (list, tuple)):
        out = out[0]
    return [int(x) for x in out]


def _apply_template(tok, msgs, add_gen: bool = False) -> list[int]:
    """渲染 Qwen3 chat 模板 → token id 列表。

    带 `enable_thinking=False`：与推理对齐（run_server 用 `--reasoning off`，content 直出），
    否则等于在教"先思考再回答"的格式、而推理时又把它关掉。
    模板不认这个 kwarg 时退回去只给 tokenize。
    """
    for extra in ({"enable_thinking": False}, {}):
        try:
            return _ids(tok.apply_chat_template(msgs, add_generation_prompt=add_gen,
                                                tokenize=True, **extra))
        except TypeError:
            continue
    raise RuntimeError("apply_chat_template 不可用")


def _encode_sft(tok, question: str, answer: str, max_len: int):
    """只对 **答案** 算损失：prompt 部分 labels=-100。

    超长（> max_len）**丢弃**而不是截断：被截断的样本等于在教模型"说到一半就停"。
    seq_len=512 时实测约 19% 的样本超长（input_ids 中位 258、答案 token 中位 222）。
    """
    msgs = [{"role": "user", "content": question}]
    prompt_ids = _apply_template(tok, msgs, add_gen=True)
    full = _apply_template(tok, msgs + [{"role": "assistant", "content": answer}])
    if len(full) > max_len:
        return None
    labels = list(full)
    for i in range(min(len(prompt_ids), len(labels))):
        labels[i] = -100
    if all(x == -100 for x in labels):
        return None
    return {"input_ids": full, "labels": labels, "attention_mask": [1] * len(full)}


def run_sft(args) -> int:
    import json as _json
    from datasets import Dataset
    from transformers import Trainer, TrainingArguments

    path = os.path.join(TRAIN_DIR, "sft_qa.jsonl")
    if not os.path.exists(path):
        print(f"!! 缺 SFT 数据：{path}")
        return 2
    model, tok = build_model(args.base, r=args.lora_r, alpha=args.lora_alpha,
                             resume=args.resume)
    rows = []
    seen = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            seen += 1
            r = _json.loads(line)
            enc = _encode_sft(tok, r["question"], r["answer"], args.seq_len)
            if enc:
                rows.append(enc)
    print(f"SFT 样本 {len(rows):,} 条（seq_len≤{args.seq_len}，只对答案算损失）；"
          f"注入 {seen:,} 条、丢弃 {seen - len(rows):,} 条（超长或纯 prompt）")
    if not rows:
        print("!! 样本数 0：模板/labels 掩码有问题，中止（否则会白跑一整轮 GPU）")
        return 2
    ds = Dataset.from_list(rows)

    def collate(features):
        maxlen = max(len(f["input_ids"]) for f in features)
        pad = tok.pad_token_id
        batch = {"input_ids": [], "labels": [], "attention_mask": []}
        for f in features:
            n = maxlen - len(f["input_ids"])
            batch["input_ids"].append(f["input_ids"] + [pad] * n)
            batch["labels"].append(f["labels"] + [-100] * n)
            batch["attention_mask"].append(f["attention_mask"] + [0] * n)
        import torch
        return {k: torch.tensor(v, dtype=torch.long) for k, v in batch.items()}

    targs = TrainingArguments(
        output_dir=os.path.join(OUT_ROOT, "sft"),
        per_device_train_batch_size=args.bs,
        gradient_accumulation_steps=args.accum,
        num_train_epochs=args.epochs,
        # ⚠ 只给 num_train_epochs 会按 19,883 条 × bs1 跑满 **19,883 步（≈27 小时）**；
        #   本机 6GB 显存下必须显式限步数（`--sft-max-steps`，0=按 epochs）。
        max_steps=args.sft_max_steps if args.sft_max_steps > 0 else -1,
        learning_rate=args.lr,
        # ⚠ transformers 5 **删掉了 `warmup_ratio`**（实测 `TypeError: __init__() got an
        #   unexpected keyword argument 'warmup_ratio'`），只能给绝对步数：按 max_steps 的 3% 折算。
        warmup_steps=max(1, int(0.03 * (args.sft_max_steps or 1000))),
        logging_steps=args.log_every,
        save_steps=args.save_every,
        save_total_limit=2,
        bf16=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        optim=args.optim,
        lr_scheduler_type="cosine",
        report_to=[],
        dataloader_pin_memory=False,
        seed=args.seed,
    )
    trainer = Trainer(model=model, args=targs, train_dataset=ds, data_collator=collate,
                      **{_trainer_kwargs(): tok})
    trainer.train()
    trainer.save_model(os.path.join(OUT_ROOT, "sft", "final"))
    tok.save_pretrained(os.path.join(OUT_ROOT, "sft", "final"))
    print("✓ SFT LoRA 已存：", os.path.join(OUT_ROOT, "sft", "final"))
    return 0


# ------------------------------------------------------------------------- smoke
def run_smoke(args) -> int:
    import torch
    from datasets import Dataset
    from transformers import DataCollatorForLanguageModeling, Trainer, TrainingArguments

    print(f"CUDA={torch.cuda.is_available()} {torch.cuda.get_device_name(0)}")
    t0 = time.time()
    model, tok = build_model(args.base, r=8, alpha=16)
    print(f"模型加载+LoRA {time.time()-t0:.1f}s；显存 {torch.cuda.memory_allocated()/2**20:.0f}MB allocated "
          f"/ {torch.cuda.max_memory_allocated()/2**20:.0f}MB peak")
    L, N = args.seq_len, 16
    filler = tok("华为 S16700 display vlan 命令回显字段含义。", add_special_tokens=False)["input_ids"]
    blocks = [(filler * (L // max(1, len(filler)) + 1))[:L] for _ in range(N)]
    ds = Dataset.from_dict({"input_ids": blocks, "attention_mask": [[1] * L] * N})
    targs = TrainingArguments(
        output_dir=os.path.join(OUT_ROOT, "_smoke"), per_device_train_batch_size=args.bs,
        gradient_accumulation_steps=1, max_steps=args.max_steps, learning_rate=2e-4,
        logging_steps=1, save_strategy="no", bf16=True, gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False}, optim=args.optim,
        report_to=[], dataloader_pin_memory=False, seed=1,
    )
    trainer = Trainer(model=model, args=targs, train_dataset=ds,
                      data_collator=DataCollatorForLanguageModeling(tokenizer=tok, mlm=False),
                      **{_trainer_kwargs(): tok})
    t1 = time.time()
    out = trainer.train()
    dt = time.time() - t1
    steps_tok = args.max_steps * args.bs * L
    print(f"冒烟 {args.max_steps} 步 用时 {dt:.1f}s ⇒ {steps_tok/dt:.0f} token/s（bs={args.bs} seq={L}）")
    print(f"显存峰值 {torch.cuda.max_memory_allocated()/2**20:.0f}MB / 总 "
          f"{torch.cuda.get_device_properties(0).total_memory/2**20:.0f}MB")
    print(f"loss {out.training_loss:.4f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["smoke", "cpt", "sft"])
    ap.add_argument("--base", default=BASE_MODEL)
    ap.add_argument("--resume", default="")     # SFT 可接在 CPT 的 LoRA 之上
    ap.add_argument("--seq-len", type=int, default=1024)
    ap.add_argument("--bs", type=int, default=1)
    ap.add_argument("--accum", type=int, default=8)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--sft-max-steps", type=int, default=0,
                    help="SFT 显式步数上限（0=按 --epochs 跑；本机必须给，否则会跑满 19,883 步）")
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--log-every", type=int, default=10)
    ap.add_argument("--save-every", type=int, default=200)
    ap.add_argument("--optim", default="adamw_torch", help="LoRA 只训 16.5M 参数，8bit 分页优化器省不到显存反而更慢")
    ap.add_argument("--seed", type=int, default=20261018)
    a = ap.parse_args()

    os.makedirs(OUT_ROOT, exist_ok=True)
    if not os.path.isdir(a.base):
        print(f"!! 基座不存在：{a.base}（需先 snapshot_download Qwen/Qwen3-4B）")
        return 2
    return {"smoke": run_smoke, "cpt": run_cpt, "sft": run_sft}[a.mode](a)


if __name__ == "__main__":
    sys.exit(main())
