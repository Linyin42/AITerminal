#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华为 `.hdx`（eDoc 包）→ AITerminal 技术资料层。

为什么需要这个工具（而不是直接用 doc_to_commands.py）
==================================================
`doc_to_commands.py` 认的是**在线文档 / CHM 解包页**的标记：
`<table class="Table">` + `<span class="commandkeywords">`（新）或 `<b>`（老）。

实测（2026-10-05）把 ICSLite 的 27 个 hdx 全扫一遍：
**`<table class="Table">` 命令表页 = 0**。hdx 用的是另一套完全不同的、
而且**信息更全**的标记 —— 每个命令参考页在 `<meta>` 里自带结构化元数据：

    <meta name="DC.Type"      content="cliref">        ← 这一页是命令参考
    <meta name="DC.Title"     content="display bgp vpnv4 routing-table">
    <meta name="featurename"  content="BGP">
    <meta name="featuretype"  content="IP路由">

正文按 `<h2 class="sectiontitle">` 分节：命令功能 / 命令格式 / 参数说明 /
视图 / 缺省级别 / 使用指南 / 使用实例。命令关键字是
`<b><span class="cmdname">`，变量是 `<i><span class="varname">`。

所以**不能**硬套旧解析器（会静默产出 0 条命令，还不报错）——
本工具专门吃这套 hdx 标记。

编码坑：hdx 里的 html 声明 `charset=gb2312`，实际是 **GB18030**。
用 utf-8 读**不会报错**，只会静默产出乱码 —— 必须按 gb18030 解。

产物
====
1. `_vendor_docs/huawei_<slug>/`
       resources/**.html      正文页（只抽 html，跳过图片/脚本，省空间）
       toc.txt                `<hhc>` 书签 → 权威小节标题（h3c toc.txt 同格式）
       profile.xml            产品/版本元数据
2. `_vendor_cmd/huawei/hdx_commands.jsonl`
       {"vendor":"huawei","model":..,"feature":..,"kind":"command",
        "command":..,"syntax":..,"view":..,"source":"huawei_<slug>/resources/x.html"}

之后再跑（顺序不能反，见 doc_to_commands.py 的说明）：
    python tools/hdx_to_tech.py --dir <hdx目录> --append
    python tools/build_tech_docs.py --vendor huawei

用法
====
    python tools/hdx_to_tech.py --dir "D:/ICSLite_download/huawei_campus_hdx/hdx" --dry-run
    python tools/hdx_to_tech.py --hdx "D:/.../S380_V600R025C10_02_zh_AZQ0331F.hdx"
    python tools/hdx_to_tech.py --dir <目录> --append      # 合并进 commands.jsonl（自动备份）
"""
from __future__ import annotations

import argparse
import glob
import html as html_mod
import io
import json
import os
import re
import shutil
import sys
import time
import zipfile

DOC_ROOT = os.environ.get("AIT_VENDOR_DOC_ROOT") or os.path.join(
    os.path.expanduser("~"), "deepseek_harness", "_vendor_docs")
CMD_ROOT = os.environ.get("AIT_VENDOR_CMD_ROOT") or os.path.join(
    os.path.expanduser("~"), "deepseek_harness", "_vendor_cmd")

#: 抽出来的命令记录先落这里；`--append` 才合并进 commands.jsonl
STAGE_NAME = "hdx_commands.jsonl"

# ---------------------------------------------------------------- 解码 / 清洗
_META_RE = re.compile(r'<meta\s+name\s*=\s*"([^"]+)"\s+content\s*=\s*"([^"]*)"', re.I)
_H_RE = re.compile(r"<h([1-4])[^>]*>(.*?)</h\1>", re.S | re.I)
_SCRIPT_RE = re.compile(r"<(script|style|noscript|head)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\xa0]+")


def decode(raw: bytes) -> str:
    """hdx 的 html 是 GB18030。**不能**先试 utf-8 —— 它几乎不报错，只会给乱码。"""
    for enc in ("gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="replace")


def plain(frag: str) -> str:
    """HTML 片段 → 单行纯文本"""
    t = _SCRIPT_RE.sub(" ", frag or "")
    t = _TAG_RE.sub("", t)
    t = html_mod.unescape(t)
    return _WS_RE.sub(" ", t).replace("\n", " ").strip()


def slugify(text: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s,，]+", "_", str(text or "").strip().lower())
    return re.sub(r"_+", "_", s).strip("_") or "unnamed"


#: 这些 feature 名是"命令索引页"留下的噪声，不能当技术分类。
#: 实测：有的页 `<meta name="DC.Type" content="cliref">`，但其实是**命令索引页**
#: （DC.Title 就是一个动词 `display`，没有「命令格式」节）。它们会把 feature
#: 污染成 `display`/`undo`，还会让 `display stp` 这种正常命令被误判成"通用命令"
#: （因为同时出现在 MSTP / 以太网交换 / display 三个 feature 里）。
_DEGENERATE_FEATURES = {
    "display", "undo", "reset", "configure", "system-view", "interface", "cli",
    "命令", "参考", "命令行", "命令参考", "未分类",
}


def _sane_feature(feature: str, cmd: str, fallback: str) -> str:
    """把退化/自指的 feature 名换掉（先用 featuretype 兜底，再兜"未分类"）"""
    f = (feature or "").strip()
    if f.lower() in _DEGENERATE_FEATURES or f.lower() == (cmd or "").strip().lower():
        f = (fallback or "").strip()
    if not f or f.lower() in _DEGENERATE_FEATURES:
        return "未分类"
    return f


def _resolve_feature(feature: str, cmd: str, ftype: str, path) -> str:
    """元信息 → feature；元信息不可用（为空/退化）时退到**书签层级**，最后才兜「未分类」。"""
    got = _sane_feature(feature, cmd, ftype)
    if got != "未分类":
        return got
    return _feature_from_path(path) or "未分类"


# ---------------------------------------------------------------- profile / hhc
def read_profile(zf: zipfile.ZipFile) -> dict:
    names = [n for n in zf.namelist() if n.lower().endswith("profile.xml")]
    if not names:
        return {}
    raw = zf.read(names[0]).decode("utf-8", errors="replace")
    out = {}
    for tag in ("buildVersion", "libId", "libVersion", "libName", "productType",
                "productVersion", "issueDate", "language"):
        m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), raw, re.S)
        if m:
            out[tag] = html_mod.unescape(m.group(1).strip())
    return out


#: hhc 里一条书签：Name=标题，Local=相对 resources/ 的路径
_HHC_OBJ_RE = re.compile(r'<OBJECT[^>]*text/sitemap[^>]*>(.*?)</OBJECT>', re.S | re.I)
_HHC_NAME_RE = re.compile(r'name\s*=\s*"Name"\s+value\s*=\s*"([^"]*)"', re.I)
_HHC_LOCAL_RE = re.compile(r'name\s*=\s*"Local"\s+value\s*=\s*"([^"]*)"', re.I)


def read_bookmap(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """→ [(标题, hdx 内相对路径)]，保持书签顺序（即厂商的目录顺序）。"""
    hhc = [n for n in zf.namelist() if n.lower().endswith(".hhc")]
    if not hhc:
        return []
    raw = decode(zf.read(hhc[0]))
    out: list[tuple[str, str]] = []
    for body in _HHC_OBJ_RE.findall(raw):
        nm = _HHC_NAME_RE.search(body)
        lo = _HHC_LOCAL_RE.search(body)
        if not (nm and lo):
            continue
        local = html_mod.unescape(lo.group(1)).split("#")[0].strip().replace("\\", "/")
        if not local:
            continue
        if not local.lower().startswith("resources/"):
            local = "resources/" + local
        out.append((html_mod.unescape(nm.group(1)).strip(), local))
    return out


#: hhc 的 `<UL>/<LI>` 结构 —— 用它还原**书签层级**，而不只是"标题列表"。
_HHC_TOKEN_RE = re.compile(r"<UL>|</UL>|<LI>|</LI>|<OBJECT\b.*?</OBJECT>", re.S | re.I)


def read_bookmap_tree(zf: zipfile.ZipFile) -> dict[str, list[str]]:
    """→ {hdx 内相对路径(小写): [顶层书, 二级书, …, 本页标题]}

    为什么需要它：部分产品（实测 S200/S1700 V200R023C00、V200R024C00，
    S300/S500/S2700/S5700/S6700 同版本，S9300X V200R025C00 等）的 hdx 里
    `featurename`/`featuretype`/`product` **全是空的**，单看页面元信息只能得到
    「未分类」—— 实测这些包每个约 3070 条命令，五个包合起来 14866 条，
    全部堆进一个「未分类」桶，等于没整理。

    但 `.hhc` 书签是完整的树：`命令参考 › 接口管理命令 › 以太网接口配置命令 › <命令>`。
    取**二级**（`接口管理命令`）而不是三级，是因为二级书的名字与其它产品
    `featuretype` 给出的章节名（`IP单播路由命令` / `WLAN-AC命令` / `安全命令` …）
    同一套命名 —— 这样兜底出来的分类会**并进已有桶**，而不是另起一套把同一个
    命令拆到两个 feature 里（那会再次把 n_features 抬高、把正常命令误判成"通用命令"）。
    """
    hhc = [n for n in zf.namelist() if n.lower().endswith(".hhc")]
    if not hhc:
        return {}
    raw = decode(zf.read(hhc[0]))
    stack: list[str | None] = []
    out: dict[str, list[str]] = {}
    for m in _HHC_TOKEN_RE.finditer(raw):
        tok = m.group(0)
        up = tok.upper()
        if up == "<LI>":
            stack.append(None)
        elif up == "</LI>":
            if stack:
                stack.pop()
        elif up.startswith("<OBJECT"):
            nm = _HHC_NAME_RE.search(tok)
            lo = _HHC_LOCAL_RE.search(tok)
            if not (nm and lo):
                continue
            if stack and stack[-1] is None:
                stack[-1] = html_mod.unescape(nm.group(1)).strip()
            local = html_mod.unescape(lo.group(1)).split("#")[0].strip().replace("\\", "/")
            if not local:
                continue
            if not local.lower().startswith("resources/"):
                local = "resources/" + local
            out.setdefault(local.lower(), [x for x in stack if x])
    return out


#: 书签路径里的**导航栏**层：纯结构，不是分类。
_NAV_SECTIONS = {
    "文档指南", "文档约定", "文档说明", "了解产品", "安装", "配置", "参考",
    "二次开发", "运维与故障处理", "安全声明", "周边配套说明",
    "获取更多文档、视频和工具",
}
#: **书卷**（整本书 / 整本手册），不是功能分类。分类要从书卷里面再下一层取。
_VOLUME_EXACT = {
    "告警处理", "典型配置案例", "产品描述", "硬件描述", "安全加固与维护",
    "前言", "概述", "简介", "目录",
}
_VOLUME_CONTAINS = ("指南", "宝典", "手册")
_VOLUME_SUFFIX = ("参考", "产品文档", "描述", "白皮书")


def _is_nav(name: str) -> bool:
    return str(name or "").strip() in _NAV_SECTIONS


def _is_volume(name: str) -> bool:
    s = str(name or "").strip()
    if not s:
        return True
    if s in _VOLUME_EXACT:
        return True
    if any(k in s for k in _VOLUME_CONTAINS):
        return True
    return s.endswith(_VOLUME_SUFFIX)


def _feature_from_path(path: "list[str] | None") -> str:
    """书签路径 → feature 名（**只在页面元信息拿不到分类时**兜底）。

    路径末位是页面自己的标题，不算分类。取法是"跳过结构层，落到功能层"：
      1. 跳过开头的**导航栏**（`配置` / `参考` / `运维与故障处理` …）；
      2. 从那里起，取第一个**不是书卷**的节点 —— 它才是真正的功能分类；
      3. 若一路全是书卷（例：`运维与故障处理 › 告警处理 › <告警ID>`，
         告警条目直接挂在书卷下），则退回**最后一个书卷**，绝不能落到告警 ID 上。

    为什么不能简单取"第二层"：各产品包的书签树**深度不一样**。实测
      S200/S1700      `命令参考 › 接口管理命令 › …`            → 第二层就是分类
      S300/S9300X     `命令参考 › 基础配置命令 › …`            → 第二层是分类
      S770/S110       `参考 › 命令参考 › 基础配置命令 › …`      → 第二层还是书卷！
      S300 配置章     `配置 › 配置指南（命令行） › 以太网配置`   → 第二层是整本书！
    固定取第二层会把整本《配置指南（命令行）》和《告警处理》当成一个 feature
    （实测分别灌进 16841 / 10449 条），既超 `MAX_FEATURE_BYTES` 被截断，
    也等于没分类。
    """
    parts = [p.strip() for p in (path or []) if p and p.strip()]
    if len(parts) < 2:
        return parts[0] if parts else ""
    anc = parts[:-1]                                  # 去掉末位的"本页标题"
    i = 0
    while i < len(anc) - 1 and _is_nav(anc[i]):        # 1) 跳过导航栏
        i += 1
    for j in range(i, len(anc)):                       # 2) 第一个非书卷
        if not _is_volume(anc[j]):
            return anc[j]
    return anc[-1] if anc else ""                      # 3) 全是书卷 → 最后一个


# ---------------------------------------------------------------- 正文抽取
def sections_of(raw: str) -> list[tuple[str, str]]:
    """按 `<h2 class="sectiontitle">` 把正文切成 [(小标题, 该节 HTML)]"""
    marks = [(m.start(), m.end(), plain(m.group(2)))
             for m in _H_RE.finditer(raw)]
    if not marks:
        return []
    out = []
    for i, (_, end, title) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(raw)
        out.append((title, raw[end:nxt]))
    return out


def pick_section(secs: list[tuple[str, str]], *names: str) -> str:
    for title, body in secs:
        if title in names:
            return plain(body)
    return ""


def syntax_of(secs: list[tuple[str, str]], raw: str) -> str:
    """命令格式：优先 `cliformatbody`，退化成"命令格式"那一节。"""
    m = re.search(r'<div[^>]*class="[^"]*cliformatbody[^"]*"[^>]*>(.*?)</div>',
                  raw, re.S | re.I)
    if m:
        txt = plain(m.group(1))
        if txt:
            return txt
    return pick_section(secs, "命令格式")


#: 配置举例页里的命令块。**必须保留换行** —— 不能走 `plain()`（它会把换行压成空格，
#: 一段配置脚本会糊成一行，例子就没法看了）。
_PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S | re.I)
#: 举例页里"命令序列"所在的小节，按优先级：完整脚本 > 操作步骤
_STEP_SECTIONS = ("配置脚本", "操作步骤")


def steps_of(secs: list[tuple[str, str]]) -> list[str]:
    """配置举例页 → 命令序列（逐字，保留换行）。

    为什么单独做这一步：批次三的 `build_vendor_knowledge.py` 收例子时读的是记录里的
    `steps` 字段（命令序列列表）。hdx 抽出来的记录**原本没有这个字段**，于是
    7925 条 campus 产品的「配置举例」页在建库时全被判成"空例子"丢掉 ——
    实测 `examples` 表只剩 4 个 CHM 老文档的 13510 条，27 个产品一条都没有，
    界面里「厂家命令」查命令时也就永远看不到这些产品的配置举例。

    这些页的结构很规整（`组网需求 / 配置思路 / 操作步骤 / 检查配置结果 / 配置脚本`），
    命令都在 `<pre>` 里，直接按小节取 `<pre>` 即可 —— 不改写、不补写。
    """
    for name in _STEP_SECTIONS:
        for title, body in secs:
            if title != name:
                continue
            blocks = []
            for m in _PRE_RE.finditer(body):
                t = html_mod.unescape(_TAG_RE.sub("", m.group(1)))
                t = t.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
                if t.strip():
                    blocks.append(t)
            if blocks:
                return blocks
    return []


# ---------------------------------------------------------------- 单包处理
def process_one(path: str, dry_run: bool = False, verbose: bool = True,
                slug_suffix: str = "", zf=None, meta_override: dict | None = None) -> dict:
    """处理单个 hdx。

    `zf` / `meta_override` 为什么存在（2026-10-11 加，为华为 `.chm` 复用本函数）
    -------------------------------------------------------------------------
    `.chm`（ITSF 格式，**不是 zip**）要先 `7z x` 解成目录树，再当成"zip"喂进来。
    调用方（`hw_chm_to_tech.py`）传一个鸭子类型的目录 shim 作 `zf`，
    于是本函数**一个字都不用改**就能吃 chm。
    同时 chm 里**没有 `profile.xml`**，`productType`/`productVersion` 只能由
    下载计划（`hw_chm_plan.json`）提供 —— 走 `meta_override` 覆盖 `read_profile`。

    两个参数都默认 `None` ⇒ **hdx 侧行为逐字不变**。

    `slug_suffix` 为什么存在
    ------------------------
    slug 由 `productType + productVersion` 决定，但**这两项不唯一**。实测
    （2026-10-06，下载池）：

        WLAN_V600R025C01_01_zh_AZQ0326H.hdx  →  productType=WLAN,
            productVersion=V600R025C01, libName="WLAN AP AirEngine X700 产品文档"
        WLAN_V600R025C01_01_zh_AZQ0327S.hdx  →  productType=WLAN,
            productVersion=V600R025C01, libName="SOHO WLAN AP 产品文档"

    两者 slug 都是 `wlan_v600r025c01` → 同一个 `_vendor_docs/huawei_wlan_v600r025c01/`
    目录被两个包混写，而且两边记录里的 `source` 前缀相同，
    `append_records()` 按 source 去重时会把**第二个包的命令全部丢掉**（静默）。

    所以调用方在发现 slug 冲突时传一个判别串（一般用 `libId`）把两者分开。
    不冲突的包不传，slug 保持原样 —— 已处理过的 27 个产品的 doc_name 不受影响。
    """
    t0 = time.time()
    owns_zf = zf is None
    if owns_zf:
        try:
            zf = zipfile.ZipFile(path)
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "why": "不是有效 zip: %s" % str(e)[:60], "path": path}

    prof = read_profile(zf)
    if meta_override:
        #: chm 没有 profile.xml → 用下载计划的元数据补齐（空值不覆盖，保持 hdx 语义）
        for _k, _v in meta_override.items():
            if _v and not prof.get(_k):
                prof[_k] = _v
    ptype = prof.get("productType") or os.path.basename(path).split("_")[0]
    pver = prof.get("productVersion") or ""
    slug = slugify("%s_%s" % (ptype, pver)) if pver else slugify(ptype)
    if slug_suffix:
        slug = "%s_%s" % (slug, slugify(slug_suffix))
    doc_name = "huawei_" + slug
    model = ("%s-%s" % (ptype, pver)) if pver else ptype

    book = read_bookmap(zf)
    title_by_file = {}
    for title, rel in book:
        title_by_file.setdefault(rel.lower(), title)
    #: 页面路径 → 书签层级（元信息缺分类时的兜底，见 read_bookmap_tree）
    paths = read_bookmap_tree(zf)

    htmls = [n for n in zf.namelist() if n.lower().endswith((".html", ".htm"))]
    dest_root = os.path.join(DOC_ROOT, doc_name)

    recs: list[dict] = []
    n_cliref = n_prose = n_example = n_skip = 0
    for name in htmls:
        try:
            raw = decode(zf.read(name))
        except Exception:                                        # noqa: BLE001
            n_skip += 1
            continue
        meta = {k.lower(): v for k, v in _META_RE.findall(raw)}
        src = "%s/%s" % (doc_name, name.replace("\\", "/"))
        dctype = (meta.get("dc.type") or "").lower()
        # feature 归属：featurename 优先（与既有 tech 层的命名一致），退化 featuretype，
        # 两者都没有（部分老版本 hdx 的 product/featurename 全是空）→ 用书签层级兜底。
        # ⚠ 原来这里还有一层 `title_by_file[...].split(" ")[0]` 兜底 —— 对命令页它等于
        #   命令的第一个词（`display`），对正文页它等于整页标题，两个都是垃圾 feature 名。
        fname = (meta.get("featurename") or "").strip()
        ftype = (meta.get("featuretype") or "").strip()
        feature = (fname or ftype or "").strip().replace("/", "、")[:60]
        toc_path = paths.get(name.lower())

        if dctype == "cliref":
            secs = sections_of(raw)
            cmd = (meta.get("dc.title") or "").strip()
            if not cmd:
                m = re.search(r'<h1[^>]*>(.*?)</h1>', raw, re.S | re.I)
                cmd = plain(m.group(1)) if m else ""
            syntax = syntax_of(secs, raw)
            # ⚠ 真正的命令参考页一定有「命令格式」节。没有 = 命令索引页（DC.Type
            #   也被写成 cliref），必须丢掉，否则 feature 会被污染成 `display`，
            #   还会把正常命令误判成"通用命令"。
            if not cmd or not syntax:
                n_skip += 1
                continue
            recs.append({
                "vendor": "huawei", "model": model,
                "feature": _resolve_feature(feature, cmd, ftype, toc_path),
                "kind": "command", "command": cmd,
                "syntax": syntax,
                "view": pick_section(secs, "视图", "命令视图", "命令支持的视图"),
                "source": src,
            })
            n_cliref += 1
            continue

        # 非 cliref：只留**有正文**的页（过滤纯目录/索引页）
        body = _SCRIPT_RE.sub(" ", raw)
        body = plain(body)
        if len(body) < 300:
            n_skip += 1
            continue
        # 日志/告警参考页不是"技术解释"：标题就是告警名（如 MDOMU/3/OMU_CFG_RESTORE_APP_FAILED），
        # feature 会变成告警 ID —— 实测这类页把 feature 数从几千撑到两万多。丢掉。
        if "日志信息" in body and "日志含义" in body:
            n_skip += 1
            continue
        title = title_by_file.get(name.lower()) or (meta.get("dc.title") or "").strip()
        rec = {
            "vendor": "huawei", "model": model,
            "feature": _resolve_feature(feature, "", ftype, toc_path),
            "title": title, "source": src,
        }
        # 配置举例页：抽出命令序列（`steps`）。抽不到就当普通正文页 ——
        # `kind=example` 的语义就是"这条有可用的命令序列"，与 CHM 那条流水线一致。
        steps = steps_of(sections_of(raw)) if ("配置举例" in body or "配置示例" in body) else []
        if steps:
            rec["kind"] = "example"
            rec["steps"] = steps
            n_example += 1
        else:
            rec["kind"] = "doc"
        recs.append(rec)
        n_prose += 1

    # 落盘：html + toc.txt + profile.xml
    if not dry_run:
        os.makedirs(dest_root, exist_ok=True)
        n_new = n_keep = n_toolong = 0
        for name in htmls:
            tgt = os.path.join(dest_root, name.replace("/", os.sep))
            #: ⚠ 单个文件写不进去**绝不能**让整包失败（2026-10-09 修，踩过）。
            #: Windows 的 MAX_PATH 是 260（本机 `LongPathsEnabled=0`），而华为
            #: `NetEngine 8000 F1A / M14K / M6K` 这三个包的 `resources/log/**` 下
            #: 有 51 个告警页，完整路径最长 **261** 字符 → `open(tgt,"wb")` 抛
            #: `FileNotFoundError`（实测：255 字符可以、260 失败）。
            #: 原来这一抛会冒泡出 `process_one` → **整包 4 万条记录全丢**，
            #: 而且 `hdx_batch_fill` 因此返回 1 → `hdx_pipeline_supervisor` 立刻
            #: "停止（不重建、不清理）" → 整条华为链永远走不到训练和清空。
            #: 这些页本来就是 `resources/log/**` 的告警参考页（取记录阶段已按
            #: "日志信息/日志含义" 丢掉），落盘只是顺手复制，跳过无实质影响。
            try:
                os.makedirs(os.path.dirname(tgt), exist_ok=True)
            except OSError:
                n_toolong += 1
                continue
            # 幂等：已存在且大小一致就跳过。重跑时既省时间，也避免在"被占用 /
            # 只读"的既有文件上做无谓覆盖写（实测重跑会 PermissionError）。
            try:
                if os.path.exists(tgt) and \
                        os.path.getsize(tgt) == zf.getinfo(name).file_size:
                    n_keep += 1
                    continue
            except OSError:
                pass
            try:
                with zf.open(name) as s, open(tgt, "wb") as d:
                    shutil.copyfileobj(s, d, 1 << 20)
            except OSError:
                # 路径超长 / 被占用 / 只读 → 跳过这一个，整包继续
                n_toolong += 1
                continue
            n_new += 1
        if verbose and (n_new or n_keep or n_toolong):
            print("       落盘 html：新写 %d，已存在跳过 %d，跳过写不了 %d"
                  % (n_new, n_keep, n_toolong))
        with io.open(os.path.join(dest_root, "toc.txt"), "w",
                     encoding="utf-8") as fh:
            for title, rel in book:
                fh.write("- %s  [%s]\n" % (title, rel))
        for nm in zf.namelist():
            if nm.lower().endswith("profile.xml"):
                with open(os.path.join(dest_root, "profile.xml"), "wb") as d:
                    d.write(zf.read(nm))
                break
    if owns_zf:
        zf.close()

    if verbose:
        print("  %-46s html=%-6d 命令=%-5d 正文=%-6d 举例=%-5d 跳过=%-6d %.1fs"
              % (doc_name[:46], len(htmls), n_cliref, n_prose, n_example, n_skip,
                 time.time() - t0))
    return {"ok": True, "doc_name": doc_name, "model": model, "slug": slug,
            "product": ptype, "version": pver, "libName": prof.get("libName", ""),
            "n_html": len(htmls), "n_cliref": n_cliref, "n_prose": n_prose,
            "n_example": n_example,
            "n_skip": n_skip, "n_bookmarks": len(book),
            "records": recs, "dest": dest_root}


# ---------------------------------------------------------------- 合并
def append_records(recs: list[dict]) -> tuple[str, int]:
    """把新记录合并进 `_vendor_cmd/huawei/commands.jsonl`（先备份，按 source 去重）。"""
    out_dir = os.path.join(CMD_ROOT, "huawei")
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, "commands.jsonl")
    existing_sources = set()
    if os.path.exists(target):
        with io.open(target, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    src = json.loads(line).get("source")
                except ValueError:
                    continue
                if src:
                    existing_sources.add(src)
        bak = target + ".bak_" + time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(target, bak)
    else:
        bak = "(无原文件)"
    new = [r for r in recs if r.get("source") not in existing_sources]
    with io.open(target, "a", encoding="utf-8") as fh:
        for r in new:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return bak, len(new)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="华为 hdx → AITerminal 技术资料层")
    ap.add_argument("--hdx", action="append", default=[], help="单个 .hdx（可重复）")
    ap.add_argument("--dir", help="hdx 所在目录（取目录下所有 .hdx）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几个包")
    ap.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    ap.add_argument("--append", action="store_true", help="合并进 commands.jsonl")
    ap.add_argument("--out-stage", help="暂存 jsonl 路径（默认 tools/hdx_commands.jsonl）")
    args = ap.parse_args()

    files = list(args.hdx)
    if args.dir:
        files += sorted(glob.glob(os.path.join(args.dir, "*.hdx")))
    files = [f for f in files if f.lower().endswith(".hdx")]
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("没有可处理的 .hdx（用 --hdx 或 --dir 指定）")
        return 2

    print("待处理 %d 个 hdx | DOC_ROOT=%s" % (len(files), DOC_ROOT))
    print("-" * 100)
    all_recs, summary = [], []
    for i, f in enumerate(files, 1):
        print("[%d/%d] %s" % (i, len(files), os.path.basename(f)[:70]))
        r = process_one(f, dry_run=args.dry_run)
        if not r.get("ok"):
            print("   ❌ %s" % r.get("why"))
            continue
        all_recs.extend(r["records"])
        r.pop("records")
        summary.append(r)

    print("-" * 100)
    print("产品 %d 个 | 命令记录 %d 条 | 正文记录 %d 条"
          % (len(summary), sum(s["n_cliref"] for s in summary),
             sum(s["n_prose"] for s in summary)))

    if args.dry_run:
        print("（--dry-run，未落盘）")
        return 0

    stage = args.out_stage or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), STAGE_NAME)
    try:
        with io.open(stage, "w", encoding="utf-8") as fh:
            for r in all_recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("暂存 → %s（%d 条）" % (stage, len(all_recs)))
    except OSError as e:
        # 暂存只是方便回看，写不了不影响主流程（--append 才是真正要做的）
        print("⚠ 暂存写入失败（忽略）：%s" % e)

    if args.append:
        bak, n = append_records(all_recs)
        print("已合并 %d 条进 commands.jsonl（备份：%s）" % (n, bak))
    else:
        print("未合并（加 --append 才写入 commands.jsonl）")

    try:
        json.dump(summary, open(stage + ".summary.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
