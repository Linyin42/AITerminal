#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建**可自动判分、且逐题验证过"答案就在注入上下文里"**的评测题集 —— v2。

v1 的失败（必须记住，否则会重犯）
================================
v1 拿"事实前 32 字原文"当问点，实测产出的题面**检索源切片召回 0/12**：
「执行命令」「通过执行命令」「在系统视图下执行命令」这类无意义问点，检索只能返回字面像的
东西（`execute` 那篇文档），于是 base+rag 恒 0% —— 量到的是"题面坏掉了"，不是模型能力。
同一批题、同一索引的对照实验：题面原文 0/12、section+事实 11/12、源句 9/12
⇒ 索引/嵌入服务/检索全部正常，瓶颈 100% 在题面。

v2 的两条硬规则
==============
1. 问点 = **清洗过的意图小句**（按标点切小句 → 逐个小句剥离悬空连接词/啰嗦后缀 → 取最后一个
   仍成立的小句），而不是任意截断的原文；清洗后不足 6 字（如"执行命令"）**丢弃该候选**。
2. **逐题过检索闸门**：用最终题面 `retrieve.search(vendor, q, topk)` 检索，再用
   `retrieve.build_context(hits, per=800, total=1200, query=q)` 真正拼出模型会看到的上下文，
   要求**全部关键事实都在拼好的上下文里**；不过闸门的题不进题集。
   ⚠ `per=800/total=1200` 的后果是**实际只注入 top-1 那 800 字**（第二块会超预算被 break），
   所以闸门是很严的 —— 正因如此，"base+rag 应该答得出"成了题集**保证的前提**，而不是事后的期望。
   ⚠ 长切片注入的是**按题面选中的窗口**（`retrieve.pick_window`），不是片头：Cisco 切片中位
   4173 字，取片头等于答案永远进不了上下文，那会把"没注入"误读成"模型答不出来"。候选筛选
   （`in_window`）与闸门用的是**同一套窗口代码**，两边口径必须一致。

⚠ 口径诚实交代
  · class=common：关键事实出现在训练语料里 ⇒ 量"微调真的学进去了多少"
  · class=longtail：关键事实一次都没出现 ⇒ 量"微调学不到、只能靠 RAG"
  · 命中 ≠ 语义完全正确（可能上下文说反了）；报告里同时给回答长度与"未找到/不确定"占比。

用法：`python local_llm/build_eval_set.py --per-vendor-per-class 5 --topk 3`
输出：`D:\\ait_llm\\train\\eval_set.jsonl` 与 `eval_set.md`
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import re
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import retrieve  # noqa: E402

# ⚠ 不要在模块级改写 sys.stdout：那会让本模块**不可被导入**（旧 wrapper 被 GC 时会把
# 共享的 buffer 关掉，import 方一 print 就 "I/O operation on closed file"）。
# 需要 UTF-8 出口时在 main() 入口显式包一次。

VENDOR_LABEL = {"huawei": "华为", "h3c": "H3C", "ruijie": "锐捷",
                "juniper": "Juniper", "cisco": "Cisco", "tplink": "TP-Link"}

#: (正则, 类别) —— 三类事实各自的问法不同，见 TEMPLATES
FACT_RES = [
    (re.compile(r"\b[A-Z][A-Za-z0-9]{1,}(?:-[A-Z0-9][A-Za-z0-9/\.]{0,12}){1,4}\b"), "part"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:km|Gbps|Mbps|MHz|GHz|dBm|dB|nm|mm|mW|kV|V|mA|W|℃)\b"), "num"),
    (re.compile(r"\b(?:display|show)\s+[a-z][a-z0-9\-]{2,20}(?:\s+[a-z0-9\-]{2,20}){0,2}\b"), "cmd"),
]
#: ⚠ 英文文档里 `display the arguments and` / `show commands may be` 这类**散文**会被
#: cmd 正则当命令收进来（实测 Cisco/Juniper 候选里大量如此），题面毫无意义。整词命中即弃。
#: 只放"命令语法里绝不出现"的虚词/元词，避免误伤 `show ip route`、`show running-config`。
EN_PROSE = {
    "THE", "AND", "OR", "OF", "TO", "FOR", "IS", "ARE", "BE", "MAY", "MUST", "WILL", "WOULD",
    "SHOULD", "THIS", "THAT", "THESE", "THOSE", "WITH", "FROM", "ON", "IN", "AT", "BY", "AS",
    "NOT", "IF", "IT", "ITS", "WHEN", "WHERE", "WHICH", "WHILE", "COMMAND", "COMMANDS",
    "FOLLOWING", "EXAMPLE", "EXAMPLES", "INFORMATION", "ABOUT", "REGARDING", "USES", "USED",
    "USING", "SEE", "ALSO", "NOTE", "YOU", "YOUR", "ARGUMENTS", "KEYWORDS", "SECTION",
}
STOP_FACTS = {
    "IP", "IPV4", "IPV6", "MAC", "VLAN", "CPU", "USB", "LED", "SNMP", "HTTP", "HTTPS", "SSH",
    "TELNET", "FTP", "TFTP", "NTP", "DNS", "DHCP", "TCP", "UDP", "ICMP", "ARP", "MTU", "QOS",
    "ACL", "VPN", "STP", "RSTP", "MSTP", "LACP", "LAG", "POE", "SFP", "SFP+", "QSFP", "QSFP28",
    "GBIC", "RJ45", "IP-ADDRESS", "N/A", "ON/OFF", "YES/NO", "1.0", "2.0", "3.0",
}
TEMPLATES = {
    "cmd":  "{v} 设备上要完成「{label}」，应该执行什么命令？请写出完整命令。",
    "part": "{v} 文档里的「{label}」具体包含哪些型号、部件号或 MIB 名？请逐个列出。",
    "num":  "{v} 文档里的「{label}」对应的具体数值是多少？",
}
#: 拿 section 当问点的退路（只在 section **不泄题** 时用）
SEC_TEMPLATES = {
    "cmd":  "{v} 设备上，文档小节「{label}」给出的完整命令是什么？",
    "part": "{v} 文档小节「{label}」里涉及哪些型号、部件号或 MIB 名？",
    "num":  "{v} 文档小节「{label}」里给出的具体数值是多少？",
}
#: 显式跳过的厂商（**不静默出 0 题**）。
#: cisco/juniper 曾经在这里：它们的中文取题启发式"事实之前的意图小句"在英文技术写作里不成立
#: （命令前那句是上下文而不是"问什么"，修完三处 bug 后题面仍是 `without，native，keyword.` 这种碎片）。
#: 现在这两家走**独立的英文取题路** `pick_qa_en`（参考字段 + Cisco 的 "To …, use the X command"），
#: 从本字典移除；本字典留给以后真的没法取题的厂商。
SKIP_VENDORS: dict[str, str] = {}
#: 默认四厂 = 已有评测（eval_set.jsonl / AIT_REPORT.md）所依据的主集。
#: 加 cisco/juniper 会改变主集构成、让历史结果不可比 ⇒ 英文题另建 `eval_set_en.jsonl`。
DEFAULT_VENDORS = ("huawei", "h3c", "ruijie", "tplink")
#: 英文厂商：文档是**参数参考手册**（条目 = 命令 + Syntax Description / Command Default /
#: Command Modes / Hierarchy Level / Description 字段表），中文那套"事实前的意图小句"不适用
#: （英文学术写作里命令前那句是上下文，不是"问什么"），所以走 `pick_qa_en` 这条独立取题路：
#:   · field：命令 + 参考字段（在哪个模式下执行 / 默认值 / 哪个配置层级）—— 逐字事实，好判分
#:   · cmd  ：Cisco 的 "To <目的>, use the <命令> command" —— 与中文题同一问法（要完成 X 用哪条命令）
EN_VENDORS = {"cisco", "juniper"}
#: 字段名 → (中文问法, 最多取几行)。只用**短、逐字、好判分**的三个字段：
#: Required Privilege Level 的值带长破折号散文，Examples 会泄题，都不收。
EN_FIELDS = {
    "Command Modes": ("在哪一命令模式下执行", 1),
    "Command Default": ("的默认值是多少", 1),
    "Hierarchy Level": ("在哪个配置层级下配置", 2),
}
EN_TEMPLATES = {
    "field": "{v} 文档里「{cmd}」{what}？请按文档原文回答。",
    "cmd":   "{v} 设备上，要「{label}」，应该执行什么命令？请写出完整命令。",
}
#: Cisco 的"目的 → 命令"句式：`To <purpose>, use the <cmd> command in <mode> mode.`
#: ⚠ 目的捕获**必须允许换行**（`[^.;]` 而不是 `[^.;\n]`）：Cisco 的 PDF 抽取结果常在
#: `use the` 前折行（"...to begin a\nterminal session, use the\nactivation-character command"），
#: 排除换行会让绝大多数条目匹配不上（实测 22 条切片只有 2 条命中）。
EN_TO_CMD = re.compile(
    r"\bTo\s+([^.;]{12,240}?)[,.]?\s+use\s+the\s+([a-z][a-z0-9\-]{2,40})\s+command\b", re.S)
#: 参考条目里的"字段标题"——遇到下一个标题就停止取值（否则会把 Examples 整段吞进来）
EN_HEADERS = {"Syntax Description", "Syntax", "Command Default", "Command Modes", "Command History",
              "Usage Guidelines", "Examples", "Related Commands", "Hierarchy Level", "Description",
              "Required Privilege Level", "Release Information", "Options", "Note", "Table"}
#: 目录/页码残片：Juniper 的 `Description  |  1849`、`IN THIS SECTION`、纯数字行
EN_NOISE = re.compile(r"^\s*(?:\d+|IN THIS SECTION|Table\b|Figure\b)|.*\|\s*\d+\s*$")
#: 悬空/啰嗦后缀：小句以此结尾说明"话没说完"（`...可以执行` / `...通过执行命令`），剥掉再看。
#: ⚠ 只用**多字**词与少数安全单字（后/中/下）——`则`/`有`/`是`/`在`/`等` 这类单字结尾在中文里
#: 多半是词尾（规则、含有、也是、存在），当后缀剥会啃掉词尾（实测「规则」→「规」）。
DANGLE_TAIL = ("命令如下", "如下命令", "操作步骤", "等信息", "所示", "见下表", "如下表", "见表",
               "命令", "操作", "步骤", "方式", "方法", "执行", "使用", "通过", "可以", "需要",
               "进行", "以下", "如下", "之后", "的时候", "包括", "包含", "分别",
               "后", "中", "下")
LEAD_DROP = re.compile(r"^(?:如果|若|如|当|此时|那么|则|并可?|请|用户|您|我们|需要|要|可通过|通过|例如|另外)[，,、:：]?")
BOILER = {"执行命令", "通过执行命令", "在系统视图下执行命令", "在用户视图下执行命令", "执行", "命令",
          "如下", "以下", "上述", "该命令", "提示", "说明"}
#: RAG 注入窗口（与 retrieve.build_context 默认一致）：事实必须落在这一窗口内，否则
#: "检索没答对"其实是"资料压根没进上下文"，那样测的不是模型能力。
PER, TOTAL = 800, 1200
#: ⚠ 旧口径"事实必须落在**片头** 700 字内"已废弃（曾用作 FACT_WINDOW=700）：Cisco 切片中位
#: 4173 字，事实大都在第 700 字之后 ⇒ 候选池整个为空（实测 cisco/juniper「试 0 题」，连闸门
#: 都没走到）。现在统一用 `retrieve.pick_window`（长切片按查询挑窗口），见 `in_window`。

def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def squash(t: str) -> str:
    """判分/闸门用的规范形：NFKC + 去全部空白 + 大写。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", t or "").upper())


def facts_in(text: str) -> list[tuple[str, str]]:
    """抽关键事实 → [(大写事实, 类别)]，长的优先。

    ⚠ 质量闸门：`part` 类必须是"像型号/MIB 的东西"——含数字或以 `-MIB` 结尾；否则会收进
    `PARAM-VALUE`、`SYSTEM-LICENSE` 这类内部字段名，问出来不是工程师会问的问题。
    """
    out, seen = [], set()
    for rx, kind in FACT_RES:
        for m in rx.finditer(text):
            f = norm(m.group(0)).upper()
            if len(f) < 6 or f in STOP_FACTS or f in seen:
                continue
            if kind == "part" and not (re.search(r"\d", f) or f.endswith("-MIB")):
                continue
            if kind == "cmd" and any(w in EN_PROSE for w in f.split()):
                continue
            seen.add(f)
            out.append((f, kind))
    out.sort(key=lambda t: -len(t[0]))
    return out[:8]


def _strip_tail(c: str) -> str:
    """小句 → 剥掉前导连词/编号与尾部悬空后缀（`...可以执行` → `''`）。"""
    prev = None
    while c and c != prev:
        prev = c
        c = re.sub(r"^[\s\d\.\-—·•（）()\[\]【】、,，:：]+", "", c).strip()
        c = LEAD_DROP.sub("", c).strip()
        for suf in DANGLE_TAIL:
            if c.endswith(suf) and len(c) > len(suf):
                c = c[: -len(suf)].strip()
                break
    return c


def clean_intent(text: str, limit: int = 32) -> str:
    """事实之前的原文 → 可当"问什么"的意图短语（v1 的致命处，见模块 docstring）。

    做法：按标点切小句 → 逐句剥悬空后缀、把剥完只剩空壳/碎片的小句丢掉 → 剩下的按原序拼回。
    v1 直接取"事实前 32 字原文"，才会出现「执行命令」「通过执行命令」这种问点。
    剥完不足 6 字就返回 ""（该候选作废）。
    """
    text = re.sub(r"^[\s\d\.\-—·•（）()\[\]【】、,，:：]+", "", text or "")
    keep: list[str] = []
    # 空白也算小句边界：华为文档表头常被抽成「和特性 功能和特性 表5 XXXX技术指标 项目」，
    # 按空格切开才能把"项目""表5"这些碎片丢掉（不切就整串留下，题面不可读）。
    for c in re.split(r"[，,、；;。！？!?\n]+|\s+", text):
        cc = _strip_tail(c.strip())
        if len(cc) >= 6 and squash(cc) not in {squash(b) for b in BOILER}:
            keep.append(cc)
    if not keep:
        return ""
    # 全英文碎片用 ", " 拼（中英混排时用中文逗号），否则英文题面读起来是「a，b，c」
    sep = ", " if all(re.fullmatch(r"[A-Za-z0-9\-\. \%<>\$]+", k) for k in keep) else "，"
    return sep.join(keep)[:limit]


def label_ok(label: str) -> bool:
    """标签得像"人话"：含中文，或是**至少两个词**的英文短语。

    ⚠ 英文分支**不能要求纯空格分隔**：`clean_intent` 用"，"拼小句，于是英文标签长成
    `RELATED，DOCUMENTATION，Example` 这种形状 —— 用 fullmatch 要求空格分隔会让
    **Cisco/Juniper 的候选 100% 被拒**（实测两厂「试 0 题」，连闸门都没走到）。
    """
    low = label.lower().strip()
    if len(label) < 6 or low in {v.lower() for v in VENDOR_LABEL.values()} | set(VENDOR_LABEL):
        return False
    if label.count(":") + label.count("：") >= 2:
        return False
    if re.search(r"[\u4e00-\u9fff]", label):
        return True
    return len(re.findall(r"[A-Za-z][A-Za-z0-9\-]*", label)) >= 2


def clean_section(sec: str) -> str:
    """section → 可当问点的小节名：去尾部括号补充、去编号、限长。

    ⚠ 编号只能按"数字 + 分隔符 + 空白"的形态剥（`1.1 概述`），不能像以前那样把**所有前导数字**
    都当编号 —— Juniper 有 `3gpp-imsi` 这种以数字开头的语句名，会被啃成 `gpp-imsi`（实测）。
    """
    s = norm(re.sub(r"[（(][^）)]{0,40}[）)]\s*$", "", norm(sec)))
    s = re.sub(r"^[\s]*(?:\d+(?:\.\d+)*[\.\、\)]?\s+)+", "", s)
    s = re.sub(r"^[\s、,，:：\.\-—·•]+", "", s)
    s = re.sub(r"\.(html?|htm|md|txt|pdf)$", "", s, flags=re.I)
    return s[:32].strip()


def in_window(text: str, label: str, facts: list[str]) -> bool:
    """事实是否落在**真正会被注入的那个窗口**里（与 retrieve 的注入口径同一套代码）。

    用 label 当查询的代理：最终题面 = 模板 + label，词元基本重合；真正的判定仍是
    `gate()`（拿最终题面走真实检索路径）—— 这里只负责把明显不可能的候选早点扔掉。
    """
    win, _off = retrieve.pick_window(text, label, PER)
    w = squash(win)
    return all(squash(f) in w for f in facts)


def leaks(section: str, facts: list[str]) -> bool:
    """section 里如果直接含有关键事实，就不能拿它当问点（否则等于把答案写进题面）。"""
    ss = squash(section)
    return any(squash(f) in ss for f in facts)


def pick_qa(text: str) -> list[dict]:
    """按**句子**出题：同一句里凑够同类别事实，就用"事实前的意图小句"当问点。

    ⚠ 事实位置必须在**本句内**查找。用整片 `text.find()` 会把别处同名事实的位置算进来，
    于是窗口判定（旧口径 `FACT_WINDOW`，已改为 `in_window`）和"事实前原文"都可能取错 —— 长切片（Cisco 中位 4173 字）
    尤其严重。
    """
    out = []
    for m in re.finditer(r"[^。；;\n]+", text):
        sent, off = m.group(0), m.start()
        if len(sent) < 20:
            continue
        sent_c = re.sub(r"\s+", " ", sent).strip()
        up_sent = sent_c.upper()
        fs = facts_in(sent_c)
        if not fs:
            continue
        for kind in ("cmd", "num", "part"):
            same = [f for f, k in fs if k == kind]
            # 命令类允许**单事实**（一句里出现两条命令极罕见，卡 2 条会让命令题全军覆没）；
            # 型号/数值类要 2 条，否则"列出型号"只有一个候选，判分没有区分度。
            need = 1 if kind == "cmd" else 2
            if len(same) < need:
                continue
            pos = [up_sent.find(f) for f in same]
            if any(p < 0 for p in pos):
                continue
            first_sent = min(pos)
            label = clean_intent(sent_c[:first_sent]) or clean_intent(text[: off + first_sent])
            if not label_ok(label):
                continue
            if not in_window(text, label, same):     # 事实必须落进真正注入的窗口
                continue
            out.append({"kind": kind, "label": label, "facts": same[:3], "sentence": sent_c[:300]})
    return out


def gate(vendor: str, question: str, facts: list[str], topk: int) -> tuple[bool, dict]:
    """检索闸门：模型实际会看到的 context 里，必须含**全部**关键事实。"""
    hits = retrieve.search(vendor, question, topk=topk)
    ctx = squash(retrieve.build_context(hits, per=PER, total=TOTAL, query=question))
    ok = all(squash(f) in ctx for f in facts)
    return ok, {"top_secs": [(h["section"] or "")[:40] for h in hits[:3]],
                "top_src": [(h["source"] or "")[:60] for h in hits[:2]]}


def en_field_values(seg: str) -> list[tuple[str, str]]:
    """英文参考条目 → [(字段名, 值文本)]。

    ⚠ 必须按行取值并**遇到下一个字段标题就停**：Juniper 的 `IN THIS SECTION` 目录、`Description | 1849`
    这类残片如果不过滤，会把页码当成"默认值"。
    """
    out: list[tuple[str, str]] = []
    for fname, (_what, maxlines) in EN_FIELDS.items():
        m = re.search(rf"^[ \t]*{re.escape(fname)}[ \t]*$", seg, re.M)
        if not m:
            continue
        vals: list[str] = []
        for ln in seg[m.end():].splitlines()[1:]:
            ln = ln.strip()
            if not ln:
                continue
            if ln.rstrip(":").strip() in EN_HEADERS:
                break
            if EN_NOISE.match(ln) or re.search(r"\|\s*\d+\s*$", ln):
                continue
            vals.append(ln)
            if len(vals) >= maxlines:
                break
        if vals:
            out.append((fname, " ".join(vals)))
    return out


def en_fact_of(value: str) -> str:
    """字段值 → 一条**逐字**事实（判分按去空白大写子串比对，所以越贴近原文越好）。

    "Line configuration (config-line)" → 取 "Line configuration"（括号里是缩写，重复）；
    "Return (decimal 13)" → 本体只有 6 字（"Return"）太泛，保留括号里的数值。
    """
    v = re.sub(r"\s+", " ", value).strip().rstrip(".,;:")
    core = re.sub(r"\s*\([^()]{0,40}\)\s*$", "", v).strip()
    cand = (core if len(core) >= 12 else v).strip("[]【】 ")
    return cand if len(re.sub(r"[\s\W_]+", "", cand)) >= 6 else ""


def en_hierarchy_fact(value: str) -> str:
    """Hierarchy Level 的多条路径 → 取**第一条路径尾部 3–4 个 token** 当事实。

    ⚠ 不能整段当事实：Juniper 一个语句常挂在多条路径下
    （`[edit protocols mld …], [edit tenants … security]`），整段 150 字要求模型逐字复现所有路径，
    只会造成假阴性；取尾部足够长的片段，模型答任一条完整路径都能命中。
    太短的路径（如 `edit security`）区分度不够，直接弃用。
    """
    first = re.split(r"\],?\s*\[", value.strip("[] "), maxsplit=1)[0].strip("[] ,")
    toks = [x for x in first.split() if x.lower() not in ("edit", "]", "[")]
    cand = " ".join(toks[-4:]) if len(toks) > 4 else " ".join(toks)
    return cand if len(cand) >= 20 and len(cand.split()) >= 3 else ""


def en_cmd_ok(cmd: str) -> bool:
    """命令名得像命令：≤4 个词、不含英文虚词、不是 `A through B` 这种页码跨度。"""
    if not cmd or len(cmd) > 60 or " through " in f" {cmd.lower()} ":
        return False
    if len(cmd.split()) > 4:
        return False
    return not re.search(r"\b(the|and|or|of|to|for|with|from|that|which|this|you|your)\b", cmd.lower())


def pick_qa_en(vendor: str, text: str, sec: str) -> list[dict]:
    """英文参考手册取题（与 `pick_qa` 并列的第二条取题路，见 EN_VENDORS 注释）。"""
    vlabel = VENDOR_LABEL.get(vendor, vendor)
    out: list[dict] = []

    # ① 字段题：命令 + 参考字段（模式 / 默认值 / 配置层级）
    cmds = [(m.start(2), m.group(2)) for m in EN_TO_CMD.finditer(text)]
    sec_cmd = clean_section(sec)
    # ⚠ section 兜底只给 Juniper：Cisco 的 section 是 `tunnel bandwidth through yellow` 这种
    # 页码跨度，当命令名问出来是垃圾题（实测）。
    if not cmds and vendor == "juniper" and en_cmd_ok(sec_cmd):
        cmds = [(0, sec_cmd)]
    for pos, cmd in cmds:
        nxt = min([p for p, _ in cmds if p > pos] + [len(text)])
        for fname, value in en_field_values(text[pos:nxt]):
            fact = en_hierarchy_fact(value) if fname == "Hierarchy Level" else en_fact_of(value)
            if not fact or squash(cmd) in squash(fact) or squash(fact) in squash(cmd):
                continue
            what = EN_FIELDS[fname][0]
            q = EN_TEMPLATES["field"].format(v=vlabel, cmd=cmd, what=what)
            if not in_window(text, f"{cmd} {what}", [fact]):
                continue                 # 事实落不进真会注入的窗口，早点扔掉（闸门还会再验一遍）
            out.append({"kind": "field", "need": 1, "label": f"{cmd} · {what}",
                        "facts": [fact], "sentence": fact[:300], "question": q})

    # ② 命令题：Cisco 的 "To <目的>, use the <命令> command"（与中文题同问法）
    for m in EN_TO_CMD.finditer(text):
        purpose, cmd = m.group(1), m.group(2)
        label = re.sub(r"\s+", " ", purpose).strip(" ,.;:")
        if len(label) < 12 or len(cmd) < 6:
            continue
        if squash(cmd) in squash(label) or not label_ok(label):
            continue
        out.append({"kind": "cmd", "need": 1, "label": label, "facts": [cmd.upper()],
                    "sentence": label[:300],
                    "question": EN_TEMPLATES["cmd"].format(v=vlabel, label=label[:120])})
    return out


def build_question(vendor: str, kind: str, label: str, tmpl: dict) -> str:
    return tmpl[kind].format(v=VENDOR_LABEL.get(vendor, vendor), label=label)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index-dir", default=r"D:\ait_llm\index")
    ap.add_argument("--train-dir", default=r"D:\ait_llm\train")
    ap.add_argument("--out", default=r"D:\ait_llm\train\eval_set.jsonl")
    ap.add_argument("--per-vendor-per-class", type=int, default=6)
    ap.add_argument("--seed", type=int, default=777)
    ap.add_argument("--max-common-count", type=int, default=10 ** 9,
                    help="common 类事实在训练语料里的出现次数上限（默认视为关闭）。"
                         "⚠ 设小会大量误滤：命令类事实在语料里出现几十上百次是常态")
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--pool-target", type=int, default=2500, help="每个厂商抽样候选数目标")
    ap.add_argument("--pool-rate-cap", type=float, default=0.5,
                    help="候选抽样比例的上限（默认 0.5 = 历史行为，保证已有题集可复现）。"
                         "小厂切片特别少（cisco 8 MB）时给 1.0 才能抽满候选池，"
                         "否则 --per-vendor-per-class 加到 20 也出不了 20 题")
    ap.add_argument("--max-attempts", type=int, default=60,
                    help="每厂商每类最多试多少个候选（过闸门才收）")
    ap.add_argument("--vendors", default=",".join(DEFAULT_VENDORS),
                    help="逗号分隔的厂商列表。默认四厂（= 已有评测所依据的主集）；"
                         "cisco,juniper 走英文取题路，形如 "
                         "--vendors cisco,juniper --per-vendor-per-class 20 --pool-target 20000 "
                         "--pool-rate-cap 1.0 --out …\\eval_set_en.jsonl")
    a = ap.parse_args()
    rng = random.Random(a.seed)

    print("载入训练语料（用于判定事实是否被训过）…", flush=True)
    train_text = ""
    for name in ("cpt_corpus.txt", "sft_qa.jsonl"):
        p = os.path.join(a.train_dir, name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                train_text += fh.read()
    train_upper = squash(train_text)
    print(f"训练语料 {len(train_upper)/1048576:.1f} MB（规范后）", flush=True)
    # ⚠ 防"语料被 append 累加"这种事悄悄发生：重复段落一多，common/longtail 的判定基准就不可信了。
    #   （`build_train_set.py` 曾经因为不清空而把 cpt_corpus.txt 累到 268.7 MB，训练实际只用 43.79 MB。）
    _paras = [x for x in train_text.split("\n\n") if len(x) > 200]
    if len(_paras) > 500:
        _sample = _paras[:: max(1, len(_paras) // 20000)][:20000]
        _dup = 1.0 - len(set(_sample)) / len(_sample)
        if _dup > 0.05:
            print(f"!! 警告：训练语料里 {_dup:.0%} 的段落在重复 —— 很可能是重跑时没有清空产物文件。"
                  f"分类基准会偏大，common/longtail 需重判。", flush=True)
    count_cache: dict[str, int] = {}

    def common_ok(f: str) -> bool:
        """该事实是否出现在训练语料里（口径与 check_eval_sets.py 一致：都去空白后子串比对）。"""
        pf = squash(f)
        if pf not in train_upper:
            return False
        if a.max_common_count >= 10 ** 6:
            return True          # 上限视为关闭时不必数次数（省掉 172M 字符的 count 扫描）
        return count_cache.setdefault(pf, train_upper.count(pf)) <= a.max_common_count

    rows: list[dict] = []
    seen_q: set[str] = set()                          # 题面去重（见收题处）
    t_all = time.time()
    vendors = tuple(x.strip() for x in a.vendors.split(",") if x.strip())
    for vendor in vendors:
        if vendor in SKIP_VENDORS:
            print(f"[{vendor:7s}] 跳过：{SKIP_VENDORS[vendor]}", flush=True)
            continue
        path = os.path.join(a.index_dir, f"{vendor}_chunks.jsonl")
        if not os.path.exists(path):
            continue
        pool: list[dict] = []
        est_lines = max(1.0, os.path.getsize(path) / 1024 / 1024 * 700)
        # ⚠ 上限从 0.5 放到 1.0：Cisco 切片只有 8.1 MB（约 5700 行），目标池 6000 时被 0.5 卡住，
        # 只能抽到一半 ⇒ 候选池不够，`--per-vendor-per-class` 再往上加也出不了更多题（实测
        # 该厂 common 池只有 17 条候选）。大厂（huawei 291 MB）受影响可忽略：
        # 6000/20 万 = 0.03，本来就不会碰到上限。
        rate = min(1.0, max(0.004, a.pool_target / est_lines))
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if rng.random() > rate:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:                                    # noqa: BLE001
                    continue
                # ⚠ 上限必须够大：Cisco 的头部是
                # `[cisco · Cisco IOS Configuration Fundamentals Command Reference/A through B · A through B]`
                # = **88 字**，卡 80 会剥不掉 ⇒ 头部文字变成"第一句"，进而污染 label 与事实
                # （实测：cisco/juniper 的 label 全是 `Configuration，Fundamentals，Comma…`）。
                raw = re.sub(r"^\[[^\]]{0,240}\]\s*", "", rec.get("text") or "")
                # ⚠ 英文厂商**必须保留换行**：`pick_qa_en` 靠"字段名独占一行"定位
                # （`^Command Modes$`）；`norm()` 会把换行压成空格，标题就再也匹配不上
                # （实测 juniper 836 条切片 0 候选，根因就是这个）。
                t = re.sub(r"[ \t]+", " ", raw).strip() if vendor in EN_VENDORS else norm(raw)
                sec = norm(rec.get("section") or "")
                # ⚠ 上限别设 1500：Cisco 切片中位长度 4173 字，卡 1500 等于整厂排除
                # （实测 cisco 可出题切片 0 条）。事实的窗口判定交给 `in_window`
                # （与注入同一套 `retrieve.pick_window`），不再用"片头 N 字"这种近似。
                if not (200 <= len(t) <= 4000) or not sec:
                    continue
                qas = pick_qa_en(vendor, t, sec) if vendor in EN_VENDORS else pick_qa(t)
                if qas:
                    pool.append({"rec": rec, "text": t, "qas": qas})
        rng.shuffle(pool)
        label = VENDOR_LABEL.get(vendor, vendor)
        common_n = long_n = 0
        tried = {"common": 0, "longtail": 0}
        rej = {"common": 0, "longtail": 0}
        t0 = time.time()
        for item in pool:
            if common_n >= a.per_vendor_per_class and long_n >= a.per_vendor_per_class:
                break
            t, sec = item["text"], norm(item["rec"].get("section"))
            src = item["rec"].get("source") or item["rec"].get("file") or ""
            sec_clean = clean_section(sec)
            for qa in item["qas"]:
                same, kind, lab = qa["facts"], qa["kind"], qa["label"]
                # ⚠ 必须拿**去空白的规范形**去比语料：事实形如 `DISPLAY VERSION`（带空格），
                #   而语料规范形是去空白的 `DISPLAYVERSION` —— 直接比会让 `in` 几乎恒 False、
                #   `not in` 几乎恒 True，于是 longtail 里塞满"其实语料里就有"的事实
                #   （实测 28/51 条分类不符，check_eval_sets.py 抓到）。
                pick_common = [f for f in same if common_ok(f)][:3]
                pick_long = [f for f in same if squash(f) not in train_upper][:3]
                # 英文题（field/cmd）自带 need=1：字段值只有一条逐字事实，"列型号"才需要两条
                need = qa.get("need") or (1 if kind == "cmd" else 2)
                for cls, facts in (("common", pick_common), ("longtail", pick_long)):
                    if len(facts) < need:
                        continue
                    if (common_n if cls == "common" else long_n) >= a.per_vendor_per_class:
                        continue
                    if tried[cls] >= a.max_attempts:
                        continue
                    tried[cls] += 1
                    # 问点一：清洗后的意图小句（英文题用 pick_qa_en 已经拼好的题面）；
                    # 问点二（退路）：不泄题的 section —— 英文厂商不走这条（section 是 "A through B" 页码跨度）
                    if qa.get("question"):
                        cands = [(qa["question"], "en-" + kind, lab)]
                    else:
                        cands = [(build_question(vendor, kind, lab, TEMPLATES), "intent", lab)]
                        if sec_clean and not leaks(sec_clean, facts) and sec_clean != lab:
                            cands.append((build_question(vendor, kind, sec_clean, SEC_TEMPLATES),
                                          "section", sec_clean))
                    hit = None
                    for q, anchor, anchor_text in cands:
                        ok, info = gate(vendor, q, facts, a.topk)
                        if ok:
                            hit = (q, anchor, anchor_text, info)
                            break
                    if not hit:
                        rej[cls] += 1
                        continue
                    q, anchor, anchor_text, info = hit
                    if q in seen_q:
                        # 同一段落里两条事实会清洗出**同一个题面**（实测 huawei-longtail-00/01），
                        # 留着就是同一道题问两遍、白跑一遍评测。
                        rej[cls] += 1
                        continue
                    seen_q.add(q)
                    rows.append({"vendor": vendor, "class": cls, "qtype": kind,
                                 "question": q, "key_facts": facts, "section": sec,
                                 "source": src, "src_text": qa["sentence"],
                                 "anchor": anchor, "anchor_text": anchor_text,
                                 "gate_ok": True, "gate_top_secs": info["top_secs"]})
                    if cls == "common":
                        common_n += 1
                    else:
                        long_n += 1
                    break
        print(f"[{vendor:<7}] common {common_n} 题 / longtail {long_n} 题 "
              f"（试 {tried['common']}/{tried['longtail']}、闸门拒 {rej['common']}/{rej['longtail']}、"
              f"{time.time()-t0:.0f}s）", flush=True)

    for n, r in enumerate(rows):                      # ID 必须唯一（评测报告按它对齐）
        r["id"] = f"{r['vendor']}-{r['class']}-{n:02d}"

    with open(a.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    md = os.path.splitext(a.out)[0] + ".md"
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(f"# 评测题集（{len(rows)} 题，自动判分：关键事实命中）\n\n")
        fh.write("每题的题面都用**真实注入路径**验证过：检索 topk 后按 `build_context(per=800,"
                 "total=1200, query=题面)` 拼出的上下文里含全部关键事实 —— 长切片注入的是"
                 "**按题面选中的窗口**（`retrieve.pick_window`），不是片头"
                 "（`anchor` 记录问点取自意图小句还是小节名）。\n\n")
        for r in rows:
            fh.write(f"## [{r['class']}/{r['qtype']}] {r['id']}（anchor={r['anchor']}）\n{r['question']}\n\n"
                     f"- 关键事实：{'、'.join(r['key_facts'])}\n- 源：`{r['source']}`\n\n")
    n_c = sum(1 for r in rows if r["class"] == "common")
    print("-" * 60)
    print(f"共 {len(rows)} 题：common {n_c}（训练语料里有）/ longtail {len(rows)-n_c}（一次没出现过）")
    print(f"过闸门率：{len(rows)} 收 / {sum(1 for r in rows)} 题；用时 {(time.time()-t_all)/60:.1f} 分钟")
    print(f"落盘：{a.out} 与 {md}")
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
