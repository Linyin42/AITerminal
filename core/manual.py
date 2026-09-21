"""
笔记手册 - 把本地资料库里的笔记批量整理成一份手册

支持:
    · 按分类 / 标签 / 关键词筛选笔记
    · 汇总标签与分类清单（供界面下拉）
    · 导出为合并 Markdown（带目录）
    · 导出为自包含 HTML（带样式与目录，可直接发同事或打印成 PDF）

不依赖任何第三方库。
"""
import html as html_mod
import os
import re
import time
from typing import Dict, List, Optional, Sequence

# 匹配笔记头部的 "> 键: 值" 元信息行
_META_RE = re.compile(r'^>\s*([^:：]{1,12})\s*[:：]\s*(.*)$')
_TITLE_RE = re.compile(r'^#\s+(.*)$', re.MULTILINE)


def parse_note(text: str) -> dict:
    """
    解析一份笔记，取出标题、元信息与正文

    返回: {"title", "meta": {键: 值}, "tags": [标签…], "category", "body"}
    """
    text = text or ""
    title = ""
    match = _TITLE_RE.search(text)
    if match:
        title = match.group(1).strip()

    meta: Dict[str, str] = {}
    body_lines = []
    in_body = False
    for line in text.split('\n'):
        if not in_body:
            found = _META_RE.match(line.strip())
            if found:
                meta[found.group(1).strip()] = found.group(2).strip()
                continue
            if line.strip() == '## 内容':
                in_body = True
                continue
            if line.startswith('#') or not line.strip():
                continue
        body_lines.append(line)

    body = '\n'.join(body_lines).strip()
    # 去掉结尾的署名行
    body = re.sub(r'\n-{3,}\s*\n\*由 AITerminal 保存.*$', '', body).strip()

    tags = []
    if meta.get('标签'):
        tags = [t.strip() for t in re.split(r'[,，;；]', meta['标签']) if t.strip()]

    return {
        "title": title,
        "meta": meta,
        "tags": tags,
        "category": meta.get('分类', ''),
        "body": body,
    }


def load_notes(knowledge_base, documents: Sequence[dict] = None) -> List[dict]:
    """
    读取资料库中的笔记（默认只取 .md，含元信息或分类目录下的）

    参数:
        knowledge_base: KnowledgeBase 实例
        documents: 指定的文档列表（来自 list_documents），为空则自动挑选
    返回:
        [{"name", "path", "title", "meta", "tags", "category", "body", "text"}]
    """
    if documents is None:
        documents = knowledge_base.list_documents()

    notes = []
    for document in documents:
        if not str(document.get("name", "")).lower().endswith(('.md', '.markdown', '.txt')):
            continue
        path = document.get("path") or os.path.join(knowledge_base.root, document.get("name", ""))
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
        except OSError:
            continue
        parsed = parse_note(text)
        notes.append({
            "name": document.get("name", os.path.basename(path)),
            "path": path,
            "title": parsed["title"] or os.path.splitext(os.path.basename(path))[0],
            "meta": parsed["meta"],
            "tags": parsed["tags"],
            "category": parsed["category"] or _guess_category(document.get("name", ""), knowledge_base),
            "body": parsed["body"],
            "text": text,
            "mtime": document.get("mtime", 0),
        })
    notes.sort(key=lambda n: (n["category"], n["title"]))
    return notes


def _guess_category(name: str, knowledge_base=None) -> str:
    """从相对路径里猜分类（第一层子目录）"""
    name = str(name).replace('\\', '/')
    if '/' in name:
        return name.split('/')[0]
    return "未分类"


def collect_tags(notes: Sequence[dict]) -> Dict[str, int]:
    """统计所有标签及出现次数（按次数倒序）"""
    counter: Dict[str, int] = {}
    for note in notes:
        for tag in note.get("tags") or []:
            counter[tag] = counter.get(tag, 0) + 1
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def collect_categories(notes: Sequence[dict]) -> Dict[str, int]:
    """统计分类及笔记数量"""
    counter: Dict[str, int] = {}
    for note in notes:
        category = note.get("category") or "未分类"
        counter[category] = counter.get(category, 0) + 1
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def filter_notes(notes: Sequence[dict], categories=None, tags=None,
                 keyword: str = "") -> List[dict]:
    """
    按分类 / 标签 / 关键词筛选笔记

    参数:
        categories: 分类列表，空表示不限
        tags: 标签列表，命中任意一个即保留；空表示不限
        keyword: 关键词，匹配标题、标签或正文
    """
    categories = [c for c in (categories or []) if c]
    tags = [t for t in (tags or []) if t]
    keyword = (keyword or "").strip().lower()

    result = []
    for note in notes:
        if categories and (note.get("category") or "未分类") not in categories:
            continue
        if tags and not set(tags) & set(note.get("tags") or []):
            continue
        if keyword:
            haystack = " ".join([
                note.get("title", ""), note.get("category", ""),
                " ".join(note.get("tags") or []), note.get("body", ""),
            ]).lower()
            if keyword not in haystack:
                continue
        result.append(note)
    return result


def build_manual(notes: Sequence[dict], title: str = "命令手册",
                 subtitle: str = "") -> str:
    """
    生成合并 Markdown（带目录）

    同一分类的笔记归到同一章节下。
    """
    lines = [f"# {title}"]
    if subtitle:
        lines.append("")
        lines.append(f"> {subtitle}")
    lines.append("")
    lines.append(f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}　|　"
                 f"共 {len(notes)} 篇笔记")
    lines.append("")

    # 目录
    lines.append("## 目录")
    lines.append("")
    index = 0
    entries = []
    for note in notes:
        index += 1
        anchor = f"{index}. {note.get('title', '未命名')}"
        entries.append((note.get("category") or "未分类", anchor))
    current_category = None
    for category, anchor in entries:
        if category != current_category:
            current_category = category
            lines.append(f"- **{category}**")
        lines.append(f"  - {anchor}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 正文
    current_category = None
    for note in notes:
        category = note.get("category") or "未分类"
        if category != current_category:
            current_category = category
            lines.append(f"# {category}")
            lines.append("")
        lines.append(f"## {note.get('title', '未命名')}")
        lines.append("")
        meta = note.get("meta") or {}
        meta_bits = [f"{key}: {value}" for key, value in meta.items() if value]
        if meta_bits:
            lines.append("> " + "　|　".join(meta_bits))
            lines.append("")
        lines.append(note.get("body", "").strip() or "（无正文）")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------------ Markdown → HTML
_CODE_FENCE_RE = re.compile(r'^```(\w*)\s*$')


def markdown_to_html(markdown_text: str) -> str:
    """
    把 Markdown 子集渲染成 HTML

    支持: 标题(#~####)、围栏代码块、引用、有序/无序列表、水平线、
    **加粗**、`行内代码`、链接。够用于本项目的笔记手册。
    """
    lines = (markdown_text or "").split('\n')
    output = []
    in_code = False
    in_list = None  # 'ul' / 'ol'

    def close_list():
        nonlocal in_list
        if in_list:
            output.append(f"</{in_list}>")
            in_list = None

    for raw in lines:
        line = raw.rstrip()

        fence = _CODE_FENCE_RE.match(line.strip())
        if fence:
            if in_code:
                output.append('</code></pre>')
                in_code = False
            else:
                close_list()
                output.append('<pre><code>')
                in_code = True
            continue
        if in_code:
            output.append(html_mod.escape(raw))
            continue

        if not line.strip():
            close_list()
            continue

        if re.match(r'^\s*(-{3,}|\*{3,})\s*$', line):
            close_list()
            output.append('<hr/>')
            continue

        heading = re.match(r'^(#{1,4})\s+(.*)$', line)
        if heading:
            close_list()
            level = len(heading.group(1))
            output.append(f'<h{level}>{_inline(heading.group(2))}</h{level}>')
            continue

        quote = re.match(r'^>\s?(.*)$', line)
        if quote:
            close_list()
            output.append(f'<blockquote>{_inline(quote.group(1))}</blockquote>')
            continue

        bullet = re.match(r'^\s*[-*+]\s+(.*)$', line)
        if bullet:
            if in_list != 'ul':
                close_list()
                output.append('<ul>')
                in_list = 'ul'
            output.append(f'<li>{_inline(bullet.group(1))}</li>')
            continue

        numbered = re.match(r'^\s*\d+[.)]\s+(.*)$', line)
        if numbered:
            if in_list != 'ol':
                close_list()
                output.append('<ol>')
                in_list = 'ol'
            output.append(f'<li>{_inline(numbered.group(1))}</li>')
            continue

        close_list()
        output.append(f'<p>{_inline(line)}</p>')

    if in_code:
        output.append('</code></pre>')
    close_list()
    return "\n".join(output)


def _inline(text: str) -> str:
    """处理行内格式：先转义，再替换标记"""
    text = html_mod.escape(text)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)', r'<a href="\2">\1</a>', text)
    return text


MANUAL_CSS = """
body { font-family: "Microsoft YaHei", "Segoe UI", -apple-system, sans-serif;
       max-width: 900px; margin: 32px auto; padding: 0 20px; color: #24292f;
       line-height: 1.7; background: #fff; }
h1 { border-bottom: 2px solid #d0d7de; padding-bottom: 8px; margin-top: 36px; }
h2 { border-bottom: 1px solid #eaeef2; padding-bottom: 6px; margin-top: 28px; }
h3, h4 { margin-top: 20px; }
code { background: #f6f8fa; padding: 2px 5px; border-radius: 4px;
       font-family: Consolas, "Courier New", monospace; font-size: 90%; }
pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
      padding: 12px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #d0d7de; margin: 8px 0; padding: 4px 14px;
             color: #57606a; background: #f6f8fa; }
hr { border: none; border-top: 1px solid #d0d7de; margin: 28px 0; }
ul, ol { padding-left: 26px; }
li { margin: 3px 0; }
a { color: #0969da; }
table { border-collapse: collapse; margin: 10px 0; }
th, td { border: 1px solid #d0d7de; padding: 5px 10px; }
th { background: #f6f8fa; }
.toc { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
       padding: 12px 20px; margin: 20px 0; }
.toc h2 { border: none; margin: 0 0 8px 0; font-size: 1.1em; }
.meta { color: #57606a; font-size: 92%; }
@media print { body { margin: 0; } }
"""


def build_manual_html(notes: Sequence[dict], title: str = "命令手册",
                      subtitle: str = "") -> str:
    """生成自包含的 HTML 手册（带目录、可直接打印成 PDF）"""
    markdown_text = build_manual(notes, title, subtitle)
    body = markdown_to_html(markdown_text)

    toc_lines = []
    for note in notes:
        toc_lines.append(f'<li>{html_mod.escape(note.get("title", ""))}'
                         f'<span class="meta">　{html_mod.escape(note.get("category") or "")}'
                         f'</span></li>')
    toc_html = ('<div class="toc"><h2>笔记清单（%d 篇）</h2><ol>%s</ol></div>'
                % (len(notes), "".join(toc_lines)))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>{html_mod.escape(title)}</title>
<style>{MANUAL_CSS}</style>
</head>
<body>
{toc_html}
{body}
<hr/>
<p class="meta">由 AITerminal 生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}</p>
</body>
</html>
"""


def export_manual(path: str, notes: Sequence[dict], title: str = "命令手册",
                  subtitle: str = "", fmt: str = None) -> bool:
    """
    导出手册

    参数:
        path: 目标文件
        notes: 笔记列表（load_notes + filter_notes 的结果）
        fmt: 'md' / 'html'，留空则按扩展名推断
    """
    fmt = (fmt or os.path.splitext(path)[1].lstrip('.') or 'md').lower()
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        if fmt in ('html', 'htm'):
            content = build_manual_html(notes, title, subtitle)
        else:
            content = build_manual(notes, title, subtitle)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except Exception:
        return False


def export_notes_csv(path: str, notes: Sequence[dict]) -> bool:
    """把笔记清单导出成 CSV（便于用 Excel 看索引）"""
    import csv
    try:
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["分类", "标题", "标签", "设备", "厂商", "记录时间", "文件"])
            for note in notes:
                meta = note.get("meta") or {}
                writer.writerow([
                    note.get("category", ""), note.get("title", ""),
                    ", ".join(note.get("tags") or []),
                    meta.get("设备", ""), meta.get("厂商", ""),
                    meta.get("记录时间", ""), note.get("name", ""),
                ])
        return True
    except Exception:
        return False
