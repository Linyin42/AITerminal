#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从各厂商官网抓取产品文档，落到 `_vendor_docs/` 的**既有契约**上，
接上 `build_tech_docs.py` → `knowledge/<vendor>/tech/` 那条已经跑通的流水线。

为什么要有两个后端（这是本工具的核心）
======================================
实测（2026-10-04，逐厂商跑过，数据见下表）：

| 厂商 | 纯 HTTP 抓 | 无头 Chrome 渲染 | 正文容器 |
|---|---|---|---|
| H3C    | ✅ 100–412 KB/页，真中文正文 | 不需要 | `documentContent` |
| Cisco  | ⛔ **403**（连 cisco.com 首页都 403） | ✅ **绕过**，186 表 / 617 行 / refbody 4.3 万字 | `refbody` |
| Juniper| ⛔ 404 / JS 空壳 | ✅ SPA 渲染后 107,650 字 | `refbody` |
| 锐捷    | ⛔ 543 KB 全是导航壳（正文 0） | ⚠ 只拿到目录，深页未通 | `pwmDoc`（待定） |
| 华为    | ⛔ 377 字节 JS 壳 | ⚠ 需正确的 EDOC docid | `main-container`（待定） |

两条结论：
1. **纯 HTTP 只对 H3C 成立**。Cisco 的 403 是**边缘节点拦截**（首页也 403，
   不是 UA/头问题，换全套 Sec-Fetch 头一样 403）；Juniper 是 SPA。
2. **无头 Chrome 能同时解决这两类**：既绕开 403（Chrome 的 TLS/头指纹被放行），
   又能把 SPA 渲染成 DOM。所以 `render` 后端是必需的，不是锦上添花。

⚠ 用 Chrome 的 `--dump-dom` 而不是装 playwright：
   `%LOCALAPPDATA%/ms-playwright/chromium-*/chrome-win64/chrome.exe` 通常**已经存在**
   （Playwright 装过就有），`--dump-dom` 直接输出渲染后的 DOM，**零依赖**。
   注意是 `chrome-win64`（不是 `chrome-win`），headless shell 在
   `chromium_headless_shell-*/chrome-headless-shell-win64/`。

产物契约（与既有 `_vendor_docs/h3c_s12500` 完全一致）
====================================================
    _vendor_docs/<out_name>/
        toc.txt                       `- 标题  [相对路径]`，2 空格缩进
        <章名>/html/<小节标题>.htm     正文页（UTF-8）
        _fetch_manifest.json          抓取记录（url/字节/sha256/后端）

⚠ `toc.txt` 的标题是**权威标题**（`build_tech_docs.py` 靠它给正文配小节名）。
  抓不到标题时它只能从正文里猜，猜出来的标题往往等于没标题 —— 所以 toc 必须写好。

用法
====
    # 探测各厂商可达性（会打印上面那张表）
    python tools/fetch_vendor_docs.py probe

    # H3C（纯 HTTP，快）
    python tools/fetch_vendor_docs.py fetch --vendor h3c \
        --index-url https://www.h3c.com/cn/.../Operation_Manual/... --out-name h3c_s5100ei

    # Cisco / Juniper（无头渲染，慢，默认每页 8–12 秒）
    python tools/fetch_vendor_docs.py fetch --vendor cisco \
        --index-url https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/interface/command/ir-cr-book.html \
        --out-name cisco_isr --limit 5

    # 人工下好的门控文档（华为/锐捷的包）归类
    python tools/fetch_vendor_docs.py inbox --dir ~/Downloads/vendor_docs

    # 验证「文档 → 知识库」这一半通了
    python tools/fetch_vendor_docs.py verify --vendor h3c --query "acl number"
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
VENDOR_DOCS = os.path.expanduser("~/deepseek_harness/_vendor_docs")


# ============================================================ 无头 Chrome 后端
def find_chrome() -> str:
    """找可用的 Chromium。优先 Playwright 自带的，其次系统 Chrome/Edge。"""
    pats = [
        os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-*\chrome-win64\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-*\chrome-win\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for p in pats:
        hits = sorted(glob.glob(p), reverse=True)
        if hits:
            return hits[0]
    return ""


CHROME = find_chrome()


def render_get(url: str, budget: int = 9000, timeout: int = 120) -> str:
    """用无头 Chrome 渲染后取 DOM。SPA 和 403 拦截都靠它。

    ⚠ `--virtual-time-budget` 是必需的：不给的话 Chrome 会在 JS 跑完前就 dump，
      拿到的还是空壳（实测华为页 377 字节 vs 给预算后 82 KB）。
    """
    if not CHROME:
        raise RuntimeError("找不到 Chromium/Chrome，render 后端不可用")
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--disable-dev-shm-usage", "--hide-scrollbars",
           # ⚠ 必须显式带真实 UA：headless 的默认 UA 里含 "HeadlessChrome"，
           #   会被 CloudFront 之类的 WAF 拦成
           #   "ERROR: The request could not be satisfied"（实测 HPE 新站
           #   networkingdocs.hpe.com，真实浏览器可访问、无头直接被拒）。
           "--user-agent=%s" % UA,
           "--virtual-time-budget=%d" % budget, "--dump-dom", url]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return p.stdout.decode("utf-8", "replace")


# ============================================================ HTTP 后端
def http_get(url: str, timeout: int = 45, referer: str = "") -> tuple:
    """返回 (status, bytes)。不抛异常，把 HTTP 错误码原样交回给调用方判断。"""
    hdr = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        hdr["Referer"] = referer
    req = urllib.request.Request(url, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:                                     # noqa: BLE001
        return None, str(e).encode()


# ============================================================ 厂商适配器
class Adapter:
    """一个厂商的抓取规则。

    link_re  —— 目录页里"哪些 <a href> 是正文页"。必须够窄，
                否则会把导航/语言切换/法务页全抓回来（实测锐捷 543 KB 里 99% 是导航）。
    content —— 正文容器的 class 名。保存时只留这个容器，
               不然后续 `build_tech_docs.py` 读到的正文里混着几百 KB 导航。
    render  —— 是否必须走无头 Chrome。
    """

    def __init__(self, vendor, base, link_re, content, render, referer="",
                 title_re=None, note=""):
        self.vendor = vendor
        self.base = base
        self.link_re = re.compile(link_re, re.I)
        #: 正文容器 class，**可以给多个**，按顺序试第一个有内容的。
        #: 实测 Juniper 同一站不同页用的容器不一样（refbody / topicBody / topic-content），
        #: 写死一个会在部分页上静默取到空 → 落盘整个 DOM（含导航）→ 提不出命令。
        self.content = (content,) if isinstance(content, str) else tuple(content)
        self.render = render
        self.referer = referer
        self.title_re = re.compile(title_re, re.S | re.I) if title_re else None
        self.note = note


ADAPTERS = {
    # ---- H3C：唯一纯 HTTP 可抓的（已验证）
    "h3c": Adapter(
        "h3c", "https://www.h3c.com",
        r'href="(/cn/d_\d{6}/\d+_\d+_\d+\.htm)"',
        content="documentContent", render=False,
        referer="https://www.h3c.com/",
        title_re=r"<title>(.*?)</title>",
        note="纯静态 HTML，每页 100–412 KB 真中文正文"),

    # ---- Cisco：403 边缘拦截，必须渲染
    "cisco": Adapter(
        "cisco", "https://www.cisco.com",
        r'href="(/c/en/us/td/docs/[^"]+\.html)"',
        content="refbody", render=True,
        referer="https://www.cisco.com/",
        title_re=r"<title>(.*?)</title>",
        note="HTTP 403（含首页）；Chrome 渲染后 186 表/617 行"),

    # ---- Juniper：SPA，必须渲染
    # ⚠ 链接正则**必须收窄到 topics/ref/command|statement**。
    #   写成 `/documentation/us/en/software/[^"]+\.html` 会把"书级落地页"
    #   （Junos CLI Reference / License Guide）也抓进来 —— 那些页 refbody=0、
    #   正文全是导航，实测抓回 2.7 MB 却提不出 1 条命令。
    # ⚠ 同时**不能要求 href 以 /documentation/ 开头**：主题清单页里用的是
    #   `../../ref/command/xxx.html` 这类**相对**链接，写死绝对前缀会一条都匹配不到
    #   （实测：CLI Commands 主题清单页 13,599 节点 → 解析出 0 个正文页）。
    #   相对链接由 urljoin(目录页 URL, href) 解析，正好落到
    #   …/cli-reference/topics/ref/command/xxx.html —— 就是正文页的真实路径。
    "juniper": Adapter(
        "juniper", "https://networkingdocs.hpe.com",
        r'href="([^"]*topics/ref/(?:command|statement)/[^"]+\.html)"',
        content=("refbody", "topicBody", "topicbody", "topic-content"), render=True,
        referer="https://networkingdocs.hpe.com/documentation/",
        title_re=r"<title>(.*?)</title>",
        note="2026-10 起文档迁到 networkingdocs.hpe.com；旧 juniper.net 正文页 404 或 SPA 空壳"),

    # ---- 锐捷：目录壳，深页未通（如实标注）
    "ruijie": Adapter(
        "ruijie", "https://www.ruijie.com",
        r'href="(/en-global/support/documents/[^"]+)"',
        content="pwmDoc", render=True,
        referer="https://www.ruijie.com/",
        note="⚠ 目录页 543 KB 全是导航，正文 0；深页需人工确认 URL"),

    # ---- 华为：需要正确的 EDOC docid
    "huawei": Adapter(
        "huawei", "https://support.huawei.com",
        r'href="(/enterprise/zh/doc/[A-Z0-9]+[^"]*)"',
        content="main-container", render=True,
        referer="https://support.huawei.com/",
        note="⚠ HedEx 文档需正确的 EDOC docid，索引页本身是壳"),
}


# ============================================================ HTML 工具
SCRIPT_STYLE = re.compile(r"<(script|style|noscript).*?</\1>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(html: str) -> str:
    txt = TAG_RE.sub(" ", SCRIPT_STYLE.sub(" ", html))
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#160;", " ")
    return re.sub(r"\s+", " ", txt).strip()


def balanced_extract_all(html: str, cls: str) -> list:
    """取出**所有** `<tag class="...cls...">` 容器，按嵌套配平切分，返回列表。

    ⚠ 必须取"全部"而不是"第一个"。实测 Cisco 的命令参考页里
      **每个命令一个 `<section class="body refbody">`，一页 40 个**。
      只取第一个 → 一页 40 条命令只落盘 1 条，而且**不报错**
      （落盘文件看着正常、有正文、有命令表，只是少了 39 条）。
      这类"静默少数据"比报错危险得多。

    ⚠ 也**不能**用非贪婪正则：容器里套着几十层 section/div，
      正则在第一个收尾标签就停，只能拿到开头几百字。
    """
    out = []
    open_pat = r'<([a-zA-Z][\w-]*)[^>]*class="[^"]*\b%s\b[^"]*"' % re.escape(cls)
    for m in re.finditer(open_pat, html, re.I):
        tag = m.group(1)
        open_re = re.compile(r"<%s\b" % tag, re.I)
        close_re = re.compile(r"</%s\s*>" % tag, re.I)
        depth, i = 0, m.start()
        seg = ""
        while i < len(html):
            o = open_re.search(html, i)
            c = close_re.search(html, i)
            if not c:
                break
            if o and o.start() < c.start():
                depth += 1
                i = o.end()
            else:
                depth -= 1
                i = c.end()
                if depth <= 0:
                    seg = html[m.start():i]
                    break
        if not seg:
            seg = html[m.start():m.start() + 400000]     # 配平失败就截一段
        if seg not in out:
            out.append(seg)
    return out


def balanced_extract(html: str, cls: str, tag: str = "") -> str:
    """取第一个容器（保留给"整页就是一个容器"的站点用）。"""
    got = balanced_extract_all(html, cls)
    return got[0] if got else ""


def page_title(html: str, ad: Adapter) -> str:
    if ad.title_re:
        m = ad.title_re.search(html)
        if m:
            t = strip_tags(m.group(1))
            t = re.sub(r"\s*[-|]\s*(Cisco|Juniper Networks|Junos|新华三.*|H3C).*$", "",
                       t, flags=re.I).strip()
            if t:
                return t
    return ""


def safe_name(s: str) -> str:
    """标题 → 文件名。Windows 非法字符要换掉，否则落盘直接失败。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s).strip().rstrip(".")
    return (s[:90] or "untitled")


def get_page(url: str, ad: Adapter, budget: int) -> str:
    """按适配器选后端取页面 HTML。"""
    if ad.render:
        return render_get(url, budget=budget)
    st, body = http_get(url, referer=ad.referer)
    if st != 200:
        raise RuntimeError("HTTP %s" % st)
    return body.decode("utf-8", "replace")


# ============================================================ probe
PROBES = [
    ("h3c", "在线命令参考（正文页）",
     "https://www.h3c.com/cn/d_202607/2876059_30005_0.htm"),
    ("h3c", "官方文档包 CDN",
     "https://download.h3c.com/app/cn/download.do?id=4311165"),
    ("cisco", "cisco.com 首页（基线）", "https://www.cisco.com/"),
    ("cisco", "IOS XE 命令参考书",
     "https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/interface/command/ir-cr-book/ir-a1.html"),
    ("juniper", "Junos show interfaces",
     "https://www.juniper.net/documentation/us/en/software/junos/cli-reference/"
     "topics/ref/command/show-interfaces-extensive.html"),
    ("ruijie", "RGOS 配置指导",
     "https://www.ruijie.com/en-global/support/documents/"
     "slide_rg-s5360-e-and-rg-s5350-e-rgos-12-6-4-b0702-configuration-guide"),
    ("huawei", "HedEx 文档页",
     "https://support.huawei.com/enterprise/zh/doc/EDOC1100218407"),
]


def cmd_probe(a) -> int:
    log("=" * 100)
    log("厂商文档可达性探测 —— 同时测 HTTP 和无头渲染两条路")
    log("Chrome: %s" % (CHROME or "（没找到，render 后端不可用）"))
    log("=" * 100)
    log("%-8s %-28s %-10s %-14s %s" % ("厂商", "端点", "HTTP", "无头渲染", "判读"))
    log("-" * 100)
    for vendor, what, url in PROBES:
        st, body = http_get(url)
        http_v = ("%s/%dKB" % (st, len(body) // 1024)) if st else "不可达"
        rd_v, verdict = "-", ""
        if CHROME:
            try:
                html = render_get(url, budget=8000, timeout=90)
                txt = strip_tags(html)
                rd_v = "%dKB/%d字" % (len(html.encode()) // 1024, len(txt))
                if len(txt) > 4000:
                    verdict = "✅ 渲染后是真正文"
                else:
                    verdict = "⚠ 渲染后仍是壳"
            except Exception as e:                             # noqa: BLE001
                rd_v, verdict = "失败", str(e)[:40]
        else:
            verdict = "（无 Chrome，无法判读渲染）"
        if st == 200 and len(body) > 20000:
            verdict = "✅ 纯 HTTP 即可抓（不需要渲染）" + ("；" + verdict if rd_v != "-" else "")
        elif st == 403:
            verdict = "⛔ HTTP 403 → " + verdict
        log("%-8s %-28s %-10s %-14s %s"
            % (vendor, what[:28], http_v, rd_v, verdict))
    log("-" * 100)
    log("结论：**纯 HTTP 只有 H3C 成立**；Cisco（403）/Juniper（SPA）必须走无头渲染。")
    log("      厂商的**文档包**（CHM/PDF）各家都门控 → 走 inbox 人工下载。")
    return 0


# ============================================================ list / fetch
def parse_index(html: str, ad: Adapter) -> list:
    """目录页 → [(href, 标题), ...]，按适配器的 link_re 过滤。"""
    seen, items = set(), []
    for m in ad.link_re.finditer(html):
        href = m.group(1)
        if href in seen:
            continue
        # 标题：优先取 <a> 内的文本，取不到就用 href 末段
        seg = html[m.end():m.end() + 400]
        t = re.search(r"^[^>]*>(.*?)</a>", seg, re.S | re.I)
        title = strip_tags(t.group(1)) if t else ""
        if not title or len(title) > 120:
            title = href.rstrip("/").split("/")[-1].rsplit(".", 1)[0]
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        seen.add(href)
        items.append((href, title))
    return items


def cmd_list(a) -> int:
    ad = ADAPTERS[a.vendor]
    try:
        html = get_page(a.index_url, ad, a.budget)
    except Exception as e:                                     # noqa: BLE001
        log("✗ 目录页取不到：%s" % e)
        return 1
    items = parse_index(html, ad)
    log("厂商: %s   后端: %s" % (a.vendor, "无头渲染" if ad.render else "纯 HTTP"))
    log("目录页解析出 %d 个正文页链接" % len(items))
    for href, title in items[:a.limit or len(items)]:
        log("  %-70s %s" % (href[:70], title[:40]))
    if ad.note:
        log("备注: %s" % ad.note)
    return 0


def cmd_fetch(a) -> int:
    ad = ADAPTERS[a.vendor]
    out_dir = os.path.join(VENDOR_DOCS, a.out_name)
    os.makedirs(out_dir, exist_ok=True)
    toc_lines, manifest = [], []
    n_ok = n_skip = n_fail = 0
    backend = "render" if (a.render or (ad.render and not a.no_render)) else "http"
    if a.render:
        backend = "render"
    if a.no_render:
        backend = "http"
    ad_use = Adapter(ad.vendor, ad.base, ad.link_re.pattern, ad.content,
                     backend == "render", ad.referer,
                     ad.title_re.pattern if ad.title_re else None, ad.note)

    for idx_url in a.index_url:
        try:
            html = get_page(idx_url, ad_use, a.budget)
        except Exception as e:                                 # noqa: BLE001
            log("✗ 目录页取不到（%s）：%s" % (e, idx_url))
            continue
        chapter = page_title(html, ad_use) or a.chapter or "未命名章"
        chapter = safe_name(chapter)
        items = parse_index(html, ad_use)
        log("章「%s」：解析出 %d 个正文页" % (chapter, len(items)))
        toc_lines.append("- %s" % chapter)
        if a.limit:
            items = items[:a.limit]

        for href, title in items:
            rel = "%s/html/%s.htm" % (chapter, safe_name(title))
            dest = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            toc_lines.append("  - %s  [%s]" % (title, rel))
            if os.path.exists(dest) and not a.force:
                n_skip += 1
                continue
            full = href if href.startswith("http") else ad.base + href
            try:
                page = get_page(full, ad_use, a.budget)
            except Exception as e:                             # noqa: BLE001
                log("   ✗ %-40s %s" % (title[:40], e))
                n_fail += 1
                continue
            # 只留正文容器：不然后续读到的"正文"里混着几百 KB 导航。
            # 取**全部**容器 —— Cisco 一页 40 个 refbody（每个命令一个）。
            # 容器名可以给多个，按顺序试到第一个有内容的（Juniper 各页不统一）。
            parts = []
            for cls in (ad.content or ()):
                parts = balanced_extract_all(page, cls)
                if parts:
                    break
            body = "\n<!-- ---- container ---- -->\n".join(parts) if parts else page
            txt = strip_tags(body)
            if len(txt) < 400:
                log("   ⚠ %-40s 正文仅 %d 字，疑似壳，仍落盘备查"
                    % (title[:40], len(txt)))
            # UTF-8 落盘：build_tech_docs._decode 先试 utf-8，与既有 gb18030 数据兼容
            with io.open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("<!-- source: %s -->\n" % full)
                fh.write("<title>%s</title>\n" % title)
                fh.write(body)
            manifest.append({"title": title, "url": full, "rel": rel,
                             "bytes": len(body.encode()), "text_chars": len(txt),
                             "backend": backend,
                             "sha256": hashlib.sha256(body.encode()).hexdigest()[:16]})
            n_ok += 1
            log("   ✓ %-40s %7d 字节 / %6d 字" % (title[:40], len(body.encode()), len(txt)))
            time.sleep(a.delay)

    # ---- 直取模式：目录发现不了（SPA 侧边栏/门控）时，直接给正文页 URL ----
    # 实测 Juniper 的书籍级 TOC 是 SPA 渲染的，脚本拿不到干净的目录；
    # 但它每个命令页的 URL 是可预测的，所以给一条"绕过目录"的路。
    if a.page_url:
        chapter = safe_name(a.chapter or a.out_name)
        toc_lines.append("- %s" % chapter)
        for full in a.page_url:
            title = safe_name(full.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0])
            rel = "%s/html/%s.htm" % (chapter, title)
            dest = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            toc_lines.append("  - %s  [%s]" % (title, rel))
            if os.path.exists(dest) and not a.force:
                n_skip += 1
                continue
            try:
                page = get_page(full, ad_use, a.budget)
            except Exception as e:                             # noqa: BLE001
                log("   ✗ %-40s %s" % (title[:40], e))
                n_fail += 1
                continue
            parts = []
            for cls in (ad.content or ()):
                parts = balanced_extract_all(page, cls)
                if parts:
                    break
            body = "\n<!-- ---- container ---- -->\n".join(parts) if parts else page
            txt = strip_tags(body)
            if len(txt) < 400:
                log("   ⚠ %-40s 正文仅 %d 字，疑似壳" % (title[:40], len(txt)))
            with io.open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("<!-- source: %s -->\n" % full)
                fh.write("<title>%s</title>\n" % title)
                fh.write(body)
            manifest.append({"title": title, "url": full, "rel": rel,
                             "bytes": len(body.encode()), "text_chars": len(txt),
                             "backend": backend, "mode": "page-url",
                             "sha256": hashlib.sha256(body.encode()).hexdigest()[:16]})
            n_ok += 1
            log("   ✓ %-40s %7d 字节 / %6d 字" % (title[:40], len(body.encode()), len(txt)))
            time.sleep(a.delay)

    toc_path = os.path.join(out_dir, "toc.txt")
    with io.open(toc_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("##### crawled by fetch_vendor_docs.py  vendor=%s  backend=%s  (%d 页) #####\n"
                 % (ad.vendor, backend, len(manifest)))
        fh.write("\n".join(toc_lines) + "\n")
    with io.open(os.path.join(out_dir, "_fetch_manifest.json"), "w",
                 encoding="utf-8") as fh:
        json.dump({"out_name": a.out_name, "vendor": ad.vendor, "backend": backend,
                   "index_urls": a.index_url, "n_ok": n_ok, "n_skip": n_skip,
                   "n_fail": n_fail, "items": manifest},
                  fh, ensure_ascii=False, indent=1)

    log("-" * 74)
    log("完成：新增 %d，跳过 %d，失败 %d（后端=%s）" % (n_ok, n_skip, n_fail, backend))
    log("落盘：%s" % out_dir)
    log("toc.txt：%s（%d 行）" % (toc_path, len(toc_lines)))
    return 0 if n_fail == 0 else 1


# ============================================================ pkg（文档包）
#: ⚠ 修正一条旧结论：H3C 的文档包**不是** 403 门控的。
#:   先前记录"dltt.h3c.com/.../*.rar?token=… → 403"，那是因为**直接去请求带 token 的
#:   CDN 直链**。正确入口是 download.do，它 302 到 dltt.h3c.com/websitedownload/…
#:   并且**原样返回文件**（实测 200 + 8,131,184 字节 RAR，Content-Disposition 带真实文件名）。
#:   关键是请求头要带 `Accept: */*` 和 Referer，且**跟随重定向**。
H3C_DL = "https://download.h3c.com/app/cn/download.do?id=%s"


def find_7z() -> str:
    for p in (r"C:\Program Files\7-Zip\7z.exe",
              r"C:\Program Files (x86)\7-Zip\7z.exe",
              r"C:\Program Files\WinRAR\UnRAR.exe"):
        if os.path.exists(p):
            return p
    return ""


SEVEN_ZIP = find_7z()


def _parse_hhc(txt: str) -> list:
    """CHM 的 .hhc（目录树）→ [(depth, name, local), ...]。

    复用既有 `_analyze.py` 的实现：先 import，import 不到就内联一份，
    避免工具依赖某个固定路径。
    """
    try:
        sys.path.insert(0, VENDOR_DOCS)
        from _analyze import parse_hhc                  # type: ignore
        return parse_hhc(txt)[0]
    except Exception:                                   # noqa: BLE001
        obj_re = re.compile(r"<OBJECT\b.*?</OBJECT>|<OBJECT\b[^>]*/?>", re.I | re.S)
        nm_re = re.compile(r"""name\s*=\s*["']?Name["']?\s+value\s*=\s*["'](.*?)["']""",
                           re.I | re.S)
        lc_re = re.compile(r"""name\s*=\s*["']?Local["']?\s+value\s*=\s*["'](.*?)["']""",
                           re.I | re.S)
        tok = re.compile(r"(<UL\b[^>]*>)|(</UL\s*>)|(<OBJECT\b.*?</OBJECT\s*>)", re.I | re.S)
        depth, out = 0, []
        for m in tok.finditer(txt):
            if m.group(1):
                depth += 1
            elif m.group(2):
                depth = max(0, depth - 1)
            else:
                nm = nm_re.search(m.group(3))
                if not nm:
                    continue
                name = nm.group(1).strip()
                lc = lc_re.search(m.group(3))
                out.append((max(0, depth - 1), name, lc.group(1).strip() if lc else ""))
        return out


def _decode_any(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def cmd_pkg(a) -> int:
    """下载厂商**文档包**（CHM），解包并生成 toc.txt —— 比抓在线 HTML 更完整。

    链路：download.do?id=N → RAR → 7z 解出 CHM → 7z 解出 htm+hhc → toc.txt
    这条路的产物和既有 `_vendor_docs/h3c_s12500` **完全同构**，
    所以下游 `build_tech_docs.py` 不需要任何改动。
    """
    if not SEVEN_ZIP:
        log("✗ 找不到 7-Zip / UnRAR，无法解包")
        return 1
    out_dir = os.path.join(VENDOR_DOCS, a.out_name)
    raw_dir = os.path.join(out_dir, "_pkg")
    os.makedirs(raw_dir, exist_ok=True)

    archives = []
    for dl_id in a.id:
        url = a.url or (H3C_DL % dl_id)
        dest = os.path.join(raw_dir, "%s.bin" % dl_id)
        if os.path.exists(dest) and not a.force:
            log("= 已存在，跳过下载：%s" % dest)
            archives.append(dest)
            continue
        log("↓ 下载 id=%s" % dl_id)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "*/*",                     # ← 必须 */*；用 text/html 会被当成浏览页面
            "Referer": "https://www.h3c.com/cn/Service/Document_Software/Document_Center/",
        })
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
                cd = r.headers.get("Content-Disposition", "")
                log("   HTTP %s  %d 字节  %s" % (r.status, len(data), cd[:90]))
        except Exception as e:                                 # noqa: BLE001
            log("   ✗ 下载失败：%s" % e)
            continue
        if len(data) < 10000:
            log("   ✗ 体量异常（%d 字节），不是文档包" % len(data))
            continue
        with open(dest, "wb") as fh:
            fh.write(data)
        archives.append(dest)

    # ---- 解包：RAR/ZIP → CHM → 目录树
    chms = []
    for arc in archives:
        ex_dir = os.path.join(raw_dir, "x_" + os.path.basename(arc).rsplit(".", 1)[0])
        os.makedirs(ex_dir, exist_ok=True)
        log("⚙ 解包 %s" % os.path.basename(arc))
        p = subprocess.run([SEVEN_ZIP, "x", arc, "-o" + ex_dir, "-y"],
                           capture_output=True)
        log("   7z exit=%d" % p.returncode)
        for r, _d, fs in os.walk(ex_dir):
            for f in fs:
                if f.lower().endswith(".chm"):
                    chms.append(os.path.join(r, f))
    if not chms:
        log("✗ 压缩包里没有 .chm（看看 %s 里是什么）" % raw_dir)
        return 1

    for chm in chms:
        log("⚙ 解 CHM：%s（%.1f MB）" % (os.path.basename(chm),
                                        os.path.getsize(chm) / 2 ** 20))
        p = subprocess.run([SEVEN_ZIP, "x", chm, "-o" + out_dir, "-y"],
                           capture_output=True)
        log("   7z exit=%d" % p.returncode)

    # ---- .hhc → toc.txt（与既有 h3c_s12500 同格式）
    hhcs = []
    for r, _d, fs in os.walk(out_dir):
        for f in fs:
            if f.lower().endswith(".hhc"):
                hhcs.append(os.path.join(r, f))
    if not hhcs:
        log("⚠ 没找到 .hhc，toc.txt 无法生成（下游标题会退化成文件名）")
        return 1
    lines = []
    for h in sorted(hhcs, key=lambda x: -os.path.getsize(x)):
        raw = open(h, "rb").read()
        txt = _decode_any(raw)
        tree = _parse_hhc(txt)
        rel = os.path.relpath(h, out_dir)
        tops = sum(1 for d, _n, _l in tree if d == 0)
        lines.append("##### %s (%d bytes, %d entries, %d top-level) #####"
                     % (rel, len(raw), len(tree), tops))
        for d, name, local in tree:
            lines.append("  " * d + "- " + name + ("  [%s]" % local if local else ""))
        lines.append("")
        log("   %s：%d 条目录，%d 个顶层" % (rel, len(tree), tops))
    toc_path = os.path.join(out_dir, "toc.txt")
    with io.open(toc_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    log("toc.txt → %s（%d 行）" % (toc_path, len(lines)))
    log("落盘：%s" % out_dir)
    log("下一步：python tools/doc_to_commands.py --vendor <v> ... 或直接 build_tech_docs.py")
    return 0


# ============================================================ inbox
INBOX_RULES = [
    (re.compile(r"^H3C\b", re.I), "h3c", "chm"),
    (re.compile(r"\bH3C\b", re.I), "h3c", "chm"),
    (re.compile(r"(AR\d|CloudEngine|NetEngine|USG\d|S\d{4,}|CE\d{4,})", re.I),
     "huawei", "chm"),
    (re.compile(r"^(RG-|Ruijie)", re.I), "ruijie", "pdf"),
    (re.compile(r"\b(Juniper|Junos|EX\d|MX\d|SRX\d|QFX\d)\b", re.I), "juniper", "pdf"),
    (re.compile(r"\b(Cisco|IOS|Catalyst|Nexus|ASA)\b", re.I), "cisco", "pdf"),
]


def cmd_inbox(a) -> int:
    """把人工下载的原始文档归类，并打印**下一步该跑哪条既有脚本**。

    本命令**不移动、不删除**任何文件，因为厂商文档包动辄几百 MB，误移代价高。
    """
    src = os.path.expanduser(a.dir)
    if not os.path.isdir(src):
        log("✗ 目录不存在：%s" % src)
        return 1
    files = []
    for name in sorted(os.listdir(src)):
        p = os.path.join(src, name)
        if os.path.isfile(p):
            files.append((name, os.path.getsize(p)))
        elif os.path.isdir(p):
            for sub in sorted(os.listdir(p)):
                sp = os.path.join(p, sub)
                if os.path.isfile(sp):
                    files.append((os.path.join(name, sub), os.path.getsize(sp)))
    if not files:
        log("（目录里没有文件）")
        return 0

    log("%-58s %10s  %-8s %-8s %s" % ("文件", "大小", "厂商", "形态", "下一步"))
    log("-" * 104)
    todo = []
    for rel, size in files:
        base = os.path.basename(rel)
        ext = os.path.splitext(base)[1].lower()
        vendor, form = "", ""
        for rx, v, f in INBOX_RULES:
            if rx.search(base):
                vendor, form = v, f
                break
        if ext == ".pdf":
            form = "pdf"
        elif ext == ".chm":
            form = "chm"
        elif ext in (".zip", ".rar", ".7z"):
            form = "archive"
        nxt = {"chm": "_extract_chm.ps1（7z 解包）",
               "pdf": "_extract_pdf.py（pypdf 逐页）",
               "archive": "先 7z 解开再看里面是 chm 还是 pdf",
               }.get(form, "无法识别，请人工确认")
        log("%-58s %9.1fM  %-8s %-8s %s"
            % (rel[:58], size / 2 ** 20, vendor or "?", form or "?", nxt))
        if vendor and form:
            todo.append((rel, vendor, form))
    log("-" * 104)
    log("识别到 %d 个可入库的文件。提取脚本在 %s" % (len(todo), VENDOR_DOCS))
    log("⚠ 本命令只做识别与归类建议，**没有移动/删除任何文件**。")
    return 0


# ============================================================ verify
def cmd_verify(a) -> int:
    """验证「文档 → 知识库」这一半真的通了：直接调运行时检索。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from core import tech_knowledge
    except Exception as e:                                     # noqa: BLE001
        log("✗ 导不进 core.tech_knowledge：%s" % e)
        return 1
    log("=" * 78)
    log("技术资料层自检  vendor=%s" % a.vendor)
    log("=" * 78)
    for q in a.query:
        try:
            r = tech_knowledge.explain(a.vendor, q)
        except Exception as e:                                 # noqa: BLE001
            log("  %-24s ✗ %s" % (q, e))
            continue
        log("  %-24s available=%-5s how=%-18s feature=%s"
            % (q, r.get("available"), r.get("how"), (r.get("feature") or "")[:34]))
        if not r.get("available"):
            log("      need_ai=%s  候选=%s"
                % (r.get("need_ai"), (r.get("candidates") or [])[:3]))
    return 0


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe").set_defaults(fn=cmd_probe)

    p = sub.add_parser("list")
    p.add_argument("--vendor", required=True, choices=sorted(ADAPTERS))
    p.add_argument("--index-url", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--budget", type=int, default=9000, help="渲染等待毫秒")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("fetch")
    p.add_argument("--vendor", required=True, choices=sorted(ADAPTERS))
    p.add_argument("--index-url", action="append", default=[],
                   help="目录页，可重复传多个")
    p.add_argument("--page-url", action="append", default=[],
                   help="直接指定正文页 URL（目录发现不了时用，如 SPA 站点），可重复")
    p.add_argument("--out-name", required=True,
                   help="落到 _vendor_docs/<out-name>/，如 h3c_s5100ei")
    p.add_argument("--chapter", default="", help="章名（目录页取不到标题时用）")
    p.add_argument("--limit", type=int, default=0, help="每章最多抓多少页（试跑用）")
    p.add_argument("--delay", type=float, default=0.4, help="每请求间隔秒")
    p.add_argument("--budget", type=int, default=9000, help="渲染等待毫秒")
    p.add_argument("--render", action="store_true", help="强制无头渲染")
    p.add_argument("--no-render", action="store_true", help="强制纯 HTTP")
    p.add_argument("--force", action="store_true", help="已存在的也重抓")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("pkg")
    p.add_argument("--vendor", default="h3c", choices=sorted(ADAPTERS))
    p.add_argument("--id", action="append", required=True,
                   help="文档包 id（H3C：download.do?id=N 里的 N），可重复传")
    p.add_argument("--url", default="", help="直接给完整下载 URL（覆盖 --id 拼接）")
    p.add_argument("--out-name", required=True)
    p.add_argument("--force", action="store_true", help="已下载的也重下")
    p.set_defaults(fn=cmd_pkg)

    p = sub.add_parser("inbox")
    p.add_argument("--dir", required=True)
    p.set_defaults(fn=cmd_inbox)

    p = sub.add_parser("verify")
    p.add_argument("--vendor", default="h3c")
    p.add_argument("--query", action="append", default=[])
    p.set_defaults(fn=cmd_verify)

    a = ap.parse_args()
    if a.cmd == "verify" and not a.query:
        a.query = ["acl number", "display interface brief", "端口聚合"]
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
