#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从厂商原始文档提取『技术讲解』正文，建成**代码可直接检索**的技术资料层。

为什么需要它
============
`knowledge/<vendor>/chapters/*.md` 里**只有命令字段表**（格式/视图/型号/分类/来源），
没有任何讲解正文 —— 实测 393 章、正文合计 **0 KB**，每章还写着
"_（本机没有该命令的讲解原文，只有上表的字段与来源）_"。
于是 `core/vendor_knowledge.explanation()` 永远返回空，界面上永远是"暂无技术解释"。

但**原始厂商文档就在盘上**（`~/deepseek_harness/_vendor_docs`），jsonl 的 `source`
字段对三厂商 **100% 可解析**，里面是真正的配置指导正文。本工具用**纯代码**把它们
提取出来、按 feature 归集成技术资料，供运行时**零 AI** 检索。

> 本工具**不调用任何模型**。AI 只在"结构化摘要"那一步用（另见 `gen_tech_summary.py`），
> 且必须以上面提取出的真实正文为**唯一依据** —— 先有原文，才允许 AI 整理；
> 绝不允许"没有原文让 AI 编"。

三种文档形态（实测，各不相同）
============================
    h3c      CHM 解包：`<章>/html/<hash>.htm`（GB18030）；另有 `toc.txt` 给出
             "标题 → 文件" 的完整层级（1343 条），用它给正文配上真实小节标题
    huawei   `*.html`（GB18030）；⚠ 有些页只是目录页（正文为空），要过滤
    ruijie   单个 `fulltext.txt`（UTF-8），`source` 用 `#L<行号>` 定位

用法
====
    python tools/build_tech_docs.py --vendor h3c
    python tools/build_tech_docs.py --vendor all --limit 20     # 先小样试
    python tools/build_tech_docs.py --vendor h3c --dry-run      # 只看统计不落盘

产物
====
    knowledge/<vendor>/tech/<feature>.md     每个 feature 一个技术资料文件
    knowledge/<vendor>/tech/index.json       命令→feature、feature→文件、术语倒排

`index.json` 的形状（运行时 `core/tech_knowledge.py` 直接读它，不再解析 md）：

    {"vendor": "h3c", "generated_at": "...", "doc_root": "...",
     "n_features": 129, "n_commands": 8556, "truncated_features": [...],
     "features": {"ACL和QoS/时间段": {"file": "tech/acl和qos-时间段.md",
                                     "commands": [...], "sources": [...], "bytes": 12345}},
     "commands": {"display time-range": {"feature": "ACL和QoS/时间段",
                                         "view": "任意视图", "syntax": "...",
                                         "generic": false, "n_features": 1,
                                         "features": ["ACL和QoS/时间段"]}},
     "terms": {"时间段": ["ACL和QoS/时间段", ...]}}

⚠ `commands` 里的 `generic` 字段很关键，别删
==========================================
`configure terminal` / `interface` / `enable` 这类**全局通用命令**在几乎每个 feature 里
都出现。早先用 `setdefault` 让"先遍历到的 feature"赢，而 feature 是按字典序排的，
于是 ruijie 的 `configure terminal` / `interface` / `enable` **全部**被挂到 `1588v2-PTP`
名下（`1588` 数字开头，排最前）—— 用户搜 `configure terminal` 会得到一篇 1588v2-PTP 的
讲解，属于**明确错误**，不是"不够精确"。

现在改成：一个命令出现在 >= `GENERIC_MIN_FEATURES` 个 feature 里 ⇒ `generic=True`，
`feature` 置空（**不硬指**），同时列出它真正出现过的 feature 供运行时给候选。
运行时 `core/tech_knowledge.py` 靠这个字段把通用命令与"某条专有命令"区分开。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import OrderedDict, defaultdict

# ---------------------------------------------------------------- 路径常量
#: 命令元数据（jsonl）根目录。source 字段的前缀与 _vendor_docs 的子目录名一一对应。
CMD_ROOT = os.environ.get("AIT_VENDOR_CMD_ROOT") or os.path.join(
    os.path.expanduser("~"), "deepseek_harness", "_vendor_cmd")
#: 原始厂商文档根目录（htm/html/txt）
DOC_ROOT = os.environ.get("AIT_VENDOR_DOC_ROOT") or os.path.join(
    os.path.expanduser("~"), "deepseek_harness", "_vendor_docs")
#: 产物根目录（默认就是仓库里的 knowledge/）
OUT_ROOT = os.environ.get("AIT_KNOWLEDGE_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge")

#: 支持的技术解释层厂商。
#: `tplink` 是后加的：它的命令记录来自 PDF 转出的 `fulltext.txt`，
#: source 形态是 `{doc}/fulltext.txt#L<n>`，与 ruijie **完全一致**，
#: 因此不需要新的解析分支（走 extract_for_source 的 `.txt` + 行号窗口那条路）。
#: 它**不进** TOC_VENDORS —— 那套契约要 `<章>/html/<节>.htm` + toc.txt，tplink 没有。
VENDORS = ("h3c", "huawei", "ruijie", "tplink", "cisco", "juniper")

#: 用"`<out_name>/<章>/html/<节>.htm` + 同目录 toc.txt"这套契约的厂商。
#: 它们的 source 形态与 h3c 完全一致（都是 htm + toc 权威标题），
#: 所以正文提取走同一条路，不需要各自的解析分支 —— 差异只在**上游抓取**那一步。
TOC_VENDORS = ("h3c", "cisco", "juniper")

#: 每个 feature 的**存储**上限（字节）。超了要如实记录，不能静默截断。
#: 注意：这是"存"的上限，不是"返回"的上限 —— 运行时要给界面一个**有界**的片段，
#: 由 core/tech_knowledge.py 在读取时再截（见它的 MAX_RETURN_BYTES）。
#: 这里放宽是因为：存少了以后就没法再要回来；存多了只是占磁盘（实测全量约 20 MB）。
MAX_FEATURE_BYTES = 600 * 1024
#: ruijie 的 fulltext 按行号锚点切片时，前后各取多少行
RUIJIE_WINDOW = 120

#: 一个命令出现在多少个 feature 以上，就判定为"全局通用命令"（不硬指某个 feature）。
#: 取 3 而不是 2：实测 2 会把 `display interface brief` 这类"恰好两个分类都讲"的命令
#: 误判成通用；3 以上才真正是 `configure terminal` 这种每个分类都出现的。
GENERIC_MIN_FEATURES = 3
#: `generic` 命令的 `features` 列表最多列几个（防止 `interface` 列出 129 个）
GENERIC_FEATURE_LIST = 12

#: 「桶」型 feature 名 —— **不是**真正的功能分类，而是文档结构桶或兜底桶。
#: 判"通用命令"时必须先把它们剔掉，否则计数被灌水。
#: 实测：`display vlan` 的 feature 列表是
#:   ['以太网交换', '以太网交换命令', '未分类']
#: 其中 `以太网交换命令` 是"命令参考"的章节桶、`未分类` 是元信息缺失的兜底桶，
#: 真正表达归属的只有 `以太网交换` 一个。不剔除的话 n=3 恰好越过阈值，
#: 一条典型的 VLAN 命令就被误判成"全局通用命令"（实测这类误判 1405 条）。
_BUCKET_FEATURE_RE = re.compile(
    r"^(未分类|其他|其它|通用|通用命令|无|none|n/?a|common|others?|misc)$"
    r"|(命令|命令参考|命令集|命令行|命令手册|指令|参考|参考手册)$",
    re.I)


def _is_bucket_feature(name: str) -> bool:
    """这个 feature 名是不是"桶"（章节/兜底），不该参与归属判定。"""
    return bool(_BUCKET_FEATURE_RE.search(str(name or "").strip()))

# ---------------------------------------------------------------- 噪声行
#: 页眉页脚/导航/版权等样板文字：命中即丢（否则正文里全是"上一节/下一节"）
_BOILERPLATE_RE = [
    re.compile(r"^版权所有"),
    re.compile(r"^Copyright\b", re.I),
    re.compile(r"^(上一节|下一节|上一条|下一条|父主题|返回顶部|目录)$"),
    re.compile(r"^[<>\s]*$"),
    re.compile(r"^---\s*第\s*\d+\s*页\s*---$"),          # ruijie 的分页标记
    re.compile(r"^[\d\s.·\-—]+$"),                        # 纯页码/纯符号
    re.compile(r"^(华为技术有限公司|新华三技术有限公司|杭州华三通信技术有限公司)$"),
    re.compile(r"^(文档版本|发布日期|浏览次数|相关文档|相关版本)\b"),
    re.compile(r"^\s*(打印|关闭|分享)\s*$"),
]
#: 正文里必须保留的结构性前缀（表格行、列表、代码），不参与"纯符号"过滤
_KEEP_PREFIX = ("|", "#", "-", "*", ">", "<", "（", "(", "【")


def _is_boilerplate(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    for rx in _BOILERPLATE_RE:
        if rx.match(s):
            return True
    return False


# ---------------------------------------------------------------- 读 + 解码
def _decode(raw: bytes) -> str:
    """按 utf-8 → gb18030 → latin-1 依次尝试。

    实测：h3c/huawei 的 htm/html 是 GB18030，ruijie 的 fulltext.txt 是 UTF-8。
    **顺序不能反** —— gb18030 几乎不会解码失败（它是全字节覆盖的），
    放前面会把 UTF-8 的字节解成乱码而不报错。
    """
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


#: 按路径缓存文件正文。**必须缓存**：ruijie 的每个 `#L<行号>` 都是一个独立 source，
#: 不缓存的话 909 个 source 会把同一份 5 MB 的 fulltext.txt 读 909 遍（≈4.5 GB IO），
#: 5 个 feature 就要 17 秒。
_TEXT_CACHE: dict[str, str] = {}
#: 缓存上限（字符数）。huawei 要读 ~2 万个 html，无上限缓存会把内存吃光
#: （实测就是这么被 OOM 掉的）。超了直接整体清空 —— 比实现 LRU 简单，且够用。
_TEXT_CACHE_MAX = 96 * 1024 * 1024
_text_cached = 0


def _longpath(path: str) -> str:
    """Windows 超长路径 → `\\\\?\\` 前缀形式。

    为什么要这个：本机 `LongPathsEnabled=0`，普通 Win32 调用 >260 字符直接
    WinError 3。而 H3C 新版 CHM（实测 S10500X-G R7753Pxx）解出来的树里有真实超长
    路径（相对 199 字符 + `_vendor_docs/h3c_xxx/` 68 字符 = 267）。
    `read_text` 的 `except OSError` 会把它**静默变成空正文** —— 技术层少内容但不报错。
    """
    if os.name != "nt":
        return path
    try:
        ap = os.path.abspath(path)
    except (OSError, ValueError):
        return path
    if len(ap) <= 240 or ap.startswith("\\\\?\\"):
        return ap
    if ap.startswith("\\\\"):
        return "\\\\?\\UNC" + ap[1:]
    return "\\\\?\\" + ap


def read_text(path: str) -> str:
    global _text_cached
    hit = _TEXT_CACHE.get(path)
    if hit is not None:
        return hit
    try:
        with open(_longpath(path), "rb") as fh:
            text = _decode(fh.read())
    except OSError:
        text = ""
    if _text_cached + len(text) > _TEXT_CACHE_MAX:
        _TEXT_CACHE.clear()
        _text_cached = 0
    _TEXT_CACHE[path] = text
    _text_cached += len(text)
    return text


# ---------------------------------------------------------------- HTML → 正文
_TAG_DROP = re.compile(r"(?is)<(script|style|noscript).*?</\1>")
_BLOCK_END = re.compile(
    r"(?i)</(p|div|li|tr|h[1-6]|table|ul|ol|pre|blockquote|dd|dt|td|th|section)\s*>")
_BR = re.compile(r"(?i)<br\s*/?>")


def html_to_blocks(text: str) -> list[str]:
    """把 HTML 变成"一行一块"的正文行列表。

    只做结构性替换（块级标签 → 换行），不试图还原版式 —— 目标是**可读可检索**，
    不是像素级还原。
    """
    t = _TAG_DROP.sub(" ", text)
    t = _BR.sub("\n", t)
    t = _BLOCK_END.sub("\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)            # 剩下的行内标签直接去掉
    t = (t.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'"))
    t = re.sub(r"&[a-zA-Z#0-9]{1,8};", " ", t)     # 剩余实体
    out = []
    for line in t.splitlines():
        s = re.sub(r"[ \t\u3000]+", " ", line).strip()
        if s and not _is_boilerplate(s):
            out.append(s)
    return out


def dedupe_blocks(blocks: list[str]) -> list[str]:
    """去重但**保序**：同一段话在多个来源页里重复出现是常态。"""
    seen, out = set(), []
    for b in blocks:
        key = b if len(b) < 120 else b[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


# ---------------------------------------------------------------- h3c 的 toc
_TOC_LINE = re.compile(r"^(\s*)-\s+(.*?)\s+\[(.+?)\]\s*$")


def load_h3c_toc(doc_root: str) -> dict:
    """读 h3c 的 `toc.txt` → {相对路径: 小节标题}。

    toc.txt 形如 `      - 1.3  使用命令行在线帮助  [01-基础配置指导/html/xxx.htm]`，
    缩进表示层级。有了它，正文就能配上**厂商自己的小节标题**，
    而不是我们瞎猜的标题 —— 这对"技术解释"的可信度很关键。
    """
    out: dict[str, str] = {}
    path = os.path.join(doc_root, "toc.txt")
    if not os.path.exists(path):
        return out
    for line in read_text(path).splitlines():
        m = _TOC_LINE.match(line)
        if not m:
            continue
        title, rel = m.group(2).strip(), m.group(3).strip()
        rel = rel.replace("\\", "/")
        # 只记第一层出现（同一文件可能被多个层级引用，先到先得即可）
        out.setdefault(rel.lower(), title)
    return out


def h3c_title_for(toc: dict, source: str) -> str:
    """source 形如 `h3c_cr16000/09-ACL和QoS配置指导/html/x.htm`；去掉厂商前缀再查。"""
    rel = source.split("/", 1)[1] if "/" in source else source
    return toc.get(rel.lower(), "")


# ---------------------------------------------------------------- ruijie 定位
#: 按**文件路径**缓存分行结果。
#: 为什么必须有：ruijie 的 source 是 `fulltext.txt#L614` —— **每个行号都是一个独立
#: source**（实测 5 个 feature 就有 909 个），而全文有 16 万行。不缓存的话每个 source
#: 都要 splitlines() 一遍全文，5 个 feature 就要 40 秒、整库要几十分钟。
#:
#: ⚠ **必须有上限**。旧实现是无上限的 `dict`，在 ruijie 全量上会吃光内存：
#:   锐捷 1657 份 fulltext 原始合计 3.41 GB，`splitlines()` 成 list[str] 后约 15~20 GB。
#:   实测（32 GB 机器，2026-10-06）：构建到第 137 个 feature 时进程已占 10.3 GB，
#:   且以 **+0.31 GB/分钟** 稳定增长，系统可用内存只剩 4.9 GB —— 会在构建中途 OOM。
#:   同一个 feature 的 source 通常集中在少数几份文档里，LRU 命中率足够。
_LINES_CACHE: "OrderedDict[str, list[str]]" = OrderedDict()
#: 缓存预算（原始字符数 + 每行 49 字节的 list/str 开销）。500M 字符 ≈ 0.5 GB 常驻。
_LINES_CACHE_MAX_CHARS = 500_000_000
_LINES_CACHE_CHARS = 0


def _lines_for(path: str) -> list[str]:
    global _LINES_CACHE_CHARS
    hit = _LINES_CACHE.get(path)
    if hit is not None:
        _LINES_CACHE.move_to_end(path)
        return hit
    lines = read_text(path).splitlines()
    _LINES_CACHE[path] = lines
    _LINES_CACHE_CHARS += sum(len(x) + 49 for x in lines)
    while _LINES_CACHE_CHARS > _LINES_CACHE_MAX_CHARS and len(_LINES_CACHE) > 1:
        _old, ol = _LINES_CACHE.popitem(last=False)
        _LINES_CACHE_CHARS -= sum(len(x) + 49 for x in ol)
    return lines


#: 每个 source 的**抽取结果**（正文块）缓存预算。
#:
#: ⚠ 这和 `_LINES_CACHE` 是**两个不同的缓存**，只给前者加 LRU 是不够的：
#:   `_lines_for` 缓存的是 `fulltext.txt` 的**行列表**（省读盘 + splitlines）；
#:   这里缓存的是 `extract_for_source()` 的**产物**（抽出来的正文块）。
#:   两者都随 feature 循环增长。
#:
#: 实测（32 GB 机器，2026-10-06）：只修了 `_LINES_CACHE` 之后重建 ruijie 技术层，
#:   进程到第 291 个 feature 仍占 **4.47 GB**，并以 **~10 MB/feature（≈0.12 GB/分钟）**
#:   线性增长（261 个 4.17 GB -> 291 个 4.47 GB）。按 ~900 个 feature 外推约 10.6 GB，
#:   而机器上其它进程已占 ~19 GB —— 仍会在中途 OOM。
#:   增长来源就是这个 cache：它把每个 source 抽出来的正文块**永久**留着。
_SRC_CACHE_MAX_CHARS = 200_000_000


def _src_cost(got: dict) -> int:
    """一个 source 抽取结果占的近似字符数（正文块 + 每块 49 字节的容器开销）。"""
    return sum(len(b) + 49 for b in (got.get("blocks") or ()))


def ruijie_window(path: str, line_no: int) -> list[str]:
    """ruijie 的 source 是 `fulltext.txt#L614` —— 取该行前后一段窗口。

    为什么不用"整篇"：fulltext 有 16 万行，整篇塞进每个命令不可用。
    窗口取 RUIJIE_WINDOW 行，并**向前回退到最近的章节标题**，避免从半句话开始。
    """
    lines = _lines_for(path)
    if not lines:
        return []
    i = max(0, min(len(lines) - 1, line_no - 1))
    start = max(0, i - RUIJIE_WINDOW)
    # 向前找最近的标题行（形如 `1.5   配置命令的执行权限` 或 `3. 配置步骤`）
    for j in range(i, start, -1):
        if re.match(r"^\s*\d+(\.\d+)*\s{1,4}\S", lines[j] or ""):
            start = j
            break
    end = min(len(lines), i + RUIJIE_WINDOW)
    out = []
    for line in lines[start:end]:
        s = re.sub(r"[ \t\u3000]+", " ", (line or "")).strip()
        if s and not _is_boilerplate(s):
            out.append(s)
    return out


# ---------------------------------------------------------------- 文档 → 正文块
def extract_for_source(source: str, doc_root: str, toc: dict) -> dict:
    """一条 source → {ok, title, blocks, reason}。任何失败都如实返回，不抛异常。"""
    out = {"ok": False, "title": "", "blocks": [], "reason": "", "path": ""}
    src = (source or "").strip()
    if not src:
        out["reason"] = "source 为空"
        return out

    # ruijie：#L<行号> 锚点
    line_no = 0
    m = re.search(r"#L(\d+)$", src)
    if m:
        line_no = int(m.group(1))
        src = src[: m.start()]

    path = os.path.join(doc_root, src.replace("/", os.sep))
    out["path"] = path
    if not os.path.exists(path):
        out["reason"] = "原文不存在"
        return out

    raw = read_text(path)
    if not raw.strip():
        out["reason"] = "原文为空"
        return out

    low = path.lower()
    if low.endswith((".htm", ".html", ".xhtml")):
        blocks = html_to_blocks(raw)
        out["title"] = h3c_title_for(toc, source) if toc else ""
    elif low.endswith(".txt"):
        # ruijie：按行号窗口取（走 _lines_for 的文件级缓存）
        blocks = ruijie_window(path, line_no) if line_no else html_to_blocks(raw)
    else:
        blocks = html_to_blocks(raw)

    blocks = dedupe_blocks(blocks)
    if not blocks:
        # huawei 有一批页只是目录页 —— 这里如实说明，别当成"没读到"
        out["reason"] = "该页没有正文（可能只是目录页）"
        return out
    # 权威标题（h3c 的 toc）优先；拿不到就从正文里猜一个（华为/ruijie 靠这个）
    if not out["title"]:
        out["title"] = _guess_title(blocks, src)
    out.update(ok=True, blocks=blocks)
    return out


#: 小节标题的形态：`1.3 配置ACL` / `3.5.2 配置步骤`（ruijie 的窗口首行就是它）
_HEADING_NUM = re.compile(r"^\s*\d+(\.\d+)*\s{1,4}\S")


def _guess_title(blocks: list, src: str) -> str:
    """没拿到权威小节标题时，从正文里**猜一个像样的**。

    为什么值得做：这个标题会写进 `tech/*.md` 的 `### ` 行，而那一行是
    `gen_tech_summary.select_prose()` 分节的**唯一依据**，也是模型读文档的"地图"。
    没有它，ruijie 的节标题只能是 `fulltext.txt#L63441`、华为只能是 `feature-specific_40.html`
    —— 模型看到这种标题等于没看到。h3c 有 `toc.txt` 所以本来就有真标题，
    这里主要是给华为（html 文件名）和 ruijie（行号锚点）补。

    只认"确定像标题"的：编号开头且短，或首行本身很短、不带句末标点。
    """
    for b in (blocks or [])[:3]:
        s = (b or "").strip()
        if s and len(s) <= 60 and _HEADING_NUM.match(s):
            return s
    s = (blocks or [""])[0].strip() if blocks else ""
    if s and len(s) <= 40 and not s.endswith(("。", "，", "；", "：", ".", ",", ";")) \
            and s.count(" ") < 6:
        return s
    return ""


# ---------------------------------------------------------------- 术语倒排
_TERM_SPLIT = re.compile(r"[\s/、,，;；()（）\[\]【】:：]+")
_STOP = {"配置", "命令", "使用", "功能", "简介", "概述", "显示", "维护", "管理", "和", "与", "的"}


def terms_of(*texts: str) -> set[str]:
    """粗分词（不引第三方库）：中文按 2-4 字滑窗 + 英文/数字整词。

    为什么不上 jieba：本层要的是**检索召回**而不是语言学正确，滑窗足够，
    还能避免给引擎加依赖（引擎跑在用户的 Python 上，装包会失败）。
    """
    bag: set[str] = set()
    for t in texts:
        for chunk in _TERM_SPLIT.split(t or ""):
            chunk = chunk.strip()
            if not chunk:
                continue
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9._\-]*", chunk):
                bag.add(chunk.lower())
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
                n = len(chunk)
                for size in (2, 3, 4):
                    for i in range(0, max(0, n - size + 1)):
                        g = chunk[i:i + size]
                        if g not in _STOP:
                            bag.add(g)
                if 2 <= n <= 6:
                    bag.add(chunk)
    return bag


# ---------------------------------------------------------------- 主流程
def slugify(name: str) -> str:
    """feature → 文件名。保留中英文数字，其余换成 `-`。"""
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", str(name or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "unnamed"


#: Windows 普通 Win32 路径上限 260；留余量取 240（与 `_longpath` 同阈值）。
_WIN_PATH_BUDGET = 240


def _safe_fname(out_root: str, vendor: str, slug: str) -> tuple[str, bool]:
    """`slug` → 相对 `out_root/vendor` 的文件名（`tech/<...>.md`），必要时截断。

    为什么（2026-10-10 加，踩过，代价很大）
    ======================================
    本机 `LongPathsEnabled=0`，普通路径 >260 直接 `FileNotFoundError`。
    `OUT_ROOT/<vendor>/tech` 本身就有 81 字符
    （`...\\aiterminal\\knowledge\\huawei\\tech`），而 feature 名可以很长 ——
    华为 AR 系列把几十个型号串进一个 feature（实测
    `安装AR611E-S-AR651-…-AR6121EC-S路由器` 的 slug 184 字符 → 完整路径 270）。

    原来这里是**裸** `io.open()`：一抛就冒泡出 `main()` → **整个技术层构建
    rc=1 中断**（实测只写了 5271/5835 个文件）；而
    `rebuild_all_knowledge.main()` 无论子构建 rc 一律 `return 0` → 上层判
    "成功" → 照常训练、purge、**把源包删了**。纯静默丢内容，最难发现的一类。

    做法：路径过长就**截断文件名 + 加 6 位哈希**（`hashlib.md5`，确定性、
    不同长名不会互相覆盖）。**不用 `\\\\?\\` 前缀** —— 那只是把失败推给下游
    读取方（app 用普通路径读照样挂），本项目约定不这么干。

    返回 `(文件名, 是否被截断)`。
    """
    fname = "tech/%s.md" % slug
    if os.name != "nt":
        return fname, False
    stem = len(os.path.join(out_root, vendor, "tech")) + 1      # +1 = 分隔符
    if stem + len(slug) + len(".md") <= _WIN_PATH_BUDGET:
        return fname, False
    room = max(1, _WIN_PATH_BUDGET - stem - len(".md") - 7)     # 7 = "~" + 6 位哈希
    tail = hashlib.md5(slug.encode("utf-8")).hexdigest()[:6]
    return "tech/%s~%s.md" % (slug[:room], tail), True


def load_records(vendor: str) -> list[dict]:
    path = os.path.join(CMD_ROOT, vendor, "commands.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def build_vendor(vendor: str, limit: int = 0, dry_run: bool = False,
                 verbose: bool = True) -> dict:
    t0 = time.time()
    records = load_records(vendor)
    if not records:
        return {"vendor": vendor, "error": "找不到 %s/commands.jsonl" % vendor}

    toc = {}
    if vendor in TOC_VENDORS:
        # 合并所有 `<vendor>_*` 的 toc（cr16000 / s12500 各有一份，小节标题基本一致，
        # 先到先得即可 —— 目的是给正文配上**厂商自己的小节标题**，不是做唯一权威映射）
        # cisco / juniper 走同一条路：它们的抓取产物也是 `<章>/html/<节>.htm` + toc.txt。
        prefix = vendor + "_"
        for name in sorted(os.listdir(DOC_ROOT)) if os.path.isdir(DOC_ROOT) else []:
            if name.startswith(prefix) and os.path.exists(
                    os.path.join(DOC_ROOT, name, "toc.txt")):
                for k, v in load_h3c_toc(os.path.join(DOC_ROOT, name)).items():
                    toc.setdefault(k, v)
    if verbose and toc:
        print("    %s toc: %d 个小节标题" % (vendor, len(toc)))

    # feature → {sources: [...], commands: [...]}
    feats: "OrderedDict[str, dict]" = OrderedDict()
    #: 命令 → 该命令的**所有**元数据变体（同一条命令可能有多个型号的记录，
    #: 语法详略不同）。先全收着，等知道它是不是通用命令之后再挑用哪一份 ——
    #: 专有命令取"最完整"的语法，通用命令取"最短"的（见下面的选择规则）。
    cmd_variants: dict[str, list[dict]] = {}
    for rec in records:
        feat = str(rec.get("feature") or "未分类").strip() or "未分类"
        node = feats.setdefault(feat, {"sources": [], "commands": []})
        src = str(rec.get("source") or "").strip()
        if src and src not in node["sources"]:
            node["sources"].append(src)
        cmd = str(rec.get("command") or "").strip()
        if cmd:
            cand = {"command": cmd,
                    "view": str(rec.get("view") or ""),
                    "syntax": str(rec.get("syntax") or ""),
                    "model": str(rec.get("model") or "")}
            lst = cmd_variants.setdefault(cmd.lower(), [])
            if cand not in lst:
                lst.append(cand)
        if cmd and rec.get("kind") == "command" and cmd.lower() not in node["commands"]:
            node["commands"].append(cmd)

    names = sorted(feats)
    if limit:
        names = names[:limit]
    if verbose:
        print("    %d 个 feature（本次处理 %d 个），%d 条命令记录"
              % (len(feats), len(names), len(records)))

    out_dir = os.path.join(OUT_ROOT, vendor, "tech")
    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)

    index = {"vendor": vendor, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "doc_root": DOC_ROOT, "cmd_root": CMD_ROOT,
             "n_features": 0, "n_commands": 0, "truncated_features": [],
             "features": {}, "commands": {}, "terms": {}}
    terms: dict[str, list[str]] = defaultdict(list)
    stats = {"ok_sources": 0, "miss_sources": 0, "no_prose": 0, "truncated": 0}
    #: source -> 抽取结果。**必须有上限** —— 见 `_SRC_CACHE_MAX_CHARS` 上方注释。
    cache: "OrderedDict[str, dict]" = OrderedDict()
    cache_chars = 0
    #: 命令 → 它出现过的 feature 列表（**去重保序**）。用来判定"通用命令"。
    cmd_feats: dict[str, list[str]] = {}
    #: 同上，但**剔掉"桶"型 feature**（`未分类` / `*命令` / `*参考` …）。
    #: 判"通用命令"只看这一份 —— 见 `_is_bucket_feature` 的说明。
    cmd_feats_real: dict[str, list[str]] = {}

    for i, feat in enumerate(names, 1):
        node = feats[feat]
        parts, used_sources = [], []
        #: 跨来源全局去重。ruijie 尤其需要：同一份 fulltext.txt 里相邻行号的窗口高度重叠，
        #: 不去重的话同一个 feature 里同一段话会重复几十遍（实测 5 个 feature 命中 909 个 source）。
        seen_blocks: set[str] = set()
        for src in node["sources"]:
            got = cache.get(src)
            if got is None:
                got = extract_for_source(src, DOC_ROOT, toc)
                cache[src] = got
                cache_chars += _src_cost(got)
                while cache_chars > _SRC_CACHE_MAX_CHARS and len(cache) > 1:
                    _, _old_got = cache.popitem(last=False)
                    cache_chars -= _src_cost(_old_got)
            else:
                cache.move_to_end(src)
            if not got["ok"]:
                if "不存在" in got["reason"]:
                    stats["miss_sources"] += 1
                else:
                    stats["no_prose"] += 1
                continue
            stats["ok_sources"] += 1
            fresh = []
            for b in got["blocks"]:
                key = b if len(b) < 120 else b[:120]
                if key in seen_blocks:
                    continue
                seen_blocks.add(key)
                fresh.append(b)
            if not fresh:
                continue                       # 整段都和前面重复 → 这个来源不必再列
            used_sources.append(src)
            title = got["title"] or os.path.basename(src)
            parts.append("### %s\n\n<!-- source: %s -->\n" % (title, src))
            parts.append("\n".join(fresh))

        body = "\n\n".join(p for p in parts if p.strip())
        truncated = False
        if len(body.encode("utf-8")) > MAX_FEATURE_BYTES:
            # 按字符粗切到上限，并**如实标注**（项目约定：截断必须可见）
            keep = MAX_FEATURE_BYTES // 3
            body = body[:keep] + "\n\n> ⚠ 本 feature 正文超过 %d KB，已截断；" \
                                  "完整内容见上方 source 原文。\n" % (MAX_FEATURE_BYTES // 1024)
            truncated = True
            stats["truncated"] += 1
            index["truncated_features"].append(feat)

        fname, _long = _safe_fname(OUT_ROOT, vendor, slugify(feat))
        if _long:
            stats["long_name"] = stats.get("long_name", 0) + 1
        written = True
        if not dry_run:
            head = (
                "# %s · %s\n\n"
                "> 本文件由 `tools/build_tech_docs.py` 从**厂商原始文档**用代码提取，"
                "未经 AI 改写。\n"
                "> 提取不到正文时**留空并说明**，不做任何补写。\n\n"
                "- 厂商：%s\n- 功能分类：%s\n- 命令条数：%d\n- 来源文档：%d 个\n"
                "- 生成时间：%s\n\n---\n\n"
            ) % (vendor.upper(), feat, vendor, feat, len(node["commands"]),
                 len(used_sources), index["generated_at"])
            #: ⚠ 单个文件写不进去**绝不能**让整批失败（2026-10-10 加，踩过）。
            #: 上面的 `_safe_fname()` 已经处理了已知成因 MAX_PATH；这里兜住其余
            #: （被占用 / 只读 / 权限）。同 `hdx_to_tech.py` 的既定做法：
            #: `try/except OSError` 跳过 + 计数。**再也不能有一个文件把整个技术层
            #: 带崩** —— 那会让上层误判"重建成功"，进而把源包删掉。
            try:
                with io.open(os.path.join(OUT_ROOT, vendor, fname), "w",
                             encoding="utf-8") as fh:
                    fh.write(head)
                    if body.strip():
                        fh.write(body.strip() + "\n")
                    else:
                        fh.write("_（本 feature 在原始文档里没有提取到讲解正文；"
                                 "只有下面的命令字段）_\n")
                    if node["commands"]:
                        fh.write("\n\n---\n\n## 本分类下的命令\n\n")
                        for c in node["commands"]:
                            fh.write("- `%s`\n" % c)
            except OSError as e:
                written = False
                stats["write_fail"] = stats.get("write_fail", 0) + 1
                index.setdefault("unwritten_features", []).append(feat)
                if verbose:
                    print("      ⚠ 写不了 %s（%s）" % (fname, type(e).__name__))

        nbytes = len(body.encode("utf-8"))
        entry = {
            "file": fname, "commands": node["commands"], "sources": used_sources,
            "bytes": nbytes, "truncated": truncated,
        }
        if not written:
            #: 只在失败时出现。`verify_knowledge_read.py` / 体检脚本可据此告警。
            entry["written"] = False
        index["features"][feat] = entry
        for c in node["commands"]:
            flist = cmd_feats.setdefault(c.lower(), [])
            if feat not in flist:
                flist.append(feat)
            if not _is_bucket_feature(feat):
                rlist = cmd_feats_real.setdefault(c.lower(), [])
                if feat not in rlist:
                    rlist.append(feat)
        for t in terms_of(feat) | terms_of(*node["commands"][:40]):
            if feat not in terms[t]:
                terms[t].append(feat)

        if verbose and (i % 20 == 0 or i == len(names)):
            print("      %3d/%3d  %s" % (i, len(names), feat))

    # ---- 命令索引：区分"专有命令"与"全局通用命令"
    n_generic = 0
    for name, flist in cmd_feats.items():
        rlist = cmd_feats_real.get(name) or []
        variants = cmd_variants.get(name) or []
        # 归属判定只用"真 feature"：桶名（`未分类`/`*命令`）人人都有，算进去等于灌水。
        generic = len(rlist) >= GENERIC_MIN_FEATURES
        if generic:
            n_generic += 1
            # 通用命令取**最短**语法：长语法往往是某个型号的特例（如 ruijie 的
            # `enable algorithm-type { md5 | sha256 }`），当用户敲 `enable` 时
            # 给他最短的基础形式才对；专有命令反过来取最完整的。
            pool = [v for v in variants if v.get("syntax")] or variants
            pick = min(pool, key=lambda v: len(v.get("syntax", ""))) if pool else {}
        else:
            pool = variants or [{}]
            pick = max(pool, key=lambda v: len(v.get("syntax", "")))
        # 命令归属：优先真 feature；若一条命令只出现在桶里（元信息与书签都拿不到分类），
        # 退回列桶名 —— 如实呈现"它确实只能归到章节桶"，不编一个分类出来。
        home = (rlist or flist or [""])[0]
        index["commands"][name] = {
            "feature": "" if generic else home,
            "command": str(pick.get("command") or name),
            "view": str(pick.get("view") or ""),
            "syntax": str(pick.get("syntax") or ""),
            "model": str(pick.get("model") or ""),
            "generic": generic,
            "n_features": len(rlist) if rlist else len(flist),
            "features": flist[:GENERIC_FEATURE_LIST],
        }
    stats["generic_commands"] = n_generic

    index["n_features"] = len(index["features"])
    index["n_commands"] = len(index["commands"])
    index["terms"] = {k: v for k, v in sorted(terms.items()) if len(v) <= 40}
    index["stats"] = stats
    index["elapsed_s"] = round(time.time() - t0, 1)

    if not dry_run:
        with io.open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=1)

    if verbose:
        print("    → feature %d，命令 %d（其中通用 %d），正文来源命中 %d，缺失 %d，"
              "无正文 %d，截断 %d，%.1fs"
              % (index["n_features"], index["n_commands"], n_generic,
                 stats["ok_sources"], stats["miss_sources"], stats["no_prose"],
                 stats["truncated"], index["elapsed_s"]))
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description="从厂商原始文档提取技术讲解正文")
    ap.add_argument("--vendor", default="all", choices=list(VENDORS) + ["all"])
    ap.add_argument("--limit", type=int, default=0, help="每个厂商只处理前 N 个 feature（试跑用）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    a = ap.parse_args()

    print("命令元数据根 =", CMD_ROOT)
    print("原始文档根   =", DOC_ROOT)
    print("产物根       =", OUT_ROOT)
    if not os.path.isdir(DOC_ROOT):
        print("⚠ 原始文档根不存在 —— 先确认文档已解包到该目录")
        return 2

    vendors = list(VENDORS) if a.vendor == "all" else [a.vendor]
    rc = 0
    for v in vendors:
        print("\n=== %s ===" % v)
        res = build_vendor(v, limit=a.limit, dry_run=a.dry_run)
        if res.get("error"):
            print("    ✗", res["error"])
            rc = 1
        n_bad = len(res.get("unwritten_features") or [])
        if n_bad:
            #: 不因此返回非 0（单个文件失败不该让整批失败，见 MEMORY §2.10），
            #: 但必须**大声报出来** —— 它就是"内容静默缺失"的唯一痕迹。
            print("    ⚠ 有 %d 个 feature 的正文文件没写成功（见 tech/index.json 的 "
                  "unwritten_features）" % n_bad)
    return rc


if __name__ == "__main__":
    sys.exit(main())
