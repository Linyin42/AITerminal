# -*- coding: utf-8 -*-
r"""把 knowledge/<vendor>/tech/*.md 切成可检索片段（RAG 层的第 1 步）。

为什么不按"固定字数 + overlap"切（方案 §5.1 的做法）：
  tech/*.md 是**已经按功能切好的卡片**，每个 `### 标题` 小节后面还跟着
      <!-- source: h3c_s6520x-ei-g_r7743pxx/.../003.htm -->
  这行就是**原始文档出处**。按结构切 ⇒ 每个片段天然带"文件#小节 + 原始出处"，
  正好满足本项目"回答必须给出处"的硬要求；按字数切则会
  ① 把 CLI 命令拦腰截断（实测正文里有 "interface ten-gigabitethernet\n1/0/2" 这种折行）
  ② 丢掉出处行的归属关系。

产物: <本地 LLM 根>/index/<vendor>_chunks.jsonl（根目录见 paths.py，默认 D:\ait_llm）
      每行 {"id","vendor","file","section","source","text","chars"}

用法:
    python build_index.py --vendor h3c --chunk
    python build_index.py --vendor h3c --chunk --limit 50 --samples 3
"""
import argparse
import json
import os
import re
import sys

# 知识库目录 = 从脚本所在目录**往上找**最近的 knowledge\：脚本放在 local_llm\（应用内）或
# data_tools\chunk_embed\（随包工具，比根深两层）都能指对；找不到才退回"脚本上一级 + knowledge"。
# 原先写死"上一级" ⇒ 脚本一挪深就指到 data_tools\knowledge（不存在）。
def _find_knowledge():
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        cand = os.path.join(here, "knowledge")
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "knowledge")


KNOWLEDGE = _find_knowledge()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402  （同目录；路径的唯一来源见 paths.py）

OUTDIR = paths.INDEX

MAX_CHARS = 1600      # 单片段目标上限（语义段聚合到此值）
HARD_MAX = 4000       # 单语义段超过此值才兜底再切
OVERLAP = 120         # 二次切分时的重叠
MIN_CHARS = 40        # 低于此长度视为噪音（空小节/只有标题）

HEAD_RE = re.compile(r"^(#{2,4})\s+(.*\S)\s*$")
_NUM_RE = re.compile(r"^\s*(?:\d{1,2}(?:\.\d{1,2}){0,3}|[一二三四五六七八九十]{1,2}[、.])\s+\S")


def is_num_heading(line):
    """判断是否"文档自己的编号小标题"。

    ⚠ 这里的关键是**别把表格数据行当标题**：表里满是这样开头的行
        2000 ～ 2999        （4 位数，且带 ～）
        0000-fc00-a249 100
        1/0/2
    实测把这些当边界会把表格从中间切开，切出 `e`、`s source-wildcard` 这种
    单独看不懂的片段。所以除正则外还要卡长度与分隔符。
    """
    if len(line) > 80 or "|" in line or "～" in line or "~" in line:
        return False
    if line.lstrip().startswith("-"):       # 列表项不是标题
        return False
    return bool(_NUM_RE.match(line))
SOURCE_RE = re.compile(r"<!--\s*source:\s*(.+?)\s*-->")
CATEGORY_RE = re.compile(r"^-\s*功能分类：\s*(.+?)\s*$")


def parse_header(text):
    """取卡片头部元信息（功能分类），用于给每个片段加语境前缀。"""
    cat = ""
    for line in text.splitlines()[:20]:
        m = CATEGORY_RE.match(line)
        if m:
            cat = m.group(1)
            break
    return cat


def iter_sections(text):
    """按 ## / ### 标题切段，产出 (title, source, body)。

    规则（都是从实数据里踩出来的）：
      · **第一个标题之前的内容整体丢弃** —— 那里是免责声明与元信息，不是知识
      · source 取该小节内第一条 <!-- source: ... -->；因为标题后通常紧跟空行，
        判断"是否已有正文"必须忽略空行，否则出处行会被当成正文吞掉
    """
    title = None
    src = ""
    buf = []
    for line in text.splitlines():
        m = HEAD_RE.match(line)
        if m:
            if title is not None:
                yield title, src, "\n".join(buf).strip()
            title = m.group(2)
            src = ""
            buf = []
            continue
        if title is None:
            continue                      # 丢弃标题之前的头部
        ms = SOURCE_RE.search(line)
        if ms and not any(b.strip() for b in buf):
            src = ms.group(1)
            continue
        buf.append(line)
    if title is not None:
        yield title, src, "\n".join(buf).strip()


def _segments(body):
    """把正文切成语义段：在**编号小标题**（1. / 1.3.1 / 3.2 / 三、）或空行处断开。

    为什么优先认编号而不是空行：tech 卡片里的"表格"在提取阶段被拉平成连续长行
    （实测：`操作\\n命令\\n说明\\n进入系统视图\\nsystem-view...`），
    只按空行切会把表格从中间截断，切出 `2000 ～ 2999`、`s source-wildcard`
    这种**单独无法理解**的片段。文档自己的编号（1.3.1 配置基本 ACL）才是真边界。
    """
    segs, cur = [], []
    for line in body.splitlines():
        if is_num_heading(line) and any(c.strip() for c in cur):
            segs.append("\n".join(cur).strip())
            cur = [line]
        elif not line.strip() and cur:
            segs.append("\n".join(cur).strip())
            cur = []
        else:
            cur.append(line)
    if cur:
        segs.append("\n".join(cur).strip())
    return [s for s in segs if s.strip()]


def _hard_split_by_lines(text, limit=HARD_MAX):
    """最后兜底：**按行**累积，保证每片 <= limit。

    为什么必须有这一层：表格被拉平后可能出现"整块没有任何空行"的超长段
    （实测 h3c 切片最大到过 455,901 字符），只按空行切根本切不动 ——
    这种片段送进 embedding 会被静默截断（bge-m3 上限 8192 token），
    索引里留下一个"看起来很大、实际只编了一半"的假片段。
    """
    out, cur = [], ""
    for line in text.splitlines():
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur.strip():
        out.append(cur)
    return out


def split_long(body, max_chars=MAX_CHARS, overlap=OVERLAP):
    """按语义段聚合到 <= max_chars；超大段按行兜底切（不横切表格中部）。"""
    segs = _segments(body)
    pieces = []
    for s in segs:
        if len(s) > HARD_MAX:
            pieces.extend(_hard_split_by_lines(s))
        else:
            pieces.append(s)

    out, cur = [], ""
    for s in pieces:
        if cur and len(cur) + len(s) + 1 > max_chars:
            out.append(cur)
            tail = cur[-overlap:] if overlap > 0 else ""
            cur = (tail + "\n" + s) if tail else s
        else:
            cur = (cur + "\n" + s) if cur else s
    if cur.strip():
        out.append(cur)
    return [p for p in out if p.strip()]


def chunk_file(path, vendor, cat, rel):
    chunks = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return chunks
    for title, src, body in iter_sections(text):
        if len(body) < MIN_CHARS:
            continue
        prefix = f"{vendor} · {cat} · {title}".strip(" ·")
        for i, piece in enumerate(split_long(body)):
            full = f"[{prefix}]\n{piece}"
            chunks.append({
                "vendor": vendor,
                "file": rel,
                "section": title,
                "part": i,
                "source": src,
                "text": full,
                "chars": len(full),
            })
    return chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", required=True)
    ap.add_argument("--chunk", action="store_true")
    ap.add_argument("--include-ai", action="store_true", help="把 tech/ai/ 也纳入")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件（试跑）")
    ap.add_argument("--samples", type=int, default=0, help="打印 N 个片段样本")
    args = ap.parse_args()

    print(f"知识库目录: {KNOWLEDGE}", flush=True)
    base = os.path.join(KNOWLEDGE, args.vendor, "tech")
    if not os.path.isdir(base):
        raise SystemExit(f"!! 目录不存在: {base}")

    files = []
    for name in sorted(os.listdir(base)):
        if name.lower().endswith(".md") and os.path.isfile(os.path.join(base, name)):
            files.append((os.path.join(base, name), "tech/" + name))
    if args.include_ai:
        aidir = os.path.join(base, "ai")
        if os.path.isdir(aidir):
            for name in sorted(os.listdir(aidir)):
                if name.lower().endswith(".md") and os.path.isfile(os.path.join(aidir, name)):
                    files.append((os.path.join(aidir, name), "tech/ai/" + name))
    if args.limit:
        files = files[:args.limit]

    print(f"厂商 {args.vendor}: 待切 {len(files)} 个 md 文件"
          f"{'（含 tech/ai）' if args.include_ai else ''}", flush=True)
    os.makedirs(OUTDIR, exist_ok=True)
    out_path = os.path.join(OUTDIR, f"{args.vendor}_chunks.jsonl")

    total = 0
    chars = 0
    biggest = 0
    samples = []
    with open(out_path, "w", encoding="utf-8") as out:
        for i, (path, rel) in enumerate(files, 1):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                cat = parse_header(fh.read(4096))
            for ch in chunk_file(path, args.vendor, cat, rel):
                ch["id"] = total
                out.write(json.dumps(ch, ensure_ascii=False) + "\n")
                total += 1
                chars += ch["chars"]
                biggest = max(biggest, ch["chars"])
                if len(samples) < args.samples:
                    samples.append(ch)
            if i % 200 == 0:
                print(f"   ...{i}/{len(files)} 文件，{total} 片段", flush=True)

    print(f"\n=== 切片完成 ===")
    print(f"文件 {len(files)}  片段 {total}  总字符 {chars:,}  平均 {chars // max(total,1)}  最大 {biggest}")
    print(f"产物 {out_path}")
    if os.path.exists(out_path):
        print(f"体积 {os.path.getsize(out_path) / 1024**2:.1f} MB")
    for ch in samples:
        print(f"\n--- 样本 id={ch['id']} {ch['file']} | 小节={ch['section']} | "
              f"出处={ch['source'][:60] or '(无)'} | {ch['chars']} 字 ---")
        print(ch["text"][:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
