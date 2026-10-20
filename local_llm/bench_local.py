# -*- coding: utf-8 -*-
r"""本地模型基准 + 提示词验收（一次跑出 tok/s 与格式遵守情况）。

做两件事：
  1) **速度**：空系统提示下问一句，量真实生成速度（tok/s）——
     这是选型（4B vs 8B、全上卡 vs 部分卸载）唯一缺的实测数字。
  2) **提示词验收**：用 system_prompt.md §1 的正式提示词 + 从 h3c_chunks.jsonl
     真实取出的一片资料（§2 注入模板），问一个资料里**确实有**的问题，检查：
       · 是否给出处（对应 system_prompt.md §6 第 1 条）
       · 出处是否指向真实存在的 文件·小节
       · 命令是否落在 ```bash 代码块里（可被 ai_agent.parse_command_lines 解析）
       · 是否出现 DONE / ASK 或思考过程（格式禁令）

用法:
    python bench_local.py
    python bench_local.py --max-tokens 256 --no-acceptance
"""
import argparse
import json
import os
import re
import time
import urllib.request

PORT = 8080
API_KEY = "local-llm"
URL = f"http://127.0.0.1:{PORT}/v1/chat/completions"
import paths  # noqa: E402  （同目录；路径的唯一来源见 paths.py）
CHUNKS = os.path.join(paths.INDEX, "h3c_chunks.jsonl")
PROMPT_MD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.md")

#: 正式提示词的**唯一权威文件**。原先脚本里内嵌了一份副本，违反"一份数据一个来源"——
#: 改提示词就得改两处，必然走样。现在只从这里读。
PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "prompt_local_assistant.txt")


def load_prompt():
    with open(PROMPT_FILE, "r", encoding="utf-8") as fh:
        return fh.read()


def post(payload, timeout=300):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(URL, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {API_KEY}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def chat(system, user, max_tokens=256, temperature=0.2):
    t0 = time.time()
    res = post({"model": "local", "messages": (
        ([{"role": "system", "content": system}] if system else [])
        + [{"role": "user", "content": user}]
    ), "max_tokens": max_tokens, "temperature": temperature, "stream": False})
    dt = time.time() - t0
    usage = res.get("usage") or {}
    msg = ((res.get("choices") or [{}])[0].get("message") or {})
    text = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    return {"text": text, "reasoning": reasoning, "elapsed": dt,
            "ptok": usage.get("prompt_tokens"), "ctok": usage.get("completion_tokens"),
            "raw_finish": ((res.get("choices") or [{}])[0]).get("finish_reason")}


def find_chunk(needle):
    """从真实切片文件里取一片资料（不造假数据）。"""
    if not os.path.exists(CHUNKS):
        return None
    with open(CHUNKS, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if needle in (obj.get("section") or "") or needle in (obj.get("text") or ""):
                return obj
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--no-acceptance", action="store_true")
    args = ap.parse_args()

    # ---------- 1) 速度 ----------
    print("=== 1) 速度基准（无系统提示，max_tokens=%d）===" % args.max_tokens, flush=True)
    r = chat("", "用一句话说明 VLAN 的作用。", max_tokens=args.max_tokens)
    c = r["ctok"] or 0
    speed = (c / r["elapsed"]) if r["elapsed"] > 0 else 0
    print(f"  耗时 {r['elapsed']:.2f}s  prompt_tok={r['ptok']}  completion_tok={c}")
    print(f"  ★ 生成速度 ≈ {speed:.1f} tok/s   finish={r['raw_finish']}")
    print(f"  答复：{r['text'][:160]}")
    if r["reasoning"]:
        print(f"  [!] 出现思考过程（reasoning_content {len(r['reasoning'])} 字）——需关思考！")

    if args.no_acceptance:
        return 0

    # ---------- 2) 提示词验收 ----------
    print("\n=== 2) 提示词验收（真实资料注入）===", flush=True)
    ch = find_chunk("配置ACL")
    if not ch:
        print("  ! 取不到资料片段，跳过验收")
        return 1
    context = (f"[资料 1] 文件：{ch['file']} · 小节：{ch['section']}\n"
               f"原始出处：{ch['source']}\n{ch['text'][:800]}")
    print(f"  注入资料：{ch['file']} · {ch['section']}（{len(ch['text'])} 字，截 800）")
    sys_prompt = load_prompt().format(context=context, vendor="h3c", model="未知")

    q = "H3C 上怎么配置 IPv4 基本 ACL？"
    print(f"  提问：{q}")
    r2 = chat(sys_prompt, q, max_tokens=args.max_tokens)
    txt = r2["text"]
    print(f"  --- 答复（{r2['elapsed']:.2f}s，{r2['ctok']} tok）---")
    print(txt)

    # ---- 自动判定（对应 system_prompt.md §6）----
    src_hits = re.findall(r"\[?来源[：:]\s*([^\]]+)\]?", txt)
    has_cite = bool(src_hits)
    real = bool(ch["file"].split("/")[-1].replace(".md", "") in txt or ch["section"][:6] in txt)
    bash = re.findall(r"```(?:bash|sh)?\s*\n(.*?)```", txt, re.S)
    has_bash = bool(bash)
    forbidden = [w for w in ("DONE", "ASK") if re.search(rf"\b{w}\b", txt)]
    print("\n  --- 自动判定 ---")
    print(f"  [{'PASS' if has_cite else 'FAIL'}] 带出处？{'提及: ' + str(src_hits[:2]) if has_cite else '未发现'}")
    print(f"  [{'PASS' if real else 'FAIL'}] 出处指向真实 文件/小节？")
    print(f"  [{'PASS' if has_bash else 'WARN'}] 命令在 ```bash 代码块内（{len(bash)} 块）")
    if bash:
        print("      命令：" + " | ".join(bash[0].strip().splitlines()[:6]))
    print(f"  [{'FAIL' if forbidden else 'PASS'}] 未出现 DONE/ASK：{forbidden or '无'}")
    print(f"  [{'FAIL' if r2['reasoning'] else 'PASS'}] 未输出思考过程")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
