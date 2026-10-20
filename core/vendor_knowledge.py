"""厂商知识库的**结构化数据访问层**（批次二读、批次三建）。

目录结构（批次三的脚本会按这个落数据）：

    knowledge/<vendor>/                 # vendor: h3c / huawei / ruijie
      commands.sqlite                   # ① 命令表（一条命令一行，见 build_vendor_knowledge.py）
      chapters/<章节>.md                # ② 技术讲解：每个章节一个 md，带 <a id="..."> 锚点
      examples/<...>.md                 # ③ 例子：单独文件
      index.json                        # ④ 索引：命令/章节 → 文件#锚点

**这一层只读、只做确定性查找**：没有任何 AI 调用，也不生成内容。
数据不在（本机还没建）就返回明确的"没有"，由界面显示空状态 —— 不编。

为什么单独一个模块：命令表的建/读、索引的建/读是同一件事的两半，
放在一起才不会出现"写的地方和读的地方对不上"（批次三会往这里加 build_*）。
"""

from __future__ import annotations

import json
import os
import re

from core import app_paths

#: 知识库根目录。允许用环境变量覆盖（自测指向临时目录）；默认跟着"基准目录"走：
#: 源码态 = 仓库根，打包态 = exe 同目录（见 core/app_paths.py 的两态判据）。
DEFAULT_DIR = os.path.join(app_paths.base_dir(), "knowledge")

#: 命令表文件名（批次三的建表脚本用同一个常量，避免两边写死不同的名字）
COMMANDS_DB = "commands.sqlite"

#: 没有技术讲解时的统一文案（界面直接用，保证"绝不静默"）
EMPTY_REASON = "该命令暂无技术解释（本机还没有从厂商文档提取的讲解文本）"


def knowledge_dir() -> str:
    return os.environ.get("AIT_KNOWLEDGE_DIR") or DEFAULT_DIR


def vendor_dir(vendor: str) -> str:
    return os.path.join(knowledge_dir(), str(vendor or "").strip().lower())


def load_index(vendor: str) -> dict:
    """读 knowledge/<vendor>/index.json；不存在就返回空结构（不抛异常）。"""
    path = os.path.join(vendor_dir(vendor), "index.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"vendor": vendor, "commands": {}, "chapters": {}}


_ANCHOR = re.compile(r'<a\s+id="([^"]+)"\s*>\s*</a>', re.IGNORECASE)
#: 下一个锚点或下一个二级标题 = 本段的结束位置
_NEXT_BREAK = re.compile(r'(<a\s+id="[^"]+"\s*>\s*</a>)|(^##\s)', re.IGNORECASE | re.MULTILINE)


def _slice_anchor(markdown: str, anchor: str) -> str:
    """从 md 里切出某个锚点那一段（到下一个锚点/二级标题为止）。

    为什么不留整篇：知识库面板要"跳到对应位置"，把整篇塞进去用户还得自己找。
    """
    if not anchor:
        return ""
    m = None
    for hit in _ANCHOR.finditer(markdown):
        if hit.group(1) == anchor:
            m = hit
            break
    if not m:
        return ""
    rest = markdown[m.end():]
    nxt = _NEXT_BREAK.search(rest)
    return (rest[:nxt.start()] if nxt else rest).strip()


def explanation(vendor: str, command: str) -> dict:
    """某条命令的**厂商技术解释**。

    返回：
      {"available": bool, "reason": str, "title": str, "file": str,
       "anchor": str, "markdown": str, "source": str}

    available=False 时 reason 一定有内容 —— 界面上要明确写"暂无"，不能静默。
    """
    out = {"available": False, "reason": EMPTY_REASON, "title": "", "file": "",
           "anchor": "", "markdown": "", "source": ""}
    cmd = str(command or "").strip().lower()
    if not vendor or not cmd:
        return out

    idx = load_index(vendor)
    # 厂商数据本身不完整（例如 ruijie 源文档缺 show 命令参考）→ 空状态里如实说明。
    # 必须在"找不到条目"**之前**判断：缺命令时更要说明是"数据不完整"，不能只说"暂无"。
    if idx.get("incomplete"):
        out["reason"] = "%s；%s" % (EMPTY_REASON, idx.get("incomplete_reason") or "")
    entry = ((idx.get("commands") or {}).get(cmd)) or {}
    if not isinstance(entry, dict):
        return out

    # 重名命令会有多个候选（命令 + feature/型号）：**取第一个"锚点真的存在"的候选**，
    # 不是"运气取第一个"。主字段不可用时逐个回退，全都不行才如实报错。
    if not entry:
        # 索引里根本没有这条命令 → 保持上面的文案（可能是"暂无讲解"，也可能是厂商标注的
        # "数据不完整"）；**不要**用"候选都没定位到"覆盖掉它。
        return out
    cands = [c for c in (entry.get("candidates") or []) if isinstance(c, dict)]
    if not cands:
        cands = [entry]
    markdown, anchor, file_rel = "", "", ""
    for cand in cands:
        rel = str(cand.get("chapter") or cand.get("file") or "")
        anc = str(cand.get("anchor") or "")
        if not rel:
            continue
        try:
            with open(os.path.join(vendor_dir(vendor), rel), "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        body = _slice_anchor(text, anc) if anc else text.strip()
        if body:
            markdown, anchor, file_rel = text, anc, rel
            entry = cand                     # 用真的能用上的那个候选
            break
    if not markdown:
        tried = "；".join("%s#%s" % (c.get("chapter"), c.get("anchor")) for c in cands)
        out["reason"] = ("索引里有这条命令的讲解，但候选都没能定位到锚点：%s" % tried)
        return out
    body = _slice_anchor(markdown, anchor) if anchor else markdown.strip()

    out.update({
        "available": True,
        "reason": "",
        "title": str(entry.get("title") or os.path.basename(file_rel)),
        "file": file_rel,
        "anchor": anchor,
        "markdown": body,
        # 来源必须是**厂商文档**，用来给"这段话哪来的"一个可追溯的落点
        "source": str(entry.get("source") or ""),
    })
    return out


def lookup_sqlite(vendor: str, command: str) -> dict:
    """**先在 SQLite 里查这条命令**（批次三的建库产物），查不到再让调用方回退 jsonl。

    返回 {"found": bool, "layer": "sqlite", "rows": [...], "examples": [...]}。
    只读、只做确定性查询；表不在或没这条命令就 found=False（不抛异常）。
    """
    out = {"found": False, "layer": "sqlite", "rows": [], "examples": []}
    cmd = str(command or "").strip().lower()
    if not vendor or not cmd:
        return out
    db = os.path.join(vendor_dir(vendor), COMMANDS_DB)
    if not os.path.exists(db):
        return out
    try:
        import sqlite3                                   # noqa: WPS433
        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                "SELECT name, syntax, view, feature, model, source FROM commands"
                " WHERE name_lower=? LIMIT 20", (cmd,)).fetchall()
            if not rows:
                # 再试一次前缀匹配（用户可能只敲了前半截）
                rows = con.execute(
                    "SELECT name, syntax, view, feature, model, source FROM commands"
                    " WHERE name_lower LIKE ? ORDER BY LENGTH(name_lower) LIMIT 20",
                    (cmd + "%",)).fetchall()
            ex = con.execute(
                "SELECT title, source, chapter, anchor FROM examples"
                " WHERE vendor=? AND (LOWER(title) LIKE ? OR LOWER(preview) LIKE ?)"
                " LIMIT 5",
                (vendor, "%" + cmd + "%", "%" + cmd + "%")).fetchall()
        finally:
            con.close()
    except Exception:                                    # noqa: BLE001
        return out
    if not rows:
        return out
    out["found"] = True
    out["rows"] = [
        {"command": r[0], "syntax": r[1], "view": r[2], "feature": r[3],
         "model": r[4], "source": r[5]} for r in rows]
    # 正文现取现读（SQLite 里只有 chapter#anchor 指针）—— 体积换一次磁盘读，值得
    ex_out = []
    for title, source, chapter, anchor in ex:
        text = ""
        if chapter and anchor:
            try:
                with open(os.path.join(vendor_dir(vendor), chapter),
                          "r", encoding="utf-8") as fh:
                    text = _slice_anchor(fh.read(), anchor)
            except OSError:
                text = ""
        ex_out.append({"title": title, "text": text, "source": source,
                       "chapter": chapter, "anchor": anchor})
    out["examples"] = ex_out
    out["commands"] = [r[0] for r in rows]
    return out


def examples_coverage(vendor: str) -> dict:
    """建库时统计的"示例收录范围"（index.json 的 examples_coverage）。

    界面用它如实标注"收录 x/y" —— 截断必须可见，不许静默。
    """
    idx = load_index(vendor)
    cov = idx.get("examples_coverage") or {}
    return cov if isinstance(cov, dict) else {}


def stats(vendor: str) -> dict:
    """这条厂商知识库有什么（给界面做空状态判断）。"""
    idx = load_index(vendor)
    return {
        "vendor": vendor,
        "dir": vendor_dir(vendor),
        "has_index": bool(idx.get("commands")),
        "n_commands": len(idx.get("commands") or {}),
        "n_chapters": len(idx.get("chapters") or {}),
        "generated_at": idx.get("generated_at", ""),
    }
