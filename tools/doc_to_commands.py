#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桥接：抓下来的厂商文档 → `_vendor_cmd/<vendor>/commands.jsonl`。

为什么必须有这一步（这是"整个流程打通"缺的那一环）
================================================
`build_tech_docs.py` 的输入**不是** `_vendor_docs/` 本身，而是
`_vendor_cmd/<vendor>/commands.jsonl` 里每条记录的 **`source` 字段**：

    {"command":"display vlan","feature":"二层技术-以太网交换/VLAN",
     "source":"h3c_cr16000/04-二层技术-以太网交换配置指导/html/xxx.htm"}
                                  ↑ 这条才是 build_tech_docs 去读正文的地址

`build_tech_docs.py` 只按 `feature` 分组、把 `sources` 里的文件正文抓出来，
它**从不遍历 `_vendor_docs/` 找新文档**。所以：

    只把新文档丢进 _vendor_docs/ → build_tech_docs **完全看不见它**
    必须先在 commands.jsonl 里为它写出 (feature, command, source) 三元组

实测（2026-10-04）：`_vendor_docs/` 里放了 `h3c_s5100ei/`，直接跑 build_tech_docs
产出与不放完全一致 —— 就是这个原因。

数据流全景
==========
    fetch_vendor_docs.py fetch   厂商官网在线文档 → _vendor_docs/<name>/（章/html/*.htm + toc.txt）
              ↓  ★ 本工具（缺的就是这一环）
    _vendor_cmd/<vendor>/commands.jsonl   （command + feature + source）
              ↓  build_tech_docs.py（纯代码，零 AI）
    knowledge/<vendor>/tech/<feature>.md + index.json
              ↓  gen_tech_summary.py（唯一允许调模型的一步）
    knowledge/<vendor>/tech/ai/*.md
              ↓  core/tech_knowledge.py（纯代码检索）
    运行时 knowledge.explain → 界面

新老页面格式的唯一差异（实测）
============================
新在线文档（S5100 这类，2026 抓的）与老 CHM 解包页结构**几乎一致**：

    相同：`<table class=Table>` + `<thead>` 表头 + `<p class=TableText>` 单元格
    不同：老页用 **`<b>` 加粗**标记"必须照输的关键字"；
          新页改用 **`<span class=commandkeywords>`**（参数是 `commandparameter`）。
          实测新页 `<b>` 数量 = 0 —— 照搬老解析器会**一条命令都提不出来**，
          而且不会报错，只是静默产出空结果。

本工具同时认这两种标记，所以新旧文档都能吃。

用法
====
    python tools/doc_to_commands.py --doc-name h3c_s5100ei --vendor h3c \
                                    --model S5100EI --out /tmp/cmds.jsonl
    python tools/doc_to_commands.py ... --append        # 追加进真实 jsonl（自动备份）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import time

VENDOR_DOCS = os.path.expanduser("~/deepseek_harness/_vendor_docs")
CMD_ROOT = os.path.expanduser("~/deepseek_harness/_vendor_cmd")

# ⚠ 这三条正则**必须用单引号**的 raw string。
#   踩过的坑：写成 r"...class=\"?Table..." 时，raw string 里的 \" **不是**转义，
#   而是「反斜杠 + 引号」两个字面字符 —— 于是正则变成要求 HTML 里真有 `\Table\b`，
#   一条都匹配不到。更阴的是它**不报错**：TABLE_RE 仍能靠 [^>]* 的回溯匹配到
#   `<table ...>` 本身（于是"表数=7"看着正常），但 ROW_RE 拿到的是残缺片段，
#   最后静默产出 0 条命令。
TABLE_RE = re.compile(r'<table[^>]*class="?Table\b.*?</table>', re.S | re.I)
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
#: 单元格里的"命令变体"段落。老页靠 <b>，新页靠 <span class=commandkeywords>
CELL_P_RE = re.compile(r'<p[^>]*class="?TableText[^>]*>(.*?)</p>', re.S | re.I)
KEYWORD_RE = re.compile(
    r'<span[^>]*class="?commandkeywords"?[^>]*>(.*?)</span>|<b[^>]*>(.*?)</b>',
    re.S | re.I)
TOC_RE = re.compile(r"^(\s*)-\s+(.*?)\s+\[(.*?)\]\s*$")


def log(m: str) -> None:
    print(m, flush=True)


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def read_text(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return decode(fh.read())
    except OSError:
        return ""


def load_toc(doc_root: str) -> dict:
    """toc.txt → {相对路径(lower): 小节标题}。与 build_tech_docs.load_h3c_toc 同构。"""
    out = {}
    path = os.path.join(doc_root, "toc.txt")
    for line in read_text(path).splitlines():
        m = TOC_RE.match(line)
        if not m:
            continue
        title, rel = m.group(2), m.group(3).strip()
        if rel:
            out[rel.lower().replace("\\", "/")] = title
    return out


def split_rows(tbl_html: str) -> list:
    """把表格切成行。

    ⚠ **不能**用 `<tr[^>]*>(.*?)</tr>`。H3C 这些页面是 Word 导出的 HTML，
    `<tr>` 是**不闭合**的（实测 `</tr` 出现 0 次），行与行之间只靠下一个
    `<tr>` 或 `</table>` 分隔。用 `</tr>` 做右边界会**一条都匹配不到**，
    而且不报错 —— 表现为"表数正常、行数全 0、最终 0 条命令"。

    改成按 `<tr` 切分：对闭合/不闭合两种 HTML 都成立。
    """
    parts = re.split(r"<tr[^>]*>", tbl_html, flags=re.I)
    return [p for p in parts[1:] if p.strip()]


def feature_for(doc_name: str, rel: str, toc: dict) -> str:
    """feature = 一级（章）/ 二级（小节），与既有 jsonl 的命名习惯一致。

    既有 h3c jsonl 的 feature 形如 `基础配置/CLI`、`二层技术-以太网交换/VLAN` ——
    正是"章名去掉 NN- 与 配置指导 后缀" + "小节名去掉 NN- 与 配置 后缀"。
    对不上时会退化成 `章/小节` 原样，仍然可用（只是不如规范化后好看）。
    """
    parts = rel.split("/")
    chapter = parts[0] if parts else ""
    section = toc.get(rel.lower(), "")
    chapter = re.sub(r"^\d+-", "", chapter)
    chapter = re.sub(r"配置指导$", "", chapter).strip()
    section = re.sub(r"^\d+-", "", section)
    section = re.sub(r"配置$", "", section).strip()
    if not section:                      # 小节名没拿到 → 用文件名兜底
        section = os.path.splitext(parts[-1])[0] if parts else ""
    return ("%s/%s" % (chapter, section)).strip("/")


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    s = s.replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"\s+", " ", s).strip()


def command_from_cell(cell_html: str) -> list:
    """一个命令单元格 → 若干 (command, syntax)。

    H3C 的写法：`<p class=TableText>` 里，命令关键字被标出来，参数是占位符。
    整段文本就是 syntax；命令名 = 开头的关键字序列（截到第一个 [ { ( < | 之前）。
    """
    out = []
    for raw in CELL_P_RE.findall(cell_html):
        # 语法行 = 该段落全文（去掉标记后）
        syntax = clean(raw)
        if not syntax or len(syntax) > 300:
            continue
        # 命令名：优先用被标记的关键字拼接；没有标记就按标点截断
        kws = [clean(a or b) for a, b in KEYWORD_RE.findall(raw)]
        kws = [k for k in kws if k]
        if kws:
            name = " ".join(kws).strip()
        else:
            name = re.split(r"[\[{\(<|]", syntax)[0].strip()
        name = re.sub(r"\s+", " ", name).strip()
        if not name or len(name) > 80:
            continue
        # 命令名不得混入中文/提示符（既有 jsonl 的自检项之一）
        if re.search(r"[\u4e00-\u9fa5]", name) or ">" in name or "#" in name:
            continue
        if len(name.split()) > 6:
            continue
        out.append((name, syntax))
    return out


def parse_page(path: str, rel: str, doc_name: str, vendor: str, model: str,
               toc: dict) -> list:
    html = read_text(path)
    if not html:
        return []
    feature = feature_for(doc_name, rel, toc)
    source = "%s/%s" % (doc_name, rel)
    recs, seen = [], set()
    for tbl in TABLE_RE.findall(html):
        rows = split_rows(tbl)
        if not rows:
            continue
        head = [clean(c) for c in CELL_RE.findall(rows[0])]
        # 只认命令表：表头里有"命令"，且不是"用户角色名|权限"这类
        if not any("命令" in h for h in head):
            continue
        ci = next((i for i, h in enumerate(head) if "命令" in h), None)
        if ci is None:
            continue
        for row in rows[1:]:
            cells = CELL_RE.findall(row)
            if ci >= len(cells):
                continue
            for name, syntax in command_from_cell(cells[ci]):
                key = (name, syntax)
                if key in seen:
                    continue
                seen.add(key)
                recs.append({"vendor": vendor, "model": model,
                             "command": name, "syntax": syntax,
                             "view": infer_view(syntax),
                             "feature": feature, "kind": "command",
                             "source": source})
    return recs


# ============================================================ Cisco / Juniper
# 为什么必须分开写：三家的**正文标记完全不同**，不是换个 class 名的事。
#
#   h3c     命令表：`<table class=Table>` + `<p class=TableText>`
#           关键字：`<span class=commandkeywords>`（新页）/ `<b>`（老页）
#   cisco   命令标题：`<h2 class="title topictitle2">anti-jam</h2>`
#           语法块：`<section class="section refsyn">` 里的
#                   `<span class="keyword kwd">` 拼接（思路同 h3c，但 class 不同）
#   juniper 命令标题：`<h1 class="title topictitle1">show interfaces extensive</h1>`
#           语法块：`<h2>Syntax</h2>` 之后的那一节
#
# 实测（2026-10-04）：Cisco 页面里 `<table class=Table>`（大写 T）出现 0 次，
# 它是 `class="table"`；照搬 h3c 的正则会**静默产出 0 条命令**。
CISCO_CMD_RE = re.compile(
    r'<h2[^>]*class="[^"]*topictitle2[^"]*"[^>]*>(.*?)</h2>', re.S | re.I)
CISCO_SYN_RE = re.compile(
    r'<section[^>]*class="[^"]*refsyn[^"]*"[^>]*>(.*?)</section>', re.S | re.I)
JUNIPER_CMD_RE = re.compile(
    r'<h1[^>]*class="[^"]*topictitle1[^"]*"[^>]*>(.*?)</h1>', re.S | re.I)
JUNIPER_SYN_RE = re.compile(
    # ⚠ h2 里 "Syntax" 后面还跟着 `<a class="bookmarktitle">`（复制链接按钮），
    #   所以不能写成 `>\s*Syntax\s*</h2>` —— 那样一个都匹配不到。
    r'<h2[^>]*class="[^"]*sectiontitle[^"]*"[^>]*>\s*Syntax\b.*?</h2>'
    r'(.*?)(?=<h2|<div class="section"|$)',
    re.S | re.I)
#: 关键字标记：Cisco 的 `keyword kwd` 与 H3C 的 `commandkeywords` 是同一套思路
KWD_SPAN_RE = re.compile(
    r'<span[^>]*class="[^"]*\bkeyword\b[^"]*"[^>]*>(.*?)</span>', re.S | re.I)


#: 文档页里的 **UI 按钮文字**：Juniper 在每个代码块旁渲染"复制/缩放"图标，
#: 图标名会变成可见文本 `content_copy zoom_out_map`，紧贴在命令前面。
#: 实测不清掉会得到 `content_copy zoom_out_map show interfaces extensive` 这种假命令名。
_UI_NOISE_RE = re.compile(
    r"(?:content_copy|zoom_out_map|bookmarktitle|keyboard_arrow_\w+|"
    r"Copy link to this section|open_in_new|file_download)\s*", re.I)


def _kw_syntax(block_html: str) -> tuple:
    """从语法块里取 (命令名, 语法整串)。

    ⚠ 命令名**不能**简单地把所有关键字 span 拼起来：
      Cisco 的 `cd [ filesystem : ] [directory]` 里 `:` 也是 `keyword kwd`，
      拼起来会得到 `cd :`。正确做法是**在第一个 `[`/`{`/`(`/`|` 处截断** ——
      那之后是可选/参数部分，不属于命令名。
    """
    syntax = _UI_NOISE_RE.sub("", clean(block_html)).strip()
    if not syntax or len(syntax) > 300:
        return "", ""
    name = re.split(r"[\[{\(<|]", syntax)[0].strip().rstrip(":").strip()
    if not name:                                    # 截不出东西才退回关键字拼接
        kws = [clean(m) for m in KWD_SPAN_RE.findall(block_html)]
        name = " ".join(k for k in kws if k).strip()
    name = re.sub(r"\s+", " ", name).strip()
    if not name or len(name) > 80:
        return "", ""
    if re.search(r"[\u4e00-\u9fa5]", name) or ">" in name or "#" in name:
        return "", ""
    if len(name.split()) > 6:
        return "", ""
    return name, syntax


def parse_page_cisco(path, rel, doc_name, vendor, model, toc) -> list:
    """Cisco：**每个 `<section class="section refsyn">` 就是一条命令**。

    为什么不按标题切：实测同一家的两个书用的标记不一样 ——
      `ir-cr-book/*` 有 `<h2 class="title topictitle2">命令名</h2>`；
      `cf_command_ref/*` 页面上这种 h2 出现 **0 次**，命令名只能从 refsyn 里取。
    按 refsyn 切对两种书都成立，是更稳的锚点。
    """
    html = read_text(path)
    if not html:
        return []
    feature = feature_for(doc_name, rel, toc)
    source = "%s/%s" % (doc_name, rel)
    recs, seen = [], set()
    for sm in CISCO_SYN_RE.finditer(html):
        # ⚠ 一个 refsyn 节里可能有**多条语法行**（Cisco 把正/反两种形式放在同一节）：
        #     <p class="figgroup synblk">activation-character ascii-number</p>
        #     <p class="figgroup synblk">no activation-character [ascii-number]</p>
        #   整节当成一条会得到 `activation-character ascii-number no activation-character`
        #   这种拼起来的假命令名。按 <p 拆开才是对的。
        #   ⚠ 切分要**保留 `<`**（`(?=<p\b)` 零宽前瞻），否则 `class="...">cd`
        #     里的属性文本会混进命令名，被 `">" in name` 整条丢掉 —— 表现为"全 0 条"。
        for part in re.split(r"(?=<p\b)", sm.group(1))[1:] or [sm.group(1)]:
            name, syntax = _kw_syntax(part)
            if not name or (name, syntax) in seen:
                continue
            seen.add((name, syntax))
            recs.append({"vendor": vendor, "model": model,
                         "command": name, "syntax": syntax,
                         "view": infer_view(syntax), "feature": feature,
                         "kind": "command", "source": source})
    return recs


def parse_page_juniper(path, rel, doc_name, vendor, model, toc) -> list:
    """Juniper：一个 `<h1 class=topictitle1>` 就是一条命令，语法在 `<h2>Syntax</h2>` 后。"""
    html = read_text(path)
    if not html:
        return []
    feature = feature_for(doc_name, rel, toc)
    source = "%s/%s" % (doc_name, rel)
    cm = JUNIPER_CMD_RE.search(html)
    title = clean(cm.group(1)) if cm else ""
    recs, seen = [], set()
    for sm in JUNIPER_SYN_RE.finditer(html):
        name, syntax = _kw_syntax(sm.group(1))
        if not name:
            name = title
            syntax = title
        if not name or len(name) > 80:
            continue
        if (name, syntax) in seen:
            continue
        seen.add((name, syntax))
        recs.append({"vendor": vendor, "model": model,
                     "command": name, "syntax": syntax,
                     "view": infer_view(syntax), "feature": feature,
                     "kind": "command", "source": source})
    return recs


#: 厂商 → 页面解析器。h3c 走表格，cisco/juniper 走标题+语法节。
VENDOR_PARSERS = {
    "h3c": lambda *a: parse_page(*a),
    "cisco": parse_page_cisco,
    "juniper": parse_page_juniper,
}


def infer_view(syntax: str) -> str:
    """H3C 文档没有独立的「视图」小节，既有 jsonl 里 view 也是推断值。

    这里只做最保守的语义兜底（display/reset 类多在任意视图，其余标系统视图），
    与既有数据同性质 —— **如实标注是推断，不假装是原文**。
    """
    low = syntax.lower()
    if low.startswith(("display ", "reset ", "ping", "tracert", "telnet", "ssh",
                       "show ", "clear ")):
        return "任意视图"
    return "系统视图"


def load_existing_features(vendor: str) -> set:
    """读既有 commands.jsonl 里出现过的 feature 名。

    ★ 为什么必须对齐它们：`build_tech_docs.py` 是按 **feature 名**分组的。
      新文档若派生出 `基础/CLI`，而既有数据里是 `基础配置/CLI`，那么两者
      **不会合并**，知识库里会多出一个平行的新 feature —— 不是"把新文档补充进
      已有条目"，而是"另起一条"。用户搜同一个词会看到两条互不相干的结果。
      所以入库前先把 feature 名对齐到既有命名。
    """
    path = os.path.join(CMD_ROOT, vendor, "commands.jsonl")
    feats = set()
    if not os.path.exists(path):
        return feats
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                f = json.loads(line).get("feature")
            except ValueError:
                continue
            if f:
                feats.add(f)
    return feats


def align_feature(derived: str, existing: set) -> str:
    """把派生出来的 feature 名对齐到既有命名；对不上就保持原样（并如实保留）。

    规则（保守，宁可不对齐也不要错并）：
      1. 派生名已在既有集合里 → 直接用；
      2. 否则看"末段"（模块名，如 `CLI`）相同的既有 feature：
         · 恰好一个 → 用它；
         · 多个 → 取首段与派生名首段**互相包含**的那个（`基础` ↔ `基础配置`）；
         · 都不满足 → 不并，保留派生名。
    """
    if not existing or not derived:
        return derived
    if derived in existing:
        return derived
    head, _, tail = derived.partition("/")
    if not tail:
        return derived
    same_tail = [f for f in existing if f.endswith("/" + tail)]
    if not same_tail:
        return derived
    if len(same_tail) == 1:
        return same_tail[0]
    for f in same_tail:
        fh = f.split("/", 1)[0]
        if head in fh or fh in head:
            return f
    return derived


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-name", required=True, help="_vendor_docs 下的子目录名")
    ap.add_argument("--vendor", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", default="", help="输出 jsonl；不给就只统计")
    ap.add_argument("--append", action="store_true",
                    help="追加进 _vendor_cmd/<vendor>/commands.jsonl（自动备份）")
    ap.add_argument("--allow-new-vendor", action="store_true",
                    help="该厂商语料还不存在时，允许新建（全新厂商首次入库用）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-align", action="store_true",
                    help="不对齐既有 feature 名（默认会对齐，避免并成两条）")
    a = ap.parse_args()

    doc_root = os.path.join(VENDOR_DOCS, a.doc_name)
    if not os.path.isdir(doc_root):
        log("✗ 找不到 %s" % doc_root)
        return 1
    toc = load_toc(doc_root)
    log("doc_root = %s" % doc_root)
    log("toc 小节 = %d" % len(toc))

    pages = []
    for base, _dirs, files in os.walk(doc_root):
        for f in sorted(files):
            if f.lower().endswith((".htm", ".html")):
                full = os.path.join(base, f)
                pages.append((full, os.path.relpath(full, doc_root).replace("\\", "/")))
    pages.sort(key=lambda x: x[1])
    if a.limit:
        pages = pages[:a.limit]
    log("页面 = %d" % len(pages))

    all_recs, per_feature = [], {}
    parser = VENDOR_PARSERS.get(a.vendor)
    if parser is None:
        log("✗ 没有 %s 的解析器（已支持：%s）"
            % (a.vendor, "、".join(sorted(VENDOR_PARSERS))))
        return 1
    for full, rel in pages:
        recs = parser(full, rel, a.doc_name, a.vendor, a.model, toc)
        if recs:
            per_feature.setdefault(recs[0]["feature"], 0)
            per_feature[recs[0]["feature"]] += len(recs)
        all_recs.extend(recs)
        log("  %-52s %3d 条" % (rel[:52], len(recs)))

    # ---- 把 feature 名对齐到既有命名，否则会并成两条互不相干的条目 ----
    if not a.no_align:
        existing = load_existing_features(a.vendor)
        log("既有 feature 名 %d 个" % len(existing))
        remap = {}
        for r in all_recs:
            old = r["feature"]
            new = align_feature(old, existing)
            if new != old:
                remap[old] = new
            r["feature"] = new
        if remap:
            log("feature 对齐：")
            for k, v in sorted(remap.items()):
                log("   %-28s → %s" % (k, v))
        else:
            log("feature 对齐：无需改动")
        per_feature = {}
        for r in all_recs:
            per_feature[r["feature"]] = per_feature.get(r["feature"], 0) + 1

    log("-" * 70)
    log("合计 %d 条命令，覆盖 %d 个 feature" % (len(all_recs), len(per_feature)))
    for k, v in sorted(per_feature.items(), key=lambda x: -x[1])[:12]:
        log("   %-40s %4d" % (k, v))
    if all_recs:
        log("样例：")
        for r in all_recs[:3]:
            log("   " + json.dumps(r, ensure_ascii=False)[:190])

    if a.out:
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as fh:
            for r in all_recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        log("已写 %s" % a.out)

    if a.append:
        vdir = os.path.join(CMD_ROOT, a.vendor)
        dest = os.path.join(vdir, "commands.jsonl")
        if not os.path.exists(dest):
            # 全新厂商（实测 cisco/juniper 之前完全没有语料）：
            # 建目录 + 空文件即可，不需要别的初始化 —— build_tech_docs 只认这个 jsonl。
            if not a.allow_new_vendor:
                log("✗ 目标不存在：%s" % dest)
                log("  这是**全新厂商**，首次入库请加 --allow-new-vendor 确认。")
                return 1
            os.makedirs(vdir, exist_ok=True)
            io.open(dest, "a", encoding="utf-8").close()
            log("＋ 新建厂商语料：%s" % dest)
        else:
            bak = dest + ".bak-before-%s" % time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(dest, bak)
            log("已备份 → %s" % bak)
        with io.open(dest, "a", encoding="utf-8", newline="\n") as fh:
            for r in all_recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        log("已追加 %d 条 → %s" % (len(all_recs), dest))
        log("下一步：python tools/build_tech_docs.py --vendor %s" % a.vendor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
