# -*- coding: utf-8 -*-
r"""检索层（RAG 第 3 步）：query → 向量 → top-k 片段 → 注入提示词 → 本地模型回答。

为什么用 numpy 而不是 FAISS：
    当前规模（三厂商 ~19 万片段，四厂商估计 ~30-40 万）下，一次查询就是
    一次 40万×1024 的点积 ≈ 0.4 GFLOP，CPU 上几十毫秒；而 FAISS 会引入新依赖、
    还要处理索引与元数据一致性。**先用 numpy，量级涨到千万再换**（换的时候
    接口不用动，只换 search 内部）。

关键实现点：
  · 向量库是 **float16 + 已归一化** ⇒ 相似度就是点积，无需再除模长
  · 分块（5 万行）转 float32 再点积，避免把整个库展开成 float32 撑爆内存
  · **只搜已嵌入的部分**（meta.done）：嵌入跑一半时也能用，不会读到全零行
  · 出处三件套（文件 / 小节 / 原始出处）从切片 jsonl 原样带出，供回答标注来源

用法：
    python retrieve.py --vendor h3c --query "H3C 怎么配置基本 ACL" --topk 3
    python retrieve.py --vendor h3c --query "..." --context      # 打印注入用的 context
    python retrieve.py --vendor h3c --query "..." --ask          # 端到端（自动起服务）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402  （同目录；路径的唯一来源见 paths.py）

INDEX_DIR = paths.INDEX
CHAT_PORT = 8080
API_KEY = "local-llm"
import embed_chunks as E  # noqa: E402  复用嵌入与归一化（一份实现，别抄两遍）

_cache = {}


def load_index(vendor):
    """加载向量库与元数据（进程内缓存）。"""
    if vendor in _cache:
        return _cache[vendor]
    import numpy as np
    meta_path = os.path.join(INDEX_DIR, f"{vendor}_emb.meta.json")
    vec_path = os.path.join(INDEX_DIR, f"{vendor}_emb.f16")
    if not os.path.exists(vec_path):
        raise SystemExit(f"!! 向量库不存在: {vec_path}（先跑 embed_chunks.py --vendor {vendor}）")
    meta = json.load(open(meta_path, encoding="utf-8"))
    mm = np.lib.format.open_memmap(vec_path, mode="r", dtype=np.float16,
                                   shape=(meta["n"], meta["dim"]))
    _cache[vendor] = (mm, meta)
    return mm, meta


def _load_chunks(vendor, wanted):
    """按需取切片元数据（只读需要的行，避免整文件解析）。"""
    path = os.path.join(INDEX_DIR, f"{vendor}_chunks.jsonl")
    out = {}
    want = set(wanted)
    with open(path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i in want:
                try:
                    out[i] = json.loads(line)
                except ValueError:
                    pass
                if len(out) == len(want):
                    break
    return out


def search(vendor, query, topk=5):
    import numpy as np
    mm, meta = load_index(vendor)
    done = int(meta.get("done") or meta["n"])
    # ⚠ 冷启动必做：交付机上没有人在跑 :8081 嵌入服务，而 query 必须先嵌入。
    # 不 ensure 就是 URLError，且 core/local_rag.search() 会把它当成"没检索到"、
    # **静默**退回词法检索 —— 症状是"应用能用但答得不准"，现场毫无线索。
    # 该函数幂等：已在跑就只多做一次 /health（毫秒级）；不在跑才拉起 bge-m3。
    E.ensure_embed_server()
    q = E.normalize(E.embed_batch([query])[0])
    qa = np.asarray(q, dtype=np.float32)

    best = []
    step = 50000
    for start in range(0, done, step):
        block = np.asarray(mm[start:min(start + step, done)], dtype=np.float32)
        sims = block @ qa
        k = min(topk, sims.shape[0])
        idx = np.argpartition(-sims, k - 1)[:k] if k > 1 else np.array([0])
        best.extend((float(sims[i]), start + int(i)) for i in idx)
    best.sort(key=lambda t: -t[0])
    best = best[:topk]

    chunks = _load_chunks(vendor, [i for _s, i in best])
    hits = []
    for score, i in best:
        ch = chunks.get(i)
        if not ch:
            continue
        hits.append({"score": score, "file": ch.get("file", ""), "section": ch.get("section", ""),
                     "source": ch.get("source", ""), "text": ch.get("text", "")})
    return hits


def _tokens(q):
    """查询 → 比对用词元（**纯字符串处理，不额外调模型**）：ASCII 词/命令 + 中文 3-gram。"""
    q = (q or "").upper()
    out = set(re.findall(r"[A-Z0-9][A-Z0-9\-\.]{2,}", q))
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", q)
    out |= {cjk[i:i + 3] for i in range(max(0, len(cjk) - 2))}
    return {t for t in out if len(t) >= 3}


def _grams(q):
    """查询里的**相邻二元组**（短语级信号）：单字/单词重叠定位不了答案行，
    "问题点名了功能名 ⇒ 答案行里原样出现"才是真正管用的一条。"""
    q = (q or "").upper()
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", q)
    out = {cjk[i:i + 2] for i in range(max(0, len(cjk) - 1))}
    for phrase in re.findall(r"[A-Z0-9][A-Z0-9\-\.]{2,}(?:\s+[A-Z0-9][A-Z0-9\-\.]{2,})+", q):
        ws = phrase.split()
        out.add(phrase)
        out |= {f"{a} {b}" for a, b in zip(ws[:-1], ws[1:])}
    return {t for t in out if len(t) >= 2}


def pick_window(text, query, per):
    """长切片**不要只注入头部**：按查询在切片里挑一段更相关的窗口，返回 (窗口, 起点)。

    为什么必须这么做（实测）：Cisco 切片中位长度 4173 字，而注入预算只有 800 字 ——
    答案常落在第 700 字之后，`text[:800]` 永远不含答案 ⇒ 该厂 RAG 恒答不出、
    题集闸门也把 Cisco/Juniper 整厂排除在外。**坏的不是检索（整片向量相似度没问题），
    而是"注入哪一段"**。窗口按查询词元命中数挑选，选不到就退回头部（行为不变）。
    """
    if len(text) <= per:
        return text, 0
    toks, grams = _tokens(query), _grams(query)
    if not toks and not grams:
        return text[:per], 0
    up = text.upper()
    # **罕见词元加权**：只数"命中几个词"会让只含常见词的窗口赢过真正含答案的窗口
    # （实测 400 条长切片里 138 条如此）。权重 1/(1+出现次数)：出现 1 次的词值 0.5，
    # 出现 20 次的词只值 0.05 —— 定位答案靠的正是前者。
    w1 = {t: 1.0 / (1 + up.count(t)) for t in toks}
    w2 = {g: 3.0 / (1 + up.count(g)) for g in grams}          # 二元组是更强的信号
    step = max(64, per // 4)
    # 候选起点**本身就是换行对齐的**：先前是"选完再回退到换行"，那会把窗口尾部的答案
    # 挤出窗口（打分的内容 ≠ 实际注入的内容）。现在候选即最终内容。
    cands = set(range(0, len(text) - per + 1, step))
    i = text.find("\n")
    while 0 <= i < len(text) - per and len(cands) < 600:
        cands.add(i + 1)
        i = text.find("\n", i + 1)
    best_s, best_score = 0, -1.0
    for s in cands:
        w = up[s:s + per]
        score = sum(v for t, v in w1.items() if t in w) + sum(v for g, v in w2.items() if g in w)
        if score > best_score:
            best_s, best_score = s, score
    return text[best_s:best_s + per], best_s


def build_context(hits, per=800, total=1200, query=""):
    """按提示词 §2 的注入模板拼 context，并执行字数预算（不给模型塞长文）。

    `query` 非空且切片长于 `per` 时，注入的是**相关的窗口**而非头部（见 pick_window）。
    """
    parts, used = [], 0
    seen = set()
    for n, h in enumerate(hits, 1):
        key = (h["file"], h["section"])
        if key in seen:
            continue
        seen.add(key)
        body, _off = pick_window(h["text"], query, per)
        block = (f"[资料 {n}] 文件：{h['file']} · 小节：{h['section']}\n"
                 f"原始出处：{h['source']}\n{body}")
        if used + len(block) > total and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts) if parts else "（无可用的厂商资料）"


def chat_up():
    try:
        return '"ok"' in E._get(f"http://127.0.0.1:{CHAT_PORT}/health", timeout=3)
    except Exception:  # noqa: BLE001
        return False


def ensure_chat_server():
    """对话服务不在就起一个（4B 档，关思考）。"""
    if chat_up():
        return
    print("对话服务未在跑 → 自动起（run_server.py）", flush=True)
    # 经 paths.script_cmd：打包态下 sys.executable 是 NetOpsTerminal.exe 本身，直接拼会
    # "再开一个应用"而不是起服务（见 core/app_paths.script_cmd）。
    subprocess.run(paths.script_cmd(os.path.join(HERE, "run_server.py")),
                   capture_output=True, text=True, timeout=600)
    if not chat_up():
        raise SystemExit("!! 对话服务起不来，看 local_llm\\_llama_server.log")


def ask(vendor, query, topk=3, max_tokens=384):
    ensure_chat_server()
    hits = search(vendor, query, topk)
    context = build_context(hits, query=query)
    prompt = open(os.path.join(HERE, "prompt_local_assistant.txt"), encoding="utf-8").read()
    sys_prompt = prompt.format(context=context, vendor=vendor, model="未知")
    data = json.dumps({"model": "local", "messages": [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": query}],
        "max_tokens": max_tokens, "temperature": 0.2, "stream": False},
        ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{CHAT_PORT}/v1/chat/completions",
                                data=data, method="POST", headers={
                                    "Content-Type": "application/json; charset=utf-8",
                                    "Authorization": f"Bearer {API_KEY}"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        res = json.loads(r.read().decode("utf-8", "replace"))
    dt = time.time() - t0
    msg = (res.get("choices") or [{}])[0].get("message") or {}
    return {"answer": (msg.get("content") or "").strip(),
            "reasoning": (msg.get("reasoning_content") or "").strip(),
            "hits": hits, "context": context, "elapsed": dt,
            "usage": res.get("usage") or {}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", default="h3c")
    ap.add_argument("--query", required=True)
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--context", action="store_true")
    ap.add_argument("--ask", action="store_true")
    args = ap.parse_args()

    if args.ask:
        r = ask(args.vendor, args.query, args.topk)
        print(f"=== 检索 top-{args.topk} ===")
        for n, h in enumerate(r["hits"], 1):
            print(f"  [{n}] {h['score']:.4f}  {h['file']} · {h['section'][:40]}")
        print(f"\n=== 回答（{r['elapsed']:.1f}s, {r['usage']}）===")
        print(r["answer"])
        if r["reasoning"]:
            print(f"\n[!] 出现思考过程 {len(r['reasoning'])} 字（应关思考）")
        return 0

    hits = search(args.vendor, args.query, args.topk)
    print(f"=== {args.vendor} top-{args.topk}（query: {args.query}）===")
    for n, h in enumerate(hits, 1):
        print(f"[{n}] score={h['score']:.4f}  {h['file']} · {h['section']}")
        print(f"    出处: {h['source'][:90] or '(无)'}")
        print(f"    {h['text'][:150].replace(chr(10), ' ')}")
    if args.context:
        print("\n=== 注入用 context ===")
        print(build_context(hits, query=args.query))
    return 0


if __name__ == "__main__":
    sys.exit(main())
