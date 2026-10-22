# -*- coding: utf-8 -*-
"""定位 QLoRA 吞吐瓶颈：同一份 4B 4bit 模型，只改 seq_len / 分配器配置，测 s/it。

为什么要单独测：官方脚本 smoke 每次都要重载模型（12.6s）+ 20 步（~3 分钟），
而排查瓶颈需要"同一进程内"对比多个配置，否则差异会被加载波动掩盖。
"""
from __future__ import annotations

import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch
from datasets import Dataset
from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
                          DataCollatorForLanguageModeling, Trainer, TrainingArguments)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

BASE = r"D:\ait_llm\models_hf\Qwen3-4B"
OUT = r"D:\ait_llm\train\lora\_bench"


def build():
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_use_double_quant=True)
    tok = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        BASE, quantization_config=bnb, device_map={"": 0}, torch_dtype=torch.bfloat16)
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model.config.use_cache = False
    lora = LoraConfig(r=8, lora_alpha=16, lora_dropout=0.05, bias="none",
                      task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                      "gate_proj", "up_proj", "down_proj"])
    model = get_peft_model(model, lora)
    return model, tok


def bench(model, tok, seq_len: int, steps: int = 6):
    filler = tok("华为 S16700 display vlan 命令回显字段含义。", add_special_tokens=False)["input_ids"]
    blocks = [(filler * (seq_len // max(1, len(filler)) + 1))[:seq_len] for _ in range(16)]
    ds = Dataset.from_dict({"input_ids": blocks, "attention_mask": [[1] * seq_len] * 16})
    targs = TrainingArguments(
        output_dir=OUT, per_device_train_batch_size=1, gradient_accumulation_steps=1,
        max_steps=steps, learning_rate=2e-4, logging_steps=1, save_strategy="no",
        bf16=True, gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        optim="adamw_torch", report_to=[], dataloader_pin_memory=False, seed=1,
    )
    tr = Trainer(model=model, args=targs, train_dataset=ds,
                 data_collator=DataCollatorForLanguageModeling(tokenizer=tok, mlm=False),
                 processing_class=tok)
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    t0 = time.time()
    tr.train()
    dt = time.time() - t0
    peak = torch.cuda.max_memory_allocated() / 2 ** 20
    return dt / steps, peak, steps * seq_len / dt


def main() -> int:
    print("GPU:", torch.cuda.get_device_name(0),
          "%.0f MB" % (torch.cuda.get_device_properties(0).total_memory / 2 ** 20))
    print("分配器:", os.environ["PYTORCH_CUDA_ALLOC_CONF"])
    t0 = time.time()
    model, tok = build()
    print("模型加载 %.1fs，已分配 %.0f MB" % (time.time() - t0, torch.cuda.memory_allocated() / 2 ** 20))
    for seq in (int(x) for x in sys.argv[1:] or ["512", "1024"]):
        s_it, peak, tps = bench(model, tok, seq)
        print("seq=%-5d  %.2f s/it   峰值 %.0f MB   %.0f token/s" % (seq, s_it, peak, tps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
