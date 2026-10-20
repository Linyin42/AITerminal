"""
本地资料库 - 把下载好的命令手册 / 配置案例 / 培训文档放进来，AI 查命令时先在本地检索

设计目标:
    1. 用户点一个按钮就能把文件导入到统一目录，不用关心放哪
    2. 自动抽取正文并建索引（txt/md/pdf/docx/xlsx/pptx/html/csv/log...）
    3. 检索时先查本地（快、离线、贴合自己设备型号），查不到再联网
    4. 索引缓存到 .index.json，按文件修改时间增量重建

目录结构:
    ~/.aiterminal/knowledge/
        ├── 华为S5700命令手册.pdf
        ├── H3C配置案例.docx
        ├── .index.json          # 索引缓存（隐藏文件，可安全删除）
        └── ...

只依赖标准库 + 可选 pypdf（解析 PDF）。docx/xlsx/pptx 用 zipfile 直接解析，
不需要 python-docx / openpyxl。
"""
import csv
import ctypes
import gc
import html as html_mod
import io
import json
import math
import os
import re
import shutil
import threading
import time
import zipfile
from collections import Counter
from typing import Dict, List, Optional

DEFAULT_KNOWLEDGE_DIR = os.path.join(os.path.expanduser("~"), ".aiterminal", "knowledge")


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """`GetProcessMemoryInfo` 的入参结构（字段顺序/类型照 Win32 定义）"""
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def process_rss_mb() -> float:
    """当前进程常驻内存（MB）。拿不到就返回 0（调用方自己退回估算值）。

    ⚠ 两个 ctypes 坑都在这里（2026-10-17 实测踩过，表现是"RSS 恒为 0"）：
      1. `GetCurrentProcess()` 的 restype 要显式声明 c_void_p，否则返回的伪句柄
         -1（0xFFFFFFFFFFFFFFFF）会被当成 c_int 截断；
      2. `GetProcessMemoryInfo` 的 argtypes 也要显式声明，否则 ctypes 按 c_int 传参，
         那个 64 位句柄直接 OverflowError —— 被下面的 except 吞掉就成了"量尺坏了
         但看起来像内存没涨"，比报错更坑。
    """
    import sys as _sys
    if not _sys.platform.startswith("win"):
        # Linux / macOS 走这条路：/proc/self/statm 第 2 列是常驻页数，
        # 比 resource.getrusage().ru_maxrss（峰值）更贴近"现在占多少"。
        try:
            import os as _os
            with open("/proc/self/statm", encoding="ascii") as fh:
                pages = int(fh.read().split()[1])
            return pages * _os.sysconf("SC_PAGE_SIZE") / 1048576.0
        except Exception:                           # noqa: BLE001
            return 0.0
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
                       ctypes.c_ulong]
        fn.restype = ctypes.c_int
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        if not fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return 0.0
        return pmc.WorkingSetSize / 1048576.0
    except Exception:                               # noqa: BLE001
        return 0.0
INDEX_FILENAME = ".index.json"
INDEX_VERSION = 1

# 可直接按文本读取的扩展名
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".log", ".cfg", ".conf", ".ini", ".json",
    ".yaml", ".yml", ".csv", ".tsv", ".xml", ".rst", ".text",
}
# 需要解析的扩展名
PARSE_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".zip", ".hdx"}
# 老版 Office 二进制格式，无法用标准库解析
UNSUPPORTED_EXTENSIONS = {
    ".doc": "老版 .doc 无法直接解析，请另存为 .docx 或导出 PDF",
    ".xls": "老版 .xls 无法直接解析，请另存为 .xlsx",
    ".ppt": "老版 .ppt 无法直接解析，请另存为 .pptx",
    ".rar": "暂不支持 .rar，请先解压后再导入",
    ".7z": "暂不支持 .7z，请先解压后再导入",
}

SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | PARSE_EXTENSIONS

# 经验总结分类：AI 在后台把每轮交互的"成功点 / 踩坑点"写进这里，
# 之后提到同类需求时可以快速匹配出来
EXPERIENCE_CATEGORY = "经验总结"

# 单个文档最多保留的字符数（防止一本几百页的手册把索引撑爆）
MAX_DOC_CHARS = 400_000


# ======================================================================
# 正文抽取
# ======================================================================
def read_text_file(path: str) -> str:
    """按常见中文编码尝试读取文本文件"""
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, errors="strict") as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def extract_html(raw: str) -> str:
    """HTML 转纯文本"""
    text = re.sub(r'<(script|style|noscript|head)[^>]*>.*?</\1>', ' ', raw, flags=re.S | re.I)
    text = re.sub(r'<!--.*?-->', ' ', text, flags=re.S)
    text = re.sub(r'<(br|/p|/div|/li|/tr|/h[1-6]|/td)[^>]*>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = html_mod.unescape(text)
    return _tidy(text)


#: hdx 里的 html 声明 `charset=gb2312`，实际是 GB18030。
#: ⚠ 用 utf-8 读**不会报错**，只会静默产出乱码 —— 所以解码顺序不能想当然。
_HDX_ENC = ("gb18030", "utf-8", "gbk")


def _hdx_decode(raw: bytes) -> str:
    for enc in _HDX_ENC:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="replace")


def _hdx_bookmark_order(zf: "zipfile.ZipFile") -> List[str]:
    """从 `.hhc` 书签里取**厂商自己的目录顺序**（拿不到就返回空，调用方按文件名兜底）"""
    hhc = [n for n in zf.namelist() if n.lower().endswith(".hhc")]
    if not hhc:
        return []
    try:
        raw = _hdx_decode(zf.read(hhc[0]))
    except Exception:
        return []
    order = []
    for body in re.findall(r'<OBJECT[^>]*text/sitemap[^>]*>(.*?)</OBJECT>', raw, re.S | re.I):
        m = re.search(r'name\s*=\s*"Local"\s+value\s*=\s*"([^"]*)"', body, re.I)
        if not m:
            continue
        loc = html_mod.unescape(m.group(1)).split("#")[0].strip().replace("\\", "/")
        if not loc:
            continue
        if not loc.lower().startswith("resources/"):
            loc = "resources/" + loc
        order.append(loc)
    return order


def iter_hdx_pages(path: str):
    """逐页产出 hdx 正文（按 `.hhc` 书签顺序，跳过目录/索引页）。

    拆成生成器是为了让调用方**自己决定怎么拼**：
    `extract_hdx()` 拼成一个有上限的长文本；`_import_hdx()` 则按大小分片落盘。
    """
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        htmls = [n for n in names if n.lower().endswith((".html", ".htm"))]
        by_lower = {n.lower(): n for n in htmls}
        seq, seen = [], set()
        for loc in _hdx_bookmark_order(zf):
            key = loc.lower()
            if key in by_lower and key not in seen:
                seen.add(key)
                seq.append(by_lower[key])
        for n in htmls:                       # 书签没覆盖到的页，补在后面
            if n.lower() not in seen:
                seen.add(n.lower())
                seq.append(n)

        for name in seq:
            try:
                raw = _hdx_decode(zf.read(name))
            except Exception:
                continue
            text = extract_html(raw)
            if len(text) < 120:               # 目录/索引/导航页：没有实质正文
                continue
            yield text


def extract_hdx(path: str, max_chars: int = MAX_DOC_CHARS) -> str:
    """华为 `.hdx`（eDoc 产品文档包）→ 纯文本（有上限）。

    `.hdx` 就是个 ZIP：`profile.xml` + `resources/**/*.html`（正文）+ `.hhc`（书签/目录）。
    为什么要单独支持它：华为产品文档官网只发 `.hdx`，用户在软件里点「导入资料」
    直接选 .hdx 就能入库，不用先手工解包。
    """
    parts: List[str] = []
    total = 0
    for text in iter_hdx_pages(path):
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            parts.append("……（文档过长，已截断）")
            break
    return _tidy("\n\n".join(parts))[:max_chars]


def extract_docx(path: str) -> str:
    """从 docx 中抽取正文（zipfile 直接读 word/document.xml）"""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        parts = []
        if 'word/document.xml' in names:
            xml = zf.read('word/document.xml').decode('utf-8', errors='replace')
            # 段落结束转换行，单元格之间加分隔
            xml = re.sub(r'</w:p>', '\n', xml)
            xml = re.sub(r'</w:tc>', ' | ', xml)
            xml = re.sub(r'<w:tab[^>]*/>', '\t', xml)
            xml = re.sub(r'<[^>]+>', '', xml)
            parts.append(html_mod.unescape(xml))
        # 页眉页脚也带上，手册里常有型号信息
        for extra in ('word/header1.xml', 'word/footer1.xml'):
            if extra in names:
                xml = zf.read(extra).decode('utf-8', errors='replace')
                parts.append(html_mod.unescape(re.sub(r'<[^>]+>', ' ', xml)))
        return _tidy("\n".join(parts))


def extract_xlsx(path: str) -> str:
    """从 xlsx 中抽取单元格文本（zipfile 直接读 sharedStrings + worksheets）"""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        shared: List[str] = []
        if 'xl/sharedStrings.xml' in names:
            raw = zf.read('xl/sharedStrings.xml').decode('utf-8', errors='replace')
            for item in re.findall(r'<si>(.*?)</si>', raw, re.S):
                shared.append(html_mod.unescape(re.sub(r'<[^>]+>', '', item)))

        # 读取工作表真实名称（workbook.xml 里的 <sheet name=".." r:id="rIdN"/>）
        sheet_names = {}
        if 'xl/workbook.xml' in names:
            workbook = zf.read('xl/workbook.xml').decode('utf-8', errors='replace')
            for order, match in enumerate(
                    re.finditer(r'<sheet[^>]*name="([^"]*)"[^>]*>', workbook), start=1):
                sheet_names[order] = html_mod.unescape(match.group(1))

        lines = []
        sheets = sorted((n for n in names
                         if re.match(r'xl/worksheets/sheet\d+\.xml$', n)),
                        key=lambda n: int(re.search(r'(\d+)', n).group(1)))
        for sheet in sheets:
            order = int(re.search(r'(\d+)', sheet).group(1))
            label = sheet_names.get(order) or os.path.basename(sheet)
            raw = zf.read(sheet).decode('utf-8', errors='replace')
            lines.append(f"--- {label} ---")
            for row in re.findall(r'<row[^>]*>(.*?)</row>', raw, re.S):
                cells = []
                for match in re.finditer(r'<c\b([^>]*)>(.*?)</c>', row, re.S):
                    attrs, body = match.group(1), match.group(2)
                    value_match = re.search(r'<v>(.*?)</v>', body, re.S)
                    if value_match:
                        value = value_match.group(1)
                        if 't="s"' in attrs:
                            try:
                                value = shared[int(value)]
                            except (ValueError, IndexError):
                                pass
                        cells.append(html_mod.unescape(re.sub(r'<[^>]+>', '', value)))
                    else:
                        text_match = re.search(r'<t[^>]*>(.*?)</t>', body, re.S)
                        if text_match:
                            cells.append(html_mod.unescape(
                                re.sub(r'<[^>]+>', '', text_match.group(1))))
                if any(c.strip() for c in cells):
                    lines.append(' | '.join(cells))
        return _tidy("\n".join(lines))


def extract_pptx(path: str) -> str:
    """从 pptx 中抽取幻灯片文本"""
    with zipfile.ZipFile(path) as zf:
        slides = sorted((n for n in zf.namelist()
                         if re.match(r'ppt/slides/slide\d+\.xml$', n)),
                        key=lambda n: int(re.search(r'(\d+)', n).group(1)))
        parts = []
        for index, slide in enumerate(slides, start=1):
            raw = zf.read(slide).decode('utf-8', errors='replace')
            raw = re.sub(r'</a:p>', '\n', raw)
            text = html_mod.unescape(re.sub(r'<[^>]+>', '', raw))
            parts.append(f"--- 第 {index} 页 ---\n{text}")
        return _tidy("\n".join(parts))


def extract_pdf(path: str) -> str:
    """从 PDF 中抽取文本（需要 pypdf）"""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("解析 PDF 需要 pypdf，请执行: pip install pypdf")

    reader = PdfReader(path)
    parts = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            parts.append(f"--- 第 {index} 页 ---\n{text}")
        if sum(len(p) for p in parts) > MAX_DOC_CHARS:
            parts.append("……（文档过长，已截断）")
            break
    return _tidy("\n".join(parts))


def extract_csv_file(path: str) -> str:
    """CSV/TSV 转成竖线分隔的文本，便于检索"""
    raw = read_text_file(path)
    if not raw:
        return ""
    delimiter = '\t' if path.lower().endswith('.tsv') else ','
    try:
        rows = list(csv.reader(io.StringIO(raw), delimiter=delimiter))
    except Exception:
        return _tidy(raw)
    return _tidy("\n".join(' | '.join(cell.strip() for cell in row) for row in rows))


def _tidy(text: str) -> str:
    """压缩空白、去掉空行"""
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '')
    text = re.sub(r'[ \t\xa0]+', ' ', text)
    lines = [ln.strip() for ln in text.split('\n')]
    return '\n'.join(ln for ln in lines if ln)


def extract_text(path: str) -> str:
    """
    按扩展名抽取正文

    返回: 抽取到的文本（失败时抛异常，调用方记录原因）
    """
    ext = os.path.splitext(path)[1].lower()

    if ext in UNSUPPORTED_EXTENSIONS:
        raise RuntimeError(UNSUPPORTED_EXTENSIONS[ext])
    if ext in ('.csv', '.tsv'):
        return extract_csv_file(path)[:MAX_DOC_CHARS]
    if ext in TEXT_EXTENSIONS:
        return read_text_file(path)[:MAX_DOC_CHARS]
    if ext in ('.html', '.htm'):
        return extract_html(read_text_file(path))[:MAX_DOC_CHARS]
    if ext == '.docx':
        return extract_docx(path)[:MAX_DOC_CHARS]
    if ext == '.xlsx':
        return extract_xlsx(path)[:MAX_DOC_CHARS]
    if ext == '.pptx':
        return extract_pptx(path)[:MAX_DOC_CHARS]
    if ext == '.pdf':
        return extract_pdf(path)[:MAX_DOC_CHARS]
    if ext == '.hdx':
        return extract_hdx(path)
    raise RuntimeError(f"暂不支持的文件类型: {ext or '(无扩展名)'}")


# ======================================================================
# 取回命令清单
# ======================================================================
_FENCE_RE = re.compile(r'```[^\n]*\n(.*?)```', re.S)
# 一行命令的样子：英文开头，只含命令常用字符（中文说明、Markdown 标记都不算）
_COMMAND_LINE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_\-./: ]*$')


def extract_commands(text: str, limit: int = 200) -> List[str]:
    """
    从资料正文里取回命令清单（save_commands 的逆操作）

    优先取代码块里的行；没有代码块时退化成"看起来像命令的 ASCII 行"。
    取不到就返回空列表 —— 调用方据此判断缓存未命中。
    """
    body = text or ""
    blocks = _FENCE_RE.findall(body)
    lines: List[str] = []
    for block in blocks:
        lines.extend(block.splitlines())
    if not lines:
        lines = body.splitlines()

    commands: List[str] = []
    for raw in lines:
        stripped = raw.strip()
        # 先判围栏再剥反引号：否则 "```text" 会被剥成 "text" 当成命令
        if not stripped or stripped.startswith("```"):
            continue
        line = stripped.strip('`').strip()
        line = re.sub(r'^(\d+[.、)]|[-*•>])\s*', '', line).strip()
        if not line or len(line) > 80 or not _COMMAND_LINE_RE.match(line):
            continue
        if line not in commands:
            commands.append(line)
        if len(commands) >= limit:
            break
    return commands


# 命令行里允许出现的字符（参数占位符里的 <>、示例里的逗号点号都不算）
_CLI_TOKEN_RE = re.compile(r'^[A-Za-z0-9\-./_*]+$')
_CLI_FIRST_RE = re.compile(r'^[a-z][a-z0-9\-]{1,15}$')
_CJK_RE = re.compile(r'[\u4e00-\u9fff]')
#: 命令里不会出现的标点（出现就说明这行是正文/表格，不是命令）
_CLI_BAD_CHARS = ",.;:()[]{}\"'=<>|、，；：（）【】"


def find_command_candidates(text: str, limit: int = 300) -> List[str]:
    """
    从文档正文里粗筛出"看起像设备命令"的行，交给 AI 整理成正式清单

    厂商手册里的命令一般是独立成行的纯英文小写 CLI。带中文、带标点、
    超过 8 个词、首词不像命令动词的行一律跳过 —— 目的是给模型一份干净的
    候选，别把大段正文塞进去（既省额度，也让整理结果更准）。
    """
    candidates: List[str] = []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line or len(line) > 120 or _CJK_RE.search(line):
            continue
        if any(ch in line for ch in _CLI_BAD_CHARS):
            continue
        tokens = line.split()
        if not (1 <= len(tokens) <= 8):
            continue
        if not _CLI_FIRST_RE.match(tokens[0]):
            continue
        if any(not _CLI_TOKEN_RE.match(token) for token in tokens):
            continue
        if line not in candidates:
            candidates.append(line)
        if len(candidates) >= limit:
            break
    return candidates


def even_sample(items, count: int):
    """
    从头到尾均匀取 count 个（顺序保持不变）

    厚手册里的命令要跨章节采样：只取前 N 条会全是前几章的命令，
    后面章节（接口/VLAN/生成树…）的命令根本轮不上。
    """
    items = list(items or [])
    if count <= 0 or len(items) <= count:
        return items
    step = len(items) / count
    return [items[int(index * step)] for index in range(count)]



_TOKEN_ASCII_RE = re.compile(r'[a-zA-Z0-9_\-./:]{2,}')
_TOKEN_CJK_RE = re.compile(r'[\u4e00-\u9fff]+')


def tokenize(text: str) -> List[str]:
    """
    分词：英文/数字按词，中文按二元组（bigram）

    中文不分词也能有不错的召回，命令手册里的关键词多为英文命令，
    所以英文按完整词匹配，中文用 bigram 兼顾准确率。
    """
    if not text:
        return []
    low = text.lower()
    tokens = _TOKEN_ASCII_RE.findall(low)
    for block in _TOKEN_CJK_RE.findall(low):
        if len(block) == 1:
            tokens.append(block)
        else:
            tokens.extend(block[i:i + 2] for i in range(len(block) - 1))
    return tokens


# ------------------------------------------------------------ 厂家识别
# 资料按"哪个厂家的设备"分类（用户 2026-10-17：分类需求分为哪个厂家的设备即可）。
# 判定是**纯文本**猜的：文件名认不出就读正文开头 —— 不联网、不改文件。
VENDOR_LABELS = {
    "huawei": "华为",
    "h3c": "新华三(H3C)",
    "ruijie": "锐捷",
    "tplink": "TP-LINK",
    "cisco": "思科",
    "unknown": "未识别",
}
VENDOR_KEYWORDS = {
    "huawei": ("huawei", "华为", "坤灵", "cloudengine", "oceanstor", "airengine",
               "s12700", "s16700", "s5700", "s6700", "s5730", "s6730", "ce6865",
               "ce8850", "ne40", "ar6000", "usg6", "cloudengine"),
    "h3c": ("h3c", "新华三", "华三", "comware", "s5560", "s6850", "s5130", "s6520",
            "s12500", "msr", "wa2600", "hcl"),
    "ruijie": ("ruijie", "锐捷", "rgos", "rg-", "eg-", "s2910", "s5750", "nbs"),
    "tplink": ("tplink", "tp-link", "tp link", "普联", "tl-sg"),
    "cisco": ("cisco", "思科", "catalyst", "nexus", "ios-xe", "ios-xr"),
}
VENDOR_HEAD_BYTES = 1200
VENDOR_INDEX_FILENAME = ".vendors.json"


def guess_vendor(text: str) -> str:
    """从文件名或正文开头猜厂家：取**最先出现**的关键词（都认不出返回 'unknown'）

    为什么用"最先出现"而不是"出现次数最多"：厂家文档里常拿别的厂家做对标
    （比如 H3C 手册里提到华为），最先出现的厂家名通常就是文档自己的厂家；
    而且这样只读文件开头就够，不必读完 700 MB 的索引。
    """
    low = (text or "").lower()
    if not low:
        return "unknown"
    vendor = ""
    at_best = -1
    for name, words in VENDOR_KEYWORDS.items():
        for word in words:
            at = low.find(word)
            if at >= 0 and (not vendor or at < at_best):
                vendor, at_best = name, at
    return vendor or "unknown"


def vendor_label(vendor: str) -> str:
    """厂家 id → 界面上的中文名"""
    return VENDOR_LABELS.get(vendor or "unknown", vendor)


class KnowledgeBase:
    """
    本地资料库

    参数:
        root_dir: 资料库目录，默认 ~/.aiterminal/knowledge
        enabled: 是否启用
        chunk_size: 单个检索块的目标字符数
    """

    def __init__(self, root_dir: str = None, enabled: bool = True,
                 chunk_size: int = 800):
        self.root = root_dir or DEFAULT_KNOWLEDGE_DIR
        self.enabled = enabled
        self.chunk_size = max(200, int(chunk_size))
        self._index: Dict[str, dict] = {}
        self._chunk_cache: List[dict] = []
        self._token_cache: List[Counter] = []
        self._df: Counter = Counter()
        self._ready = False
        self._index_lock = threading.Lock()  # 串行化索引构建，避免并发写坏缓存
        self.last_error = ""
        # ⚠ 这里**不**加载索引：.index.json 本机实测 676 MB，读成 Python 对象要好几 GB
        # 常驻内存，而多数时候用户根本不用资料库（要跑模拟器时更是白占）。
        # 真正用到时由 _ensure_index_loaded() 惰性加载（见 load_index 上方的说明）。
        self._index_loaded = False

    # ------------------------------------------------------------ 目录
    @property
    def index_path(self) -> str:
        return os.path.join(self.root, INDEX_FILENAME)

    def ensure_dir(self) -> bool:
        """确保资料库目录存在"""
        try:
            os.makedirs(self.root, exist_ok=True)
            return True
        except Exception as e:
            self.last_error = f"无法创建资料库目录: {e}"
            return False

    def set_root(self, path: str):
        """更换资料库目录"""
        if not path:
            return
        self.root = os.path.abspath(path)
        self._index = {}
        self._ready = False
        self._index_loaded = False

    def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)

    # ------------------------------------------------------------ 内存占用
    def _ensure_index_loaded(self):
        """惰性加载磁盘索引 —— **谁用谁加载**。

        以前是在 `__init__` 里无条件 `load_index()`：`.index.json` 本机实测 676 MB
        （27,297 篇 / 518,512 段），读成 Python 对象后常驻内存好几 GB，而大多数时候
        用户根本不用资料库 —— 要跑 EVE-NG / eNSP 这类模拟器时，这些内存就是白占
        （实测主进程曾到 7.9 GB）。改成本方法后：应用启动不碰索引，第一次真正
        检索 / 列目录 / 建索引时才读。
        """
        if self._index_loaded:
            return
        self.load_index()
        self._index_loaded = True

    def memory_mb(self) -> float:
        """粗估当前内存索引 + 检索缓存占多少 MB（只给界面提示用，不求精确）

        ⚠ 这是**下界**：只数文本长度，没算 Python 对象开销（dict/list/str 头），
        实际常驻内存通常是它的好几倍。要"真实释放量"用 `release()` 的 RSS 差值。
        """
        total = 0
        for key, entry in self._index.items():
            total += len(key) + 64
            for chunk in (entry.get("chunks") or ()):
                total += len(chunk) + 64
        for chunk in self._chunk_cache:
            total += len(chunk.get("text") or "") + 128
        total += len(self._token_cache) * 96
        return total / 1048576.0

    def release(self) -> float:
        """把内存里的索引与检索缓存全丢掉，返回**实测释放的 MB**。

        索引本身还在磁盘上（`.index.json`），下次检索会自动重新加载。
        用途：界面上的「释放内存」，或用户要跑模拟器等吃内存的程序时腾地方。

        返回值优先用进程 RSS 前后差（Windows 实测准），拿不到才退回文本估算。
        """
        before = process_rss_mb()
        estimate = self.memory_mb()
        self._index = {}
        self._chunk_cache = []
        self._token_cache = []
        self._df = Counter()
        self._ready = False
        self._index_loaded = False
        gc.collect()
        after = process_rss_mb()
        delta = before - after
        return delta if delta > 1 else estimate

    # ------------------------------------------------------------ 导入
    def import_paths(self, paths, move: bool = False, recursive: bool = True,
                     subdir: str = "") -> dict:
        """
        把文件/文件夹导入资料库

        参数:
            paths: 文件或目录路径列表
            move: True=移动（导入后删除源文件），False=复制（默认，更安全）
            recursive: 目录是否递归
            subdir: 在资料库下再建一层子目录（例如按设备型号分类）
        返回:
            {"added": [文件名…], "skipped": [文件名…], "errors": [(路径, 原因)…],
             "extracted": [(文件名, 原因)…]}
        """
        result = {"added": [], "skipped": [], "errors": [], "extracted": []}
        if not self.ensure_dir():
            result["errors"].append((self.root, self.last_error))
            return result

        target_dir = os.path.join(self.root, subdir) if subdir else self.root
        os.makedirs(target_dir, exist_ok=True)

        for raw_path in paths or []:
            path = os.path.abspath(str(raw_path))
            if os.path.isdir(path):
                self._import_directory(path, target_dir, move, recursive, result)
            elif os.path.isfile(path):
                self._import_file(path, target_dir, move, result)
            else:
                result["errors"].append((path, "路径不存在"))

        self._ready = False  # 让下次检索重建索引
        return result

    def _import_directory(self, src_dir: str, target_dir: str, move: bool,
                          recursive: bool, result: dict):
        """导入整个目录"""
        folder_name = os.path.basename(src_dir.rstrip('\\/')) or "导入的资料"
        dest_dir = os.path.join(target_dir, folder_name)
        os.makedirs(dest_dir, exist_ok=True)

        for entry in sorted(os.listdir(src_dir)):
            full = os.path.join(src_dir, entry)
            if os.path.isfile(full):
                self._import_file(full, dest_dir, move, result)
            elif recursive and os.path.isdir(full):
                self._import_directory(full, dest_dir, move, result)

    def _import_file(self, src: str, target_dir: str, move: bool, result: dict):
        """导入单个文件"""
        name = os.path.basename(src)
        ext = os.path.splitext(name)[1].lower()

        # zip 包：解出里面的受支持文件
        if ext == '.zip':
            self._import_zip(src, target_dir, result)
            return

        # hdx 包：存**抽取后的正文**，而不是把几百 MB 的二进制拷进资料库
        # （27 个华为产品包 = 8 GB，直接拷进来纯属浪费，检索又只用得上正文）
        if ext == '.hdx':
            self._import_hdx(src, target_dir, result)
            return

        if ext and ext not in SUPPORTED_EXTENSIONS:
            result["skipped"].append(name)
            reason = UNSUPPORTED_EXTENSIONS.get(ext, f"暂不支持的类型 {ext}")
            result["errors"].append((src, reason))
            return

        dest = self._unique_path(os.path.join(target_dir, name))
        try:
            if move:
                shutil.move(src, dest)
            else:
                shutil.copy2(src, dest)
        except Exception as e:
            result["errors"].append((src, f"复制失败: {e}"))
            return

        result["added"].append(os.path.basename(dest))
        # 立刻尝试抽取一次，导入时就能发现"打不开"的文件
        try:
            text = extract_text(dest)
            if not text.strip():
                result["extracted"].append((os.path.basename(dest), "未抽取到文本（可能是扫描件）"))
        except Exception as e:
            result["extracted"].append((os.path.basename(dest), str(e)))

    def _import_zip(self, src: str, target_dir: str, result: dict):
        """解压 zip 中的受支持文件（按压缩包名建子目录）"""
        folder = os.path.splitext(os.path.basename(src))[0]
        dest_dir = os.path.join(target_dir, folder)
        try:
            with zipfile.ZipFile(src) as zf:
                members = [m for m in zf.namelist() if not m.endswith('/')]
                usable = [m for m in members
                          if os.path.splitext(m)[1].lower() in SUPPORTED_EXTENSIONS]
                if not usable:
                    result["skipped"].append(os.path.basename(src))
                    result["errors"].append((src, "压缩包内没有可解析的文件"))
                    return
                for member in usable:
                    try:
                        data = zf.read(member)
                    except Exception as e:
                        result["errors"].append((f"{src}::{member}", f"解压失败: {e}"))
                        continue
                    # 防止 zip slip
                    safe_name = os.path.basename(member)
                    os.makedirs(dest_dir, exist_ok=True)
                    dest = self._unique_path(os.path.join(dest_dir, safe_name))
                    with open(dest, 'wb') as f:
                        f.write(data)
                    result["added"].append(os.path.relpath(dest, self.root))
        except zipfile.BadZipFile:
            result["errors"].append((src, "不是有效的 zip 文件"))
        except Exception as e:
            result["errors"].append((src, f"处理压缩包失败: {e}"))

    #: hdx 单个产品正文动辄几 MB ~ 几十 MB，远超 MAX_DOC_CHARS。
    #: 按这个大小切片，既保证**全文都能入库**，又不让单个文档大到拖垮索引。
    HDX_CHUNK_CHARS = 350_000

    def _import_hdx(self, src: str, target_dir: str, result: dict):
        """华为 .hdx：抽出正文并**分片**存成多个 `.txt`。

        为什么不直接拷原文件：.hdx 单个 20 MB ~ 440 MB，全量 27 个产品约 8 GB；
        检索只用得上正文。存文本既省空间，也让索引更干净。
        为什么分片：`extract_text()` 有 MAX_DOC_CHARS 上限，若只存一份，
        24k 页的文档只能进前 200 页 —— 分片才能覆盖全文。
        """
        stem = os.path.splitext(os.path.basename(src))[0]
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as e:                                    # noqa: BLE001
            result["errors"].append((src, f"建目录失败: {e}"))
            return
        buf: List[str] = []
        total = 0
        index = 0
        try:
            for text in iter_hdx_pages(src):
                buf.append(text)
                total += len(text)
                if total < self.HDX_CHUNK_CHARS:
                    continue
                index += 1
                self._write_hdx_chunk(stem, index, buf, target_dir, result)
                buf, total = [], 0
            if buf:
                index += 1
                self._write_hdx_chunk(stem, index, buf, target_dir, result)
        except Exception as e:                                    # noqa: BLE001
            result["errors"].append((src, f"解析 hdx 失败: {e}"))
            return
        if index == 0:
            result["errors"].append((src, "hdx 内没有可用正文"))

    def _write_hdx_chunk(self, stem: str, index: int, buf: List[str],
                         target_dir: str, result: dict):
        suffix = "" if index == 1 else "_part%d" % index
        dest = self._unique_path(os.path.join(target_dir, stem + suffix + ".txt"))
        head = "# %s%s\n\n" % (stem, ("（第 %d 部分）" % index) if index > 1 else "")
        try:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(head + "\n\n".join(buf))
        except Exception as e:                                    # noqa: BLE001
            result["errors"].append((dest, f"写入失败: {e}"))
            return
        result["added"].append(os.path.basename(dest))

    @staticmethod
    def _unique_path(path: str) -> str:
        """目标已存在时自动加序号，避免覆盖用户已有资料"""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        index = 2
        while os.path.exists(f"{base}_{index}{ext}"):
            index += 1
        return f"{base}_{index}{ext}"

    def import_text(self, name: str, content: str) -> Optional[str]:
        """把一段文本（如网页复制的内容）存成资料文件"""
        if not self.ensure_dir():
            return None
        name = (name or "").strip() or f"剪贴内容_{time.strftime('%Y%m%d_%H%M%S')}"
        if not os.path.splitext(name)[1]:
            name += ".md"
        dest = self._unique_path(os.path.join(self.root, name))
        try:
            with open(dest, 'w', encoding='utf-8') as f:
                f.write(content or "")
        except Exception as e:
            self.last_error = f"保存失败: {e}"
            return None
        self._ready = False
        return dest

    # ------------------------------------------------------------ 知识沉淀
    NOTE_CATEGORIES = ("AI笔记", "命令速查", "配置案例", "排错记录", "巡检记录",
                       EXPERIENCE_CATEGORY)

    def save_note(self, title: str, content: str, category: str = "AI笔记",
                  tags=None, meta: dict = None) -> Optional[str]:
        """
        把一段内容（AI 回答 / 命令清单 / 排错记录）存成资料文件

        存进去的文件会参与后续检索，等于把"查到的命令"沉淀成自己的知识库。

        参数:
            title: 标题，同时作为文件名
            content: 正文（Markdown 或纯文本）
            category: 分类，作为资料库下的子目录
            tags: 标签列表或逗号分隔字符串
            meta: 附加信息，如 {"设备": "核心交换机", "厂商": "h3c", "任务": "…"}
        返回:
            保存后的文件路径，失败返回 None
        """
        if not self.ensure_dir():
            return None

        title = (title or "").strip() or f"笔记_{time.strftime('%Y%m%d_%H%M%S')}"
        title = title.replace('\n', ' ').strip()
        category = (category or "AI笔记").strip() or "AI笔记"

        if isinstance(tags, str):
            tag_list = [t.strip() for t in re.split(r'[,，;；]', tags) if t.strip()]
        else:
            tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()]

        target_dir = os.path.join(self.root, category)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as e:
            self.last_error = f"创建分类目录失败: {e}"
            return None

        filename = re.sub(r'[\\/:*?"<>|]+', '_', title)[:80].strip() or "笔记"
        dest = self._unique_path(os.path.join(target_dir, filename + ".md"))

        lines = [f"# {title}", ""]
        lines.append(f"> 记录时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"> 分类: {category}")
        if tag_list:
            lines.append(f"> 标签: {', '.join(tag_list)}")
        for key, value in (meta or {}).items():
            if value:
                lines.append(f"> {key}: {value}")
        lines.append("")
        lines.append("## 内容")
        lines.append("")
        lines.append((content or "").strip())
        lines.append("")
        lines.append("---")
        lines.append("*由 AITerminal 保存，可被本地资料库检索*")

        try:
            with open(dest, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            self.last_error = f"保存笔记失败: {e}"
            return None

        self._ready = False
        return dest

    def save_commands(self, title: str, commands, category: str = "命令速查",
                      tags=None, meta: dict = None, note: str = "") -> Optional[str]:
        """
        把一串命令存成资料文件（命令清单类笔记的便捷封装）
        """
        if isinstance(commands, str):
            commands = [line for line in commands.splitlines() if line.strip()]
        body_lines = ["```", *[str(c).strip() for c in (commands or []) if str(c).strip()], "```"]
        if note:
            body_lines += ["", note]
        return self.save_note(title, "\n".join(body_lines), category, tags, meta)

    # ------------------------------------------------------------ 管理
    # ------------------------------------------------------------ 厂家识别
    def vendor_index_path(self) -> str:
        """厂家缓存文件（以 . 开头，list_documents 会跳过它）"""
        return os.path.join(self.root, VENDOR_INDEX_FILENAME)

    def load_vendor_index(self) -> dict:
        try:
            with open(self.vendor_index_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_vendor_index(self, index: dict) -> None:
        try:
            with open(self.vendor_index_path(), 'w', encoding='utf-8') as f:
                json.dump(index, f, ensure_ascii=False)
        except OSError:
            pass

    def identify_vendors(self, documents: List[dict] = None) -> dict:
        """给每篇资料认厂家：文件名认得出就用名字，认不出再读正文开头。

        结果缓存在资料库根的 .vendors.json（按「文件名 → [修改时间, 厂家]」），
        27k 篇也只认一次，之后打开资料管理不再读盘。返回 {文件名: 厂家}。
        """
        if documents is None:
            documents = self.list_documents()
        cache = self.load_vendor_index()
        fresh: Dict[str, list] = {}
        for document in documents:
            name = document.get("name") or self._rel(document.get("path", ""))
            mtime = float(document.get("mtime") or 0)
            cached = cache.get(name)
            vendor = None
            if isinstance(cached, list) and len(cached) == 2:
                try:
                    if abs(float(cached[0]) - mtime) < 0.5:
                        vendor = str(cached[1])
                except (TypeError, ValueError):
                    vendor = None
            if vendor is None:
                vendor = guess_vendor(name)
                if vendor == "unknown":
                    vendor = guess_vendor(self._read_head(document.get("path", "")))
            fresh[name] = [mtime, vendor]
            document["vendor"] = vendor
        self.save_vendor_index(fresh)
        return fresh

    @staticmethod
    def _read_head(path: str, limit: int = VENDOR_HEAD_BYTES) -> str:
        """读文件开头（认厂家只认开头附近出现的厂商标识，不读全文）"""
        try:
            with open(path, 'rb') as f:
                raw = f.read(limit)
        except OSError:
            return ""
        return raw.decode('utf-8', errors='ignore')

    @property
    def index_loaded(self) -> bool:
        """磁盘索引是否已在内存里（界面用它决定要不要显示片段数）"""
        return bool(self._index_loaded)

    def list_documents(self, light: bool = False) -> List[dict]:
        """列出资料库里的文件

        light=True：**不加载索引**，只走目录扫描（片段数/解析错误报 0/空）。
        给"启动时刷一下状态栏"这种场景用 —— 索引 676 MB，读进来就是 883 MB 内存，
        用户要跑模拟器时不能因为刷新个状态就把它读进来。
        """
        if not os.path.isdir(self.root):
            return []
        if not light:
            self._ensure_index_loaded()
        documents = []
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for filename in filenames:
                if filename == INDEX_FILENAME or filename.startswith('.'):
                    continue
                full = os.path.join(dirpath, filename)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                ext = os.path.splitext(filename)[1].lower()
                entry = {} if light else self._index.get(self._rel(full), {})
                error = entry.get("error", "")
                if not error and ext in UNSUPPORTED_EXTENSIONS:
                    error = UNSUPPORTED_EXTENSIONS[ext]
                documents.append({
                    "name": os.path.relpath(full, self.root),
                    "path": full,
                    "ext": ext or "(无扩展名)",
                "vendor": guess_vendor(filename),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "mtime_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                    "supported": ext in SUPPORTED_EXTENSIONS,
                    "chunks": entry.get("chunk_count", 0),
                    "error": error,
                })
        documents.sort(key=lambda d: d["mtime"], reverse=True)
        return documents

    def remove_document(self, name_or_path: str) -> bool:
        """删除资料库里的文件"""
        path = name_or_path
        if not os.path.isabs(path):
            path = os.path.join(self.root, name_or_path)
        if not os.path.abspath(path).startswith(os.path.abspath(self.root)):
            return False  # 只允许删除资料库内的文件

        # 资料库里其实有**两类**条目：
        #   1) 导入的文件           —— 文件 + 索引
        #   2) save_commands()/save_note() 创建的 —— **只写进索引，不落盘**
        # 第二类没有文件可删，但索引条目必须能删掉。
        # 不处理这种情况时的表现极具迷惑性：调用方按名字删，返回 False，
        # 文件系统里也确实找不到那个名字 —— 看起来"这个条目不存在"，
        # 但它一直留在索引里参与检索。
        self._ensure_index_loaded()
        rel = self._rel(path)
        if not os.path.exists(path):
            if rel in self._index:
                self._index.pop(rel, None)
                self._ready = False
                self.save_index()
                return True
            return False

        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception as e:
            self.last_error = f"删除失败: {e}"
            return False
        self._index.pop(self._rel(path), None)
        self._ready = False
        self.save_index()
        return True

    def document_count(self) -> int:
        return len(self.list_documents())

    # ------------------------------------------------------------ 索引
    def load_index(self):
        """读取索引缓存"""
        try:
            if os.path.exists(self.index_path):
                with open(self.index_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("version") == INDEX_VERSION:
                    self._index = data.get("documents") or {}
        except Exception:
            self._index = {}

    def save_index(self):
        """写入索引缓存"""
        self._ensure_index_loaded()   # 没加载就写 = 把磁盘上 27k 篇的索引清空，必须挡住
        if not self.ensure_dir():
            return False
        try:
            with open(self.index_path, 'w', encoding='utf-8') as f:
                json.dump({"version": INDEX_VERSION, "root": self.root,
                           "updated": time.time(), "documents": self._index},
                          f, ensure_ascii=False)
            return True
        except Exception as e:
            self.last_error = f"索引写入失败: {e}"
            return False

    def build_index(self, force: bool = False) -> dict:
        """
        建立/更新索引

        按文件修改时间判断是否需要重建，未变化的文件直接复用缓存。
        用锁串行化：界面可能在后台线程建索引，用户又同时触发一次检索/重建。
        """
        self._ensure_index_loaded()   # 没加载就重建 = 27k 篇全部重抽文本（小时级），必须挡住
        with self._index_lock:
            return self._build_index_locked(force)

    def _build_index_locked(self, force: bool = False) -> dict:
        summary = {"documents": 0, "chunks": 0, "reused": 0, "failed": []}
        if not os.path.isdir(self.root):
            self._ready = True
            return summary

        for dirpath, _dirnames, filenames in os.walk(self.root):
            for filename in filenames:
                if filename == INDEX_FILENAME or filename.startswith('.'):
                    continue
                full = os.path.join(dirpath, filename)
                key = self._rel(full)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue

                cached = self._index.get(key)
                if (not force and cached
                        and cached.get("mtime") == stat.st_mtime
                        and cached.get("size") == stat.st_size):
                    summary["documents"] += 1
                    summary["chunks"] += cached.get("chunk_count", 0)
                    summary["reused"] += 1
                    continue

                try:
                    text = extract_text(full)
                except Exception as e:
                    self._index[key] = {"mtime": stat.st_mtime, "size": stat.st_size,
                                        "error": str(e), "chunks": [], "chunk_count": 0}
                    summary["failed"].append((filename, str(e)))
                    continue

                if not text.strip():
                    self._index[key] = {"mtime": stat.st_mtime, "size": stat.st_size,
                                        "error": "未抽取到文本（可能是扫描件或图片型 PDF）",
                                        "chunks": [], "chunk_count": 0}
                    summary["failed"].append((filename, "未抽取到文本"))
                    continue

                chunks = self._split_chunks(text)
                self._index[key] = {"mtime": stat.st_mtime, "size": stat.st_size,
                                    "error": "", "chunks": chunks,
                                    "chunk_count": len(chunks)}
                summary["documents"] += 1
                summary["chunks"] += len(chunks)

        self.save_index()
        self._ready = False
        return summary

    def _split_chunks(self, text: str) -> List[str]:
        """按行聚合成检索块，块之间保留一行重叠以便命令跨行时不丢上下文"""
        lines = text.split('\n')
        chunks, current, size = [], [], 0
        for line in lines:
            if not line.strip():
                continue
            current.append(line)
            size += len(line) + 1
            if size >= self.chunk_size:
                chunks.append('\n'.join(current))
                current = current[-1:]  # 重叠一行
                size = sum(len(x) + 1 for x in current)
        if current:
            chunks.append('\n'.join(current))
        return chunks

    def _ensure_ready(self):
        """首次检索前构建内存索引（词频统计）"""
        self._ensure_index_loaded()
        if self._ready:
            return
        self._chunk_cache = []
        self._token_cache = []
        self._df = Counter()

        for key, entry in self._index.items():
            if entry.get("error"):
                continue
            for index, chunk in enumerate(entry.get("chunks") or []):
                tokens = tokenize(chunk)
                if not tokens:
                    continue
                counter = Counter(tokens)
                self._chunk_cache.append({
                    "source": key,
                    "chunk_index": index,
                    "text": chunk,
                    "name_tokens": set(tokenize(os.path.basename(key))),
                })
                self._token_cache.append(counter)
                for token in counter:
                    self._df[token] += 1
        self._ready = True

    # ------------------------------------------------------------ 检索
    def search(self, query: str, top_k: int = 4) -> List[dict]:
        """
        在本地资料库中检索

        返回: [{"source": 文件名, "chunk_index": 段号, "score": 分数,
                "text": 片段, "matched": [命中词…]}]
        """
        query = (query or "").strip()
        if not query or not self.enabled:
            return []

        self._ensure_ready()
        if not self._chunk_cache:
            return []

        query_tokens = list(dict.fromkeys(tokenize(query)))
        if not query_tokens:
            return []

        # 查询里的英文/数字连起来，用于"短语整体命中"加成。
        # 命令类查询（display interface brief）真正的信号是**这几个词连着出现**，
        # 而不是它们各自在页面里出现多少次 —— 实测中 BGP 命令列表因为 display
        # 出现几十次而排在真正的 interface brief 文档前面，就是缺这一项。
        ascii_phrase = " ".join(t for t in query_tokens
                                if t.isascii() and t.isalnum())
        total = len(self._chunk_cache)

        # 查询词分档加权 —— 中文查询召回的根因所在。
        # 文档正文里"配置""查看"这类动作词出现在上百篇里，IDF 早已被稀释成噪声；
        # 而 VLAN / OSPF / S6700 这种技术术语才真正决定该召回哪一篇。
        # 不区分两者时，"怎么配置 VLAN"会把 igmp-snooping 排到 VLAN 配置文档前面。
        _CJK_STOP = {
            "怎么", "如何", "什么", "哪些", "哪个", "为什", "配置", "查看",
            "显示", "命令", "信息", "方法", "步骤", "使用", "进行", "可以", "如果",
            "那么", "需要", "一个", "这个", "的话", "时候", "支持", "多少", "相关",
            "介绍", "说明", "举例", "示例", "是否", "能否", "怎样", "有无",
        }

        def query_weight(token: str) -> float:
            if token.isascii() and any(c.isalnum() for c in token):
                # 含数字或全大写 = 型号/缩写（s5700 / vlan / ospf / acl）
                if any(c.isdigit() for c in token) or token.isupper():
                    return 4.0
                return 3.0 if len(token) >= 3 else 1.5
            return 0.15 if token in _CJK_STOP else 1.0

        weights = {t: query_weight(t) for t in query_tokens}

        # 中文概念 -> 英文命令词。
        # 这是本产品最核心的用法：**工程师用中文提问，文档里写的是英文命令**。
        # 用户问"怎么查看接口状态"，文档标题是 display interface brief；
        # 中文 bigram 永远匹配不上英文字符串，必须靠这层映射搭桥。
        _CONCEPT_EN = {
            "查看": "display show", "显示": "display show", "查询": "display show",
            "看看": "display show", "检查": "display check",
            "接口": "interface", "端口": "interface port",
            "状态": "status brief", "版本": "version",
            "设备": "device", "单板": "device board slot",
            "配置": "configuration config", "路由": "route routing",
            "地址": "address", "表项": "table entry",
            "光模块": "optical module transceiver",
            "用户": "user", "日志": "log", "告警": "alarm",
            "保存": "save", "重启": "reboot", "升级": "upgrade",
            "备份": "backup", "恢复": "restore",
            "聚合": "eth-trunk link-aggregation", "堆叠": "stack",
            "生成树": "stp spanning-tree", "镜像": "mirror",
            "安全": "security", "认证": "authentication",
            "带宽": "bandwidth", "流量": "traffic",
        }
        # 泛化词：在网络文档里"设备""信息""命令"这类词几乎每篇都有，
        # 映射出来的英文词区分度很低。实测"怎么查看设备版本"会因为
        # 设备->device 让 display_poe_device 与 display_version 打平，
        # 而用户真正的意图是 version。所以这类词降权。
        _GENERIC_CONCEPT = {"设备", "信息", "命令", "系统", "功能", "情况",
                            "相关", "内容", "方式", "操作"}
        for token in list(query_tokens):
            for en in _CONCEPT_EN.get(token, "").split():
                if en not in weights:
                    query_tokens.append(en)
                    # 中等权重：它是推理出来的，不该压过用户明写的词，
                    # 但要足以把对口文档从噪声里拉上来；泛化词再低一档。
                    weights[en] = 1.2 if token in _GENERIC_CONCEPT else 2.5

        # 权重之和作为覆盖率的分母：命中一个 VLAN 远比命中三个动作词有价值
        weight_sum = sum(weights.values()) or 1.0

        scores = []
        for position, counter in enumerate(self._token_cache):
            score = 0.0
            matched = []
            matched_weight = 0.0
            for token in query_tokens:
                tf = counter.get(token, 0)
                if not tf:
                    continue
                idf = math.log(1 + total / (1 + self._df.get(token, 0))) + 0.1
                # 词频封顶：一个词重复 50 次不该比"多命中几个不同的词"更有价值。
                # 不封顶时常见词（display）会压过稀有词（brief），导致答非所问。
                score += (1 + math.log(min(tf, 8))) * idf * weights[token]
                matched.append(token)
                matched_weight += weights[token]
            if score <= 0:
                continue

            text_low = self._chunk_cache[position]["text"].lower()

            # 覆盖率：按**权重**衡量命中了查询的多大分量，而不是按词数 ——
            # 命中一个 VLAN 比命中三个动作词更能说明问题。
            score *= 0.25 + 0.75 * (matched_weight / weight_sum)

            # 短语整体命中：命令查询的最强信号，给足权重让它能压过词频噪声
            if len(ascii_phrase) >= 4 and ascii_phrase in text_low:
                score += 8.0

            # 文件名命中：用户常按型号搜，而型号往往只出现在文件名里
            name_tokens = self._chunk_cache[position]["name_tokens"]
            score += 2.0 * sum(weights[t] for t in query_tokens if t in name_tokens)

            # 长块归一化，避免长段落总是排前面
            score /= math.sqrt(max(len(text_low), 100)) / 30
            scores.append((score, position, matched))

        scores.sort(key=lambda item: item[0], reverse=True)

        results = []
        seen = set()
        for score, position, matched in scores:
            chunk = self._chunk_cache[position]
            signature = (chunk["source"], chunk["text"][:80])
            if signature in seen:
                continue
            seen.add(signature)
            results.append({
                "source": chunk["source"],
                "chunk_index": chunk["chunk_index"],
                "score": round(score, 2),
                "text": chunk["text"],
                "matched": matched[:8],
            })
            if len(results) >= max(1, top_k):
                break
        return results

    def search_text(self, query: str, top_k: int = 4,
                    max_chars: int = 4000) -> str:
        """
        检索并拼成给 AI 的上下文文本（无命中时返回空字符串）
        """
        hits = self.search(query, top_k)
        if not hits:
            return ""
        parts = []
        for index, hit in enumerate(hits, start=1):
            parts.append(
                f"[本地资料{index}] {hit['source']}（第 {hit['chunk_index'] + 1} 段）\n"
                f"{hit['text']}"
            )
        return "\n\n".join(parts)[:max_chars]

    def search_category(self, query: str, category: str,
                        top_k: int = 3) -> List[dict]:
        """
        只在某个分类（子目录）内检索

        先按更大范围召回再按目录前缀过滤：分类检索通常只要 2~3 条，
        直接取 top_k 会被其它分类占位，把真正的命中挤掉。
        """
        category = (category or "").strip().strip("/\\")
        if not category:
            return []
        hits = self.search(query, top_k=max(top_k * 6, 12))
        prefix = category.replace("\\", "/") + "/"
        return [hit for hit in hits
                if str(hit.get("source", "")).replace("\\", "/").startswith(prefix)][:top_k]

    def warm_up(self) -> int:
        """
        预热：把内存索引（词频统计）先算好，返回可检索的片段数

        启动后后台调一次，之后第一次提问匹配就不用现算，响应更快。
        """
        self._ensure_ready()
        return len(self._chunk_cache)

    # ------------------------------------------------------------ 工具
    def _rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.root)
        except ValueError:
            return path

    def stats(self, light: bool = False) -> dict:
        """资料库概况（light=True 时不加载索引，片段数按 0 报）"""
        documents = self.list_documents(light=light)
        failed = [d for d in documents if d["error"]]

        # "indexed" 在用户眼里是「资料库索建好了吗」，而 self._ready 表达的是
        # 「**内存里**的检索索引已构建」—— 它是首次 search() 时才置位的懒加载标志。
        # 直接用 _ready 的后果：刚建完索引马上去看概况，却显示"未建索引"，
        # 用户以为白建了、要再点一次重建。这里改看**磁盘上的索引文件**这个持久事实。
        index_ready = False
        try:
            if os.path.exists(self.index_path) and os.path.getsize(self.index_path) > 2:
                index_ready = True
        except OSError:
            index_ready = False

        return {
            "root": self.root,
            "enabled": self.enabled,
            "documents": len(documents),
            "failed": len(failed),
            "chunks": sum(d["chunks"] for d in documents),
            "size": sum(d["size"] for d in documents),
            "indexed": index_ready,
        }
