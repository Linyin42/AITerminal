# -*- coding: utf-8 -*-
"""固定点测试：AI 能不能「用自己的话」解释技术点 / 给出方案。

两类用例（`tests/ai_explain_cases.jsonl`）：
  A 疑问  —— 使用者对某个技术点有疑惑，问 AI「到底是怎么回事 / 卡在哪」。
             要点：讲机制、讲因果，不是把手册条目抄一遍；别背术语堆砌。
  B 方案  —— 使用者提出想法，要 AI 给能落地的方案 + 解释。
             要点：先确认目标与约束 → 分步 → 命令 → 风险/回滚/验证。

判分（全自动，不主观打分）：
  1. 事实命中：facts 里的词必须出现；any_facts 里命中 ≥ 2 个（同义/相关事实）。
  2. **自己的话**：答案与检索片段的最长公共子串 < 40 字，且 8-gram 重合率 < 25%；
     出现手册痕迹（「命令模式」「缺省级别」「使用指导」「【命令格式】」）直接判抄。
  3. 结构：疑问类要有解释性连接词（因为/所以/本质/取决于/如果）；方案类要有
     ≥3 个步骤、≥1 条命令、≥1 处风险或回滚提示。
  4. 长度：120~1500 字（太短=没解释，太长=堆资料）。

跑法：
    python tests\\ai_explain_test.py --stub            # 自检判分器（不调模型）
    python tests\\ai_explain_test.py --provider local  # 真跑本地模型（需要 :8080 起着）
    python tests\\ai_explain_test.py --provider deepseek --out docs\\AI_EXPLAIN_TEST_xxx.md
"""
import argparse
import io
import json
import os
import re
import sys
import time

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)

CASES = os.path.join(P, "tests", "ai_explain_cases.jsonl")

MANUAL_SMELL = ("命令模式", "缺省级别", "使用指导", "【命令格式】", "【参数说明】", "本章节", "如表 1", "见表 ")
STEP_RE = re.compile(r"^\s*(?:第\s*[一二三四五六七八九十\d]+\s*步|[一二三四五六七八九十]、|\d+[.、),）]|[（(]\d+[)）])\s*\S", re.M)
CMD_RE = re.compile(r"^\s*(?:show|display|interface|vlan|ip |undo |no |sysname|system-view|port |switchport|route|acl|nat)", re.M)
RISK_RE = re.compile(r"风险|注意|回滚|验证|影响|避免|谨慎|前提")
EXPLAIN_RE = re.compile(r"因为|所以|本质|取决于|如果|相当于|意味着|原因是|之所以")
JUDGE_MIN_CHARS = 120
JUDGE_MAX_CHARS = 1500


def load_cases(path):
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def longest_common_substring(a, b):
    """返回最长公共连续子串长度（a、b 都可能几 KB，用滚动数组）。"""
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    prev = [0] * (len(a) + 1)
    best = 0
    for ch in b:
        cur = [0] * (len(a) + 1)
        for i, ca in enumerate(a, 1):
            if ca == ch:
                cur[i] = prev[i - 1] + 1
                if cur[i] > best:
                    best = cur[i]
        prev = cur
    return best


def ngram_overlap(answer, refs, n=8):
    """答案里有多少比例的 n-gram 能在检索片段里找到（照抄程度）。"""
    def grams(s):
        s = re.sub(r"\s+", "", s)
        return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}
    ga = grams(answer)
    if not ga:
        return 0.0
    gr = set()
    for r in refs:
        gr |= grams(r)
    return len(ga & gr) / len(ga)


def judge(case, answer, refs, commands):
    """返回 (是否通过, 明细 dict)。"""
    answer = (answer or "").strip()
    cmds = commands or []
    chars = len(re.sub(r"\s+", "", answer))
    detail = {"chars": chars}

    # 1 事实
    missing = [f for f in case.get("facts") or [] if f.lower() not in answer.lower()]
    any_hits = [f for f in case.get("any_facts") or [] if f.lower() in answer.lower()]
    detail["facts_missing"] = missing
    detail["any_hits"] = len(any_hits)
    fact_ok = not missing and len(any_hits) >= 2

    # 2 自己的话
    smell = [s for s in MANUAL_SMELL if s in answer]
    lcs = max([longest_common_substring(answer, r) for r in refs] or [0])
    overlap = ngram_overlap(answer, refs)
    detail.update({"smell": smell, "lcs": lcs, "overlap": round(overlap, 3)})
    own_words_ok = not smell and lcs < 40 and overlap < 0.25

    # 3 结构
    steps = len(STEP_RE.findall(answer))
    n_cmd = len(CMD_RE.findall(answer)) + len(cmds)
    risk = bool(RISK_RE.search(answer))
    explain = bool(EXPLAIN_RE.search(answer))
    detail.update({"steps": steps, "commands": n_cmd, "risk": risk, "explain": explain})
    if case.get("group") == "方案":
        struct_ok = steps >= 3 and n_cmd >= 1 and risk
    else:
        struct_ok = explain and n_cmd <= 3      # 疑问类不该甩一堆命令

    # 4 长度
    len_ok = JUDGE_MIN_CHARS <= chars <= JUDGE_MAX_CHARS
    detail["len_ok"] = len_ok

    ok = fact_ok and own_words_ok and struct_ok and len_ok
    detail["fact_ok"] = fact_ok
    detail["own_words_ok"] = own_words_ok
    detail["struct_ok"] = struct_ok
    return ok, detail


def ask_engine(case, provider, vendor_override="", timeout=180):
    """真跑：走 EngineService.m_ai_ask（本地走向量 RAG + 专用提示词）。"""
    from engine.service import EngineService
    svc = EngineService()
    vendor = vendor_override or case.get("vendor") or ""
    t0 = time.time()
    res = svc.m_ai_ask({
        "question": case["question"],
        "provider": provider,
        "vendor": vendor,
        "useKnowledge": True,
    })
    ms = int((time.time() - t0) * 1000)
    answer = (res or {}).get("answer") or ""
    sources = (res or {}).get("sources") or []
    commands = [ln.strip() for ln in answer.splitlines() if CMD_RE.match(ln)]
    refs = []
    try:
        from core import local_rag
        hits = local_rag.search(case["question"], topk=4, vendor=vendor) or []
        refs = [h.get("text") or "" for h in hits]
    except Exception:                        # noqa: BLE001  检索不可用就用不到"照抄"判据
        refs = []
    return answer, commands, refs, sources, ms


def stub_answer(case):
    """自检用：造一份"合格"和一份"照抄手册"的假答案，验证判分器两个方向都灵。

    ⚠ 这里不是测模型，是测**判分器**：合格答案必须同时满足四类判据（事实/自述/结构/长度），
    所以造假答案时要把 facts 全部塞进去、any_facts 至少两个、并补足解释性连接词。
    """
    facts = list(case.get("facts") or [])
    anyf = list(case.get("any_facts") or [])[:3]
    f0 = facts[0] if facts else ""
    f1 = facts[1] if len(facts) > 1 else f0
    extra = "、".join(anyf)
    if case.get("group") == "方案":
        good = (
            "先说结论：这个想法可行，但要先把前提讲清楚。因为 %s 与 %s 决定了转发路径和故障域，"
            "所以第一步不是敲命令，而是把现状、地址规划和窗口定下来；%s 这几个点会直接影响方案取舍。\n\n"
            "1. 第一步：梳理现状——现有设备型号、端口占用、地址段、出口带宽，确认改动范围。\n"
            "2. 第二步：按区域划分 VLAN 与网段，先规划网关和互联地址，避免后面冲突。\n"
            "3. 第三步：配置互联链路与安全策略，再写默认路由/出口策略。\n"
            "4. 第四步：分批上线，每批只动一个区域，动完立刻验证。\n\n"
            "风险与回滚：变更会造成短时中断，必须在窗口内做，先备份配置；"
            "上线后验证连通性、路由与日志，异常就回滚到变更前配置。\n\n"
            "```bash\nsystem-view\nvlan 10\ndescription office\nquit\n```\n"
            % (f0, f1, extra)
        )
        bad = "命令模式：全局配置模式。缺省级别：14。使用指导：无。【命令格式】%s。如表 1 所示。" % f0
    else:
        good = (
            "本质上是两个因素一起决定的：%s 和 %s。因为设备之间只靠报文里携带的信息比较，"
            "所以谁的 %s 更优谁就当老大；优先级相同才去比 MAC。如果两边优先级都没改过，"
            "结果就由出厂默认值和 MAC 大小决定，这就是你看到「两台都觉得自己是根」的原因。"
            "相当于一场投票：大家都报自己的选票，没有人举手复议就没有 PK 环节。"
            "%s 这些细节会影响选举结果，排查时先确认优先级和 MAC，再看拓扑是否稳定。"
            % (f0, f1, f0, extra)
        )
        bad = "命令模式：特权模式。缺省级别：14。【命令格式】show %s。【参数说明】无。使用指导：无。" % f0
    return good, bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub", action="store_true", help="自检判分器，不调模型")
    ap.add_argument("--provider", default="local", choices=["local", "deepseek", "stub"])
    ap.add_argument("--vendor", default="", help="强制某厂商检索域（默认按用例）")
    ap.add_argument("--only", default="", help="只跑某个 id 前缀，如 a 或 b1")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    cases = load_cases(CASES)
    if a.only:
        cases = [c for c in cases if c["id"].startswith(a.only)]
    print("用例 %d 条（%s）" % (len(cases), "自检" if a.stub else "真跑 " + a.provider))

    rows, n_pass = [], 0
    for c in cases:
        if a.stub:
            good, bad = stub_answer(c)
            ok_good, det_good = judge(c, good, [], [])
            ok_bad, det_bad = judge(c, bad, [], [])
            good_ok = ok_good and not ok_bad        # 好的要过、照抄的要拦
            n_pass += 1 if good_ok else 0
            print("  [%s] %s 判分器自检  真答案判=%s 抄答案判=%s"
                  % ("PASS" if good_ok else "FAIL", c["id"], ok_good, ok_bad))
            rows.append((c, good, det_good, ok_good, det_bad, ok_bad))
            continue
        try:
            answer, commands, refs, sources, ms = ask_engine(c, a.provider, a.vendor)
        except Exception as exc:                 # noqa: BLE001
            print("  [FAIL] %s 调用失败: %s: %s" % (c["id"], type(exc).__name__, exc))
            rows.append((c, "", {"error": str(exc)}, False, {}, False))
            continue
        ok, det = judge(c, answer, refs, commands)
        n_pass += 1 if ok else 0
        print("  [%s] %-3s %4d字 事实%s 自述%s 结构%s 出处%d  %dms"
              % ("PASS" if ok else "FAIL", c["id"], det.get("chars", 0),
                 "✓" if det.get("fact_ok") else "✗",
                 "✓" if det.get("own_words_ok") else "✗",
                 "✓" if det.get("struct_ok") else "✗",
                 len(sources), ms))
        rows.append((c, answer, det, ok, {}, False))

    print("\n=== 合计 PASS %d / %d ===" % (n_pass, len(cases)))

    if a.out:
        lines = ["# AI「用自己的话解释」固定点测试", "",
                 "- 日期：%s" % time.strftime("%Y-%m-%d %H:%M"),
                 "- 后端：%s" % ("自检（桩）" if a.stub else a.provider),
                 "- 用例：%d 条（A 疑问 / B 方案，来自 `tests/ai_explain_cases.jsonl`）" % len(cases),
                 "- 判分：事实命中 + 最长公共子串<40 + 8-gram 重合<25% + 结构 + 长度", "",
                 "| 用例 | 类型 | 结果 | 字数 | 事实 | 自己的话 | 结构 | LCS | 8-gram |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for c, ans, det, ok, _d2, _o2 in rows:
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                c["id"], c.get("group", ""), "PASS" if ok else "FAIL",
                det.get("chars", "-"),
                "✓" if det.get("fact_ok") else "✗",
                "✓" if det.get("own_words_ok") else "✗",
                "✓" if det.get("struct_ok") else "✗",
                det.get("lcs", "-"), det.get("overlap", "-")))
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("报告:", a.out)
    return 0 if n_pass == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
