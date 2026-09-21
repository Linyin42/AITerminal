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
INDEX_FILENAME = ".index.json"
INDEX_VERSION = 1

# 可直接按文本读取的扩展名
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".log", ".cfg", ".conf", ".ini", ".json",
    ".yaml", ".yml", ".csv", ".tsv", ".xml", ".rst", ".text",
}
# 需要解析的扩展名
PARSE_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".zip"}
# 老版 Office 二进制格式，无法用标准库解析
UNSUPPORTED_EXTENSIONS = {
    ".doc": "老版 .doc 无法直接解析，请另存为 .docx 或导出 PDF",
    ".xls": "老版 .xls 无法直接解析，请另存为 .xlsx",
    ".ppt": "老版 .ppt 无法直接解析，请另存为 .pptx",
    ".rar": "暂不支持 .rar，请先解压后再导入",
    ".7z": "暂不支持 .7z，请先解压后再导入",
}

SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | PARSE_EXTENSIONS

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
    raise RuntimeError(f"暂不支持的文件类型: {ext or '(无扩展名)'}")


# ======================================================================
# 分词与检索
# ======================================================================
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
        self.load_index()

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
        self.load_index()

    def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)

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
    NOTE_CATEGORIES = ("AI笔记", "命令速查", "配置案例", "排错记录", "巡检记录")

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
    def list_documents(self) -> List[dict]:
        """列出资料库里的文件"""
        if not os.path.isdir(self.root):
            return []
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
                entry = self._index.get(self._rel(full), {})
                error = entry.get("error", "")
                if not error and ext in UNSUPPORTED_EXTENSIONS:
                    error = UNSUPPORTED_EXTENSIONS[ext]
                documents.append({
                    "name": os.path.relpath(full, self.root),
                    "path": full,
                    "ext": ext or "(无扩展名)",
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

        total = len(self._chunk_cache)
        scores = []
        for position, counter in enumerate(self._token_cache):
            score = 0.0
            matched = []
            for token in query_tokens:
                tf = counter.get(token, 0)
                if not tf:
                    continue
                idf = math.log(1 + total / (1 + self._df.get(token, 0))) + 0.1
                score += (1 + math.log(tf)) * idf
                matched.append(token)
            if score <= 0:
                continue
            # 文件名命中给额外加成（手册名里常带型号，用户常按型号搜）
            name_tokens = self._chunk_cache[position]["name_tokens"]
            score += 0.8 * sum(1 for token in query_tokens if token in name_tokens)
            # 长块归一化，避免长段落总是排前面
            score /= math.sqrt(max(len(self._chunk_cache[position]["text"]), 100)) / 30
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

    # ------------------------------------------------------------ 工具
    def _rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.root)
        except ValueError:
            return path

    def stats(self) -> dict:
        """资料库概况"""
        documents = self.list_documents()
        failed = [d for d in documents if d["error"]]
        return {
            "root": self.root,
            "enabled": self.enabled,
            "documents": len(documents),
            "failed": len(failed),
            "chunks": sum(d["chunks"] for d in documents),
            "size": sum(d["size"] for d in documents),
            "indexed": self._ready,
        }
