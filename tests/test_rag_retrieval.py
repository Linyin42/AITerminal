# -*- coding: utf-8 -*-
"""验证 P1 的检索判据（原文）：
"拿 10 个真实问题跑 bench_local.py 风格的验收，出处必须指向真实 `文件 · 小节`"。

只测**检索层**（query → 向量 → top-k → 出处），不调 LLM：毫秒级、不占显存，
能把"检索质量"单独判出来（回答质量差时，先要能分清是检索错还是模型错）。

⚠ 需要 :8081 嵌入服务在跑（查 query 要先嵌入）。h3c 的嵌入任务通常正在跑；
   没跑则本脚本自己拉起，**只停自己拉起的那个**。
⚠ 只搜**已嵌入**的部分（meta.done）—— 嵌入跑到一半也能用，这是设计。

跑法：python tests\\test_rag_retrieval.py [--vendor h3c]
"""
import argparse
import json
import os
import sys
import urllib.request

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
sys.path.insert(0, os.path.join(P, "local_llm"))
os.chdir(P)

import retrieve as R                                    # noqa: E402

PASS, FAIL = [], []

#: 10 个真实问题（都是厂商文档里必然有的题材）。⚠ 这是 P1 的原始判据，别删别改。
QUESTIONS = [
    "H3C 怎么配置基本 ACL",
    "H3C 交换机怎么创建 VLAN",
    "H3C 如何配置端口镜像",
    "H3C 查看接口状态的命令",
    "H3C 怎么配置链路聚合",
    "H3C 配置静态路由",
    "H3C 如何保存配置",
    "H3C 配置 SSH 登录",
    "H3C 怎么查看 MAC 地址表",
    "H3C 配置 DHCP 服务",
]

#: 四家的题集：h3c 用上面那 10 个；其余各家与 h3c **同题材**（便于横向比较），
#: 但题干里的厂商名换成它自己 —— 拿"H3C 怎么配 ACL"去搜锐捷语料，命中差是
#: 提问的错、不是检索的错，会把判据用成误判。
QUESTIONS_BY_VENDOR = {
    "h3c": QUESTIONS,
    "ruijie": [
        "锐捷 怎么配置基本 ACL",
        "锐捷 交换机怎么创建 VLAN",
        "锐捷 如何配置端口镜像",
        "锐捷 查看接口状态的命令",
        "锐捷 怎么配置链路聚合",
        "锐捷 配置静态路由",
        "锐捷 如何保存配置",
        "锐捷 配置 SSH 登录",
        "锐捷 怎么查看 MAC 地址表",
        "锐捷 配置 DHCP 服务",
    ],
    "tplink": [
        "TP-LINK 怎么配置基本 ACL",
        "TP-LINK 交换机怎么创建 VLAN",
        "TP-LINK 如何配置端口镜像",
        "TP-LINK 查看接口状态的命令",
        "TP-LINK 怎么配置链路聚合",
    ],
    "huawei": [
        "华为 怎么配置基本 ACL",
        "华为 交换机怎么创建 VLAN",
        "华为 如何配置端口镜像",
        "华为 查看接口状态的命令",
        "华为 怎么配置链路聚合",
    ],
}
#: 反例：与厂商文档无关的问题，其最高相似度应明显更低（否则"检索"其实没在起作用）
OFF_TOPIC = ["红烧肉怎么做才好吃", "今天北京天气怎么样"]


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def embed_up():
    try:
        req = urllib.request.Request("http://127.0.0.1:8081/health",
                                     headers={"Authorization": "Bearer local-llm"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return b"ok" in r.read()
    except Exception:  # noqa: BLE001
        return False


def load_provenance(vendor):
    """切片 jsonl 里所有 (file, section) 出处 —— 用来验证命中不是编出来的。"""
    pairs, n = set(), 0
    path = os.path.join(R.INDEX_DIR, f"{vendor}_chunks.jsonl")
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            n += 1
            try:
                o = json.loads(line)
            except ValueError:
                continue
            pairs.add((o.get("file", ""), o.get("section", "")))
    return pairs, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", default="h3c")
    args = ap.parse_args()
    vendor = args.vendor
    qs = QUESTIONS_BY_VENDOR.get(vendor, QUESTIONS)

    print(f"=== P1 检索判据（vendor={vendor}，{len(qs)} 问）===")
    started = False
    if not embed_up():
        started = True
        print("       嵌入服务没在跑 → 拉起（CPU/GPU 由 embed_chunks 自决）")
        import embed_chunks as E
        E.ensure_embed_server()
    check("嵌入服务就绪(:8081/health → ok)", embed_up())

    mm, meta = R.load_index(vendor)
    done, n = int(meta.get("done") or meta["n"]), int(meta["n"])
    print(f"       向量库 {done}/{n} 已嵌入（dim={meta['dim']}）")

    pairs, total = load_provenance(vendor)
    print(f"       切片文件 {total} 行、{len(pairs)} 个不同「文件·小节」出处")

    scores_on, bad_prov, empty = [], [], []
    print(f"\n--- {len(qs)} 个真实问题的 top-1 出处 ---")
    for q in qs:
        hits = R.search(vendor, q, topk=3)
        if not hits:
            empty.append(q)
            continue
        check_desc = all(hits[i]["score"] >= hits[i + 1]["score"] for i in range(len(hits) - 1))
        if not check_desc:
            bad_prov.append((q, "分数不降序"))
        scores_on.append(hits[0]["score"])
        h = hits[0]
        print(f"   {h['score']:.4f}  {q}\n            → {h['file']} · {h['section']}"
              f"  [原出处 {os.path.basename(h.get('source') or '无')}]")
        for hit in hits:
            if not (hit.get("file"), hit.get("section")) in pairs:
                bad_prov.append((q, f"{hit.get('file')} · {hit.get('section')}"))
            if not (hit.get("text") or "").strip():
                bad_prov.append((q, "命中片段正文为空"))

    check(f"{len(qs)} 个问题都检索到了结果", not empty,
          f"空结果 {empty}" if empty else f"{len(qs)}/{len(qs)}")
    check("所有命中的出处都真实存在于切片文件（不是编的）", not bad_prov,
          str(bad_prov[:3]) if bad_prov else f"共核验 {len(qs) * 3} 条命中")

    hits = R.search(vendor, qs[0], topk=3)
    ctx = R.build_context(hits)
    print(f"\n       注入 context 长度 = {len(ctx)} 字")
    check("注入 context 遵守字数预算（≤1200 + 少量框架文字）", len(ctx) <= 1200 + 60, f"{len(ctx)} 字")
    # ⚠ 格式以 retrieve.build_context 为准 = `[资料 N] 文件：… · 小节：…`（ASCII 方括号），
    # 它填进 prompt_local_assistant.txt 的 `【厂商资料】\n{context}`。别按应用侧 ai.ask 的
    # `【资料N】` 去断言 —— 那是另一条路径自己拼的（两处格式不同这件事本身另有问题）。
    check("注入 context 带出处标签「[资料 N]」", "[资料 1]" in ctx, ctx[:40].replace("\n", "|"))
    check("注入 context 带文件名与小节（模型才能标注来源）",
          "文件：" in ctx and "小节：" in ctx, ctx[:60].replace("\n", "|"))

    print("\n--- 反例（与厂商文档无关的问题）---")
    scores_off = []
    for q in OFF_TOPIC:
        h = R.search(vendor, q, topk=1)
        s = h[0]["score"] if h else 0.0
        scores_off.append(s)
        print(f"   {s:.4f}  {q}")
    avg_on = sum(scores_on) / len(scores_on) if scores_on else 0
    avg_off = sum(scores_off) / len(scores_off) if scores_off else 0
    print(f"       均值：相关 {avg_on:.4f} vs 无关 {avg_off:.4f}")
    check("无关问题的相似度明显低于相关问题（检索确实在区分）", avg_off < avg_on,
          f"{avg_off:.4f} < {avg_on:.4f}")
    print("       参考：若要按分数阈值决定「是否注入资料」，这两组均值之间的距离"
          f"（{avg_on - avg_off:.4f}）就是可用区间")

    if started:
        print("\n       本脚本拉起的嵌入服务**保留**（可能还有嵌入任务在跑，不动它）")

    print(f"\n=== 合计 PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    for name in FAIL:
        print(f"   FAIL: {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
