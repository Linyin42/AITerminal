"""技术资料层的**运行时检索**：纯代码、零 AI、只读。

这一层解决什么
==============
`core/vendor_knowledge.py` 管的是"**厂商文档原文**的定位"（按 command → chapter#anchor
切片）。本模块管的是"**技术讲解**的检索"—— 数据来自
`knowledge/<vendor>/tech/`（由 `tools/build_tech_docs.py` 用代码从厂商原始文档提取）。

两者的分工必须分清，否则会出现"写的地方和读的地方对不上"：

    vendor_knowledge   命令字段表 + 章节锚点切片（chapters/*.md）
    tech_knowledge     按 feature 归集的技术讲解正文（tech/*.md + tech/index.json）

设计原则（与用户 2026-10-04 的要求一致）
======================================
1. **代码优先**：能检索到就**绝不调 AI**。返回里带 `source` 标明这次是哪条路命中的。
2. **命中不了要明说**：`available=False` + `need_ai=True` + `reason`，
   由调用方决定是否上 AI —— 本模块**自己永远不调模型**。
3. **AI 的产出另存**：AI 回答回写进 `tech/ai/`（与厂商原文**物理隔离**），
   下次就能被代码检索到。**绝不把 AI 生成的内容混进厂商原文文件**——
   否则以后没人分得清哪句是厂商说的、哪句是模型编的。

与 `KnowledgeBase`（core/knowledge.py）的区别：那个是"用户导入的资料库"（可写、
走向量/TF 检索）；本模块是"随产品发布的厂商技术资料"（只读、按 feature 索引）。
"""

from __future__ import annotations

import json
import os
import re
import time

from core import app_paths

#: 知识库根目录（与 vendor_knowledge 保持同一个约定与环境变量）。
#: 默认跟着"基准目录"走：源码态 = 仓库根，打包态 = exe 同目录（见 core/app_paths.py）。
DEFAULT_DIR = os.path.join(app_paths.base_dir(), "knowledge")

#: 单次返回给界面的正文字节上限。**存**可以很大（见 build_tech_docs.MAX_FEATURE_BYTES），
#: 但**返回**必须有界 —— 界面右栏塞 600 KB 没法看，也会拖慢渲染。
MAX_RETURN_BYTES = 8 * 1024

#: AI 回写目录名（与厂商原文隔离）
AI_SUBDIR = "ai"

_index_cache: dict[str, tuple[float, dict]] = {}


def knowledge_dir() -> str:
    return os.environ.get("AIT_KNOWLEDGE_DIR") or DEFAULT_DIR


def tech_dir(vendor: str) -> str:
    return os.path.join(knowledge_dir(), str(vendor or "").strip().lower(), "tech")


# ---------------------------------------------------------------- 索引读取
def _index(vendor: str) -> dict:
    """读并缓存 `tech/index.json`。用 mtime 做失效判断（重建数据后无需重启）。"""
    v = str(vendor or "").strip().lower()
    if not v:
        return {}
    path = os.path.join(tech_dir(v), "index.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _index_cache.get(v)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    _index_cache[v] = (mtime, data)
    return data


def clear_cache() -> None:
    _index_cache.clear()


def available(vendor: str) -> bool:
    """该厂商有没有技术资料层（界面据此决定要不要显示"技术"页签）。"""
    idx = _index(vendor)
    return bool(idx.get("features"))


def stats(vendor: str) -> dict:
    idx = _index(vendor)
    return {
        "vendor": vendor,
        "dir": tech_dir(vendor),
        "available": bool(idx.get("features")),
        "n_features": idx.get("n_features", 0),
        "n_commands": idx.get("n_commands", 0),
        "n_terms": len(idx.get("terms") or {}),
        "generated_at": idx.get("generated_at", ""),
        "doc_root": idx.get("doc_root", ""),
        "stats": idx.get("stats", {}),
    }


def list_features(vendor: str) -> list[str]:
    return sorted((_index(vendor).get("features") or {}).keys())


# ---------------------------------------------------------------- 正文读取
def _read_feature(vendor: str, feature: str, max_bytes: int = MAX_RETURN_BYTES) -> dict:
    """读某个 feature 的 md，做**有界**截断。截断必须可见（项目约定）。"""
    idx = _index(vendor)
    node = (idx.get("features") or {}).get(feature) or {}
    rel = node.get("file") or ""
    out = {"markdown": "", "file": rel, "truncated": False, "bytes": 0}
    if not rel:
        return out
    path = os.path.join(knowledge_dir(), str(vendor).lower(), rel)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return out
    out["bytes"] = len(text.encode("utf-8"))
    # 去掉我们自己写的文件头（对用户没价值，且会占掉返回配额）
    text = re.sub(r"\A#[^\n]*\n(?:>[^\n]*\n|\s*\n|- [^\n]*\n)*\s*---\s*\n", "", text, count=1)
    text = text.strip()
    if len(text.encode("utf-8")) > max_bytes:
        keep = max_bytes // 3
        out["markdown"] = text[:keep] + "\n\n> ⚠ 内容较长已截断，完整讲解见 `%s`。" % rel
        out["truncated"] = True
    else:
        out["markdown"] = text
    return out


# ---------------------------------------------------------------- 术语工具
_TERM_SPLIT = re.compile(r"[\s/、,，;；()（）\[\]【】:：]+")
_STOP = {"配置", "命令", "使用", "功能", "简介", "概述", "显示", "维护", "管理", "和", "与", "的"}

#: **CLI 结构词**黑名单：这些词是命令行的语法骨架，不是技术名词。
#: 为什么必须排除（实测）：h3c 根本没有 `configure terminal` 这条命令（H3C Comware 用的是
#: `system-view`），但 `terminal` 恰好出现在某条命令名里，于是术语倒排把它投给了
#: `基础配置/登录设备` —— 一个**看着像答案的错误答案**。排除后它会老老实实落到 AI 兜底，
#: 再配合下面的跨厂商写法映射，还能直接给出 h3c 的正确写法。
_LATIN_STOP = {
    "configure", "terminal", "config", "enable", "disable", "interface", "system",
    "view", "display", "show", "set", "undo", "no", "mode", "exit", "quit", "end",
    "line", "user", "all", "brief", "verbose", "detail", "list", "info", "status",
}


def terms_of(*texts: str) -> set[str]:
    """与 build_tech_docs.terms_of **同一套规则**（改一处必须改两处，否则索引对不上）。

    粗分词（中文 2-4 字滑窗 + 英文整词）：本层要的是**召回**而不是语言学正确，
    且不能给引擎加第三方依赖（引擎跑在用户自己的 Python 上，装包会失败）。
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


def _terms_weighted(query: str) -> list[tuple[str, float]]:
    """把 query 切成 (术语, 权重)。权重 = **词长 × 位置加成**。

    为什么要位置加成（踩过坑）：`端口聚合` 会同时切出 `端口` 与 `聚合`。
    `端口` 命中 `二层技术-以太网交换/端口隔离`，`聚合` 命中 `.../以太网链路聚合`
    —— 按"命中个数"打分两者打平，再按 feature 名短者优先就选错成了 `端口隔离`。
    中文复合词的**中心语在后**（"端口聚合"说的是"聚合"），所以越靠后的术语越该重。
    位置加成后 `聚合`(2×(1+0.5)=3.0) 压过 `端口`(2×(1+0)=2.0)，落到正确的 `以太网链路聚合`。

    为什么还乘词长：长词天然比短词更具体（`链路聚合` 比 `聚合` 更能定性）。
    """
    q = str(query or "").strip()
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    n = len(q) or 1
    for chunk in _TERM_SPLIT.split(q):
        chunk = chunk.strip()
        if not chunk:
            continue
        base = q.find(chunk)
        base = base if base >= 0 else 0
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9._\-]*", chunk):
            term = chunk.lower()
            if term in _LATIN_STOP:
                continue                      # CLI 结构词不参与技术匹配
            if term not in seen:
                seen.add(term)
                out.append((term, 2.0 * (1.0 + base / n)))
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            for size in (4, 3, 2):
                for i in range(0, max(0, len(chunk) - size + 1)):
                    g = chunk[i:i + size]
                    if g in _STOP or g in seen:
                        continue
                    seen.add(g)
                    pos = (base + i) / n
                    out.append((g, size * (1.0 + 0.5 * pos)))
    return out


# ---------------------------------------------------------------- 匹配
#: 同义词表：**口语/习惯说法 → 厂商文档里的说法**。
#: 为什么需要：厂商文档用的是规范书面语，用户嘴里是习惯说法。实测 `防环` 在 h3c 语料里
#: 出现 **0 次**，而 `环路` 出现 352 次 —— 索引与正文都命中不了，只能落到 AI 兜底。
#: 这不是"猜"，是**可枚举的行业习惯差异**，用纯代码补最划算。
#: 只放"确定成立"的对应；拿不准的不放（宁可落到 AI，也不要给错的技术解释）。
_SYNONYMS = {
    "防环": "环路",
    "破环": "环路",
    "环回": "环路",
    "端口": "接口",
    "网口": "接口",
    "网卡": "接口",
    "主备": "主备倒换",
    "双机": "主备倒换",
    "双机热备": "主备倒换",
    "双活": "主备",
    "聚合": "链路聚合",
    "捆绑": "链路聚合",
    "端口聚合": "链路聚合",
    "链路捆绑": "链路聚合",
    "堆叠": "IRF",
    "打标签": "VLAN",
    "上联": "上行链路",
    "下联": "下行链路",
    "放行": "ACL",
    "限速": "QoS",
    "分流": "策略路由",
    "端口镜像": "镜像",
    "流镜像": "镜像",
    "生成树协议": "生成树",
    "网关冗余": "VRRP",
    "虚拟路由冗余": "VRRP",
    "单臂路由": "子接口",
    "邻居": "邻居",
    "起不来": "接口",
    "不通": "接口",
    "丢包": "接口",
}


def expand_terms(query: str) -> list[str]:
    """把用户说法展开成"原词 + 文档说法"。返回去重后的候选词（原词排第一）。"""
    q = str(query or "").strip()
    out = [q] if q else []
    for k, v in _SYNONYMS.items():
        if k and k in q:
            cand = q.replace(k, v) if q != k else v
            if cand and cand not in out:
                out.append(cand)
    return out


def vendor_aliases(vendor: str, query: str) -> list[tuple[str, str]]:
    """跨厂商**写法**差异 → [(等价写法, 说明)]。

    为什么值得单独做：同一件事三家叫法不同，而用户经常记混或直接照抄别家的命令。
    实测最常见的三类差异（几乎占全部记混场景）：
        `show X`   ↔ `display X`        查看类命令，H3C 用 display，华为/锐捷用 show
        `configure terminal` ↔ `system-view`   进入配置模式，H3C 用 system-view
        `no X`     ↔ `undo X`           取反，H3C 用 undo，华为/锐捷用 no

    只放这三类"确定成立且极常见"的。**拿不准的一律不放** —— 给错的对应比说
    "找不到"更糟：用户会照着错命令敲。
    """
    v = str(vendor or "").strip().lower()
    q = str(query or "").strip()
    low = q.lower()
    out: list[tuple[str, str]] = []
    if not q:
        return out

    if v == "h3c":
        if low.startswith("show "):
            out.append(("display " + q[5:], "`show X` 是华为/锐捷写法，H3C 对应 `display X`"))
        if low in ("configure terminal", "conf t", "configure"):
            out.append(("system-view", "`configure terminal` 是华为/锐捷写法，H3C 对应 `system-view`"))
        if low.startswith("no "):
            out.append(("undo " + q[3:], "`no X` 是华为/锐捷写法，H3C 对应 `undo X`"))
    elif v in ("huawei", "ruijie"):
        if low.startswith("display "):
            out.append(("show " + q[8:], "`display X` 是 H3C 写法，本厂商对应 `show X`"))
        if low in ("system-view", "sys"):
            out.append(("configure terminal", "`system-view` 是 H3C 写法，本厂商对应 `configure terminal`"))
        if low.startswith("undo "):
            out.append(("no " + q[5:], "`undo X` 是 H3C 写法，本厂商对应 `no X`"))
    return out


def _leaf(feature: str) -> str:
    """feature 的叶子名：`三层技术-IP路由/OSPF` → `OSPF`。"""
    return re.split(r"[/\\]", str(feature or ""))[-1].strip().lower()


def _match_feature_name(idx: dict, term: str) -> tuple[str, str]:
    """只做"**技术名**"两级匹配：feature 叶子名精确 → feature 全名精确。

    单独抽出来，是因为 `explain()` 需要把它排在"通用命令"判定**之前**：
    `ospf` / `stp` / `irf` 既是技术名、又可能恰好是被标成通用的命令名，
    这时用户要的是**那个技术**的讲解，不能因为它是通用命令就只回一张命令卡片。
    """
    q = (term or "").strip().lower()
    if not q:
        return "", ""
    features = idx.get("features") or {}
    for feat in features:
        if _leaf(feat) == q:
            return feat, "feature-leaf"
    for feat in features:
        if feat.lower() == q:
            return feat, "feature"
    return "", ""


def generic_command(idx: dict, term: str) -> dict:
    """term 是不是"全局通用命令"（`configure terminal`/`interface`/`enable`）？是就返回它的索引节点。

    为什么必须单独处理：这类命令在几乎每个 feature 里都出现，**指向任何一个 feature 都是错的**
    （实测 ruijie 的 `configure terminal` 被挂到了 `1588v2-PTP` 名下）。
    对它们正确的回答是"这是通用命令，语法/视图是这些，它出现在下面 N 个特性里"，
    而不是硬塞一篇不相关的技术讲解。构建期打的 `generic` 标记就是为这件事。
    """
    q = str(term or "").strip().lower()
    if not q:
        return {}
    node = (idx.get("commands") or {}).get(q) or {}
    return node if node.get("generic") else {}


def _match_feature(idx: dict, term: str, weak: bool = True) -> tuple[str, str]:
    """term → (feature, how)。how 是命中方式，写进返回里便于验收。

    优先级（**顺序很关键，踩过坑**）：
      ① feature 叶子名精确  `OSPF` → `三层技术-IP路由/OSPF`
      ② feature 全名精确
      ③ 命令精确（**跳过通用命令**）
      ④ feature 名包含
      ⑤ 命令前缀（取最长，**跳过通用命令**）
      ⑥ 术语倒排（按词长 × 位置加权）—— **弱信号**，`weak=False` 时不做

    为什么 ① 要排在 ③ 前面：`ospf` / `stp` / `irf` 这类词**既是技术名又是命令名**。
    实测把它们当命令精确匹配会落到错误的 feature（`ospf` → MPLS L3VPN、
    `stp` → WLAN，因为那里恰好也有一条同名命令）。用户敲技术名时，
    他要的是**那个技术**的讲解，不是"碰巧也叫这名字的某条命令"。

    为什么 ⑥ 是弱信号：术语倒排的索引里**只有 feature 名和命令名**，没有正文。
    `show version` 会被拆出 `version`，投给"恰好有条命令带 version"的 `安全/SSL`
    —— 一个看着像答案的错误答案。所以它必须排在"跨厂商别名"之后（见 `_resolve`）。
    """
    q = (term or "").strip().lower()
    if not q:
        return "", ""
    commands = idx.get("commands") or {}
    features = idx.get("features") or {}

    feat, how = _match_feature_name(idx, q)
    if feat:
        return feat, how

    hit = commands.get(q)
    if hit and not hit.get("generic"):
        return str(hit.get("feature") or ""), "command"

    for feat in features:
        if q in feat.lower():
            return feat, "feature-contains"

    # 命令前缀：按 **token（词）重叠** 匹配，这才是 CLI 补全的真实语义。
    #   ① 命令名是查询的前缀     `stp` → `stp global enable`
    #   ② 查询是命令名的前缀     `undo vlan 10` → `undo vlan`
    #   ③ 前 N 个词完全一致      `display ip route` → `display ip routing-table`（前 2 词同）
    # 为什么必须支持 ③：实测 `display ip route`（用户记成锐捷的 `show ip route` 再换算过来）
    # 跟 h3c 真实的 `display ip routing-table` **整串对不上**，于是掉到术语倒排，
    # 被投给了 `三层技术-IP路由/IPv6 IS-IS`。按词重叠就能对上。
    # 门槛：只要有一边是单词命令，1 个词就够；否则至少要**前 2 个词**一致
    # （只同 1 个词如 `display` 太泛，几乎每条 display 命令都满足）。
    q_tokens = q.split()
    best, best_len, best_ov = "", 0, 0
    for name, node in commands.items():
        if node.get("generic"):
            continue
        n_tokens = name.split()
        ov = 0
        for a, b in zip(n_tokens, q_tokens):
            if a == b:
                ov += 1
            else:
                break
        if not ov:
            continue
        if len(q_tokens) > 1 and len(n_tokens) > 1 and ov < 2:
            continue
        if ov > best_ov or (ov == best_ov and len(name) > best_len):
            best, best_len, best_ov = str(node.get("feature") or ""), len(name), ov
    if best:
        return best, "command-prefix"

    if not weak:
        return "", ""

    # 多词英文查询**不走**术语倒排：`display ip route` 会被拆成 `ip`/`route` 去投票，
    # 这两个词在任何网络文档里都极其常见，投出来的 feature 纯属噪声。
    # 术语倒排只对"中文词"和"单个英文词（`ospf`/`vxlan`）"有意义。
    if " " in q and not re.search(r"[\u4e00-\u9fff]", q):
        return "", ""

    terms = idx.get("terms") or {}
    score: dict[str, float] = {}
    for t, w in _terms_weighted(term):
        for feat in terms.get(t, ()):
            score[feat] = score.get(feat, 0.0) + w
    if score:
        ranked = sorted(score.items(), key=lambda kv: (-kv[1], len(kv[0])))
        # 阈值 2.0 = "至少一个两字词命中"，挡住只靠单字噪声的巧合命中。
        # **平局必须放弃**：术语倒排只登记了名字，`vlan` 会同时命中 22 个 feature，
        # 分数完全一样。早先按"feature 名最短"破平局，结果 `undo vlan 10` 被投给了
        # `FCoE`（4 个字，最短）—— 一个纯噪声的答案。平局说明"这个词不足以定位"，
        # 此时返回空、让调用方走正文检索或上 AI，才是诚实的。
        if ranked[0][1] >= 2.0 and (len(ranked) == 1 or ranked[1][1] < ranked[0][1]):
            return ranked[0][0], "term"
    return "", ""


def _command_card(node: dict) -> str:
    """把命令索引节点渲染成一小段 markdown。

    ⚠ 这**不是技术讲解**，只是命令本身的字段（格式/视图/型号）。
    只在"查询词是一条通用命令、没有对应技术分类"时才用它 ——
    此时给命令卡片是**准确**的，硬塞一篇不相关的技术讲解才是错的。
    """
    lines = []
    cmd = str(node.get("command") or "")
    if cmd:
        lines.append("### `%s`\n" % cmd)
    if node.get("syntax"):
        lines.append("- 命令格式：`%s`" % node["syntax"])
    if node.get("view"):
        lines.append("- 执行视图：%s" % node["view"])
    if node.get("model"):
        lines.append("- 适用型号：%s" % node["model"])
    if node.get("generic"):
        feats = node.get("features") or []
        lines.append("- ⚠ 这是**通用命令**，在 %d 个功能分类里都出现，"
                     "因此不指向某一个技术分类。" % node.get("n_features", len(feats)))
        if feats:
            lines.append("- 出现过的分类（前 %d 个）：%s"
                         % (len(feats), "、".join("`%s`" % f for f in feats)))
    return "\n".join(lines)


def _cross_card(term: str, hits: list[dict], limit: int = 5) -> str:
    """把"跨分类概念"的命中渲染成索引卡片。

    为什么要这张卡片：`主备倒换` 这类词在 BGP(15 处)/IS-IS(14)/OSPF(13)/文件系统管理(12)
    …… 里**均匀分布**，说明它是**横跨多分类的概念**，不归属任何一个。
    旧逻辑会取命中数最高的那个（→ OSPF），给出的是一篇"恰好提得多一点"的文档，
    属于**错答**。这里改成如实说"它跨分类"并列出分布，比假装精确要诚实得多。
    """
    lines = ["### %s（跨分类概念）\n" % term,
             "这个说法在多个功能分类里都出现，**没有单一归属**；"
             "下面按出现次数列出，点开对应分类看原文：\n"]
    for h in hits[:limit]:
        lines.append("- **%s** —— 出现 %d 处" % (h["feature"], h["count"]))
        ex = " ".join(str(h.get("excerpt") or "").split())
        if ex:
            lines.append("  > %s" % ex[:160])
    return "\n".join(lines)


def search_content(vendor: str, query: str, top_k: int = 5,
                   excerpt_chars: int = 600) -> list[dict]:
    """**正文级**检索兜底：直接在 tech/*.md 里找关键词，返回命中的 feature + 上下文片段。

    为什么要有这一层：索引只登记了 feature 名与命令名，像 `防环`、`主备倒换`
    这类**只在正文里出现**的词，索引检索必然漏。而"能搜到"是用户明确的要求，
    所以补一层内容扫描。纯代码、无依赖；全量约 20 MB，实测百毫秒级。

    命中按"出现次数"排序；片段取关键词前后各一段，便于界面直接展示。
    """
    v = str(vendor or "").strip().lower()
    q = str(query or "").strip()
    if not v or not q or len(q) < 2:
        return []
    idx = _index(v)
    if not idx:
        return []
    base = os.path.join(knowledge_dir(), v)
    needle = q.lower()
    hits = []
    for feat, node in (idx.get("features") or {}).items():
        rel = str(node.get("file") or "")
        if not rel:
            continue
        try:
            with open(os.path.join(base, rel), "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        low = text.lower()
        n = low.count(needle)
        if not n:
            continue
        pos = low.find(needle)
        start = max(0, pos - excerpt_chars // 2)
        excerpt = text[start:pos + excerpt_chars // 2].strip()
        hits.append({"feature": feat, "file": rel, "count": n,
                     "excerpt": excerpt, "how": "content"})
    hits.sort(key=lambda h: (-h["count"], len(h["feature"])))
    return hits[:top_k]


def search(vendor: str, query: str, top_k: int = 8) -> list[dict]:
    """关键词检索：返回候选 feature（带得分与命中方式）。供补全/搜索框使用。

    纯代码打分（命中术语数 + 名称包含加权），**不是**向量检索 —— 够快够稳，
    且结果可解释（每个候选都说明为什么中）。
    """
    idx = _index(vendor)
    if not idx or not query:
        return []
    q = str(query).strip().lower()
    features = idx.get("features") or {}
    terms = idx.get("terms") or {}
    commands = idx.get("commands") or {}

    score: dict[str, float] = {}
    why: dict[str, list[str]] = {}

    def bump(feat: str, w: float, tag: str) -> None:
        if not feat:
            return
        score[feat] = score.get(feat, 0.0) + w
        why.setdefault(feat, [])
        if tag not in why[feat]:
            why[feat].append(tag)

    # 原词与同义词都参与投票（`防环` → `环路`、`堆叠` → `IRF`）。
    # 为什么这里也必须展开：本函数供**补全**用（`commands.suggest` 的 `tech` 字段），
    # 而补全场景用户敲的正是口语词。早先只在 explain() 里展开了同义词，
    # 结果 `explain('防环')` 命中而 `search('防环')` 返回空 —— 补全列表里啥也没有。
    # 同义词得票打 8 折，避免别名盖过原词。
    for variant in expand_terms(query):
        w_scale = 1.0 if variant == q else 0.8
        # ⚠ 比较前必须统一小写。`expand_terms()` 原样返回用户输入（可能 `VLAN`），
        #   而 `features` 的 key 是**混合大小写**（`二层技术-以太网交换/VLAN`）、
        #   `commands` 的 key 是**小写**。旧代码直接 `variant in feat.lower()`，
        #   于是 `search('VLAN')` 恒返回 0（`'VLAN' in '…/vlan'` 为假），
        #   而 `search('vlan')` 正常 —— 用户敲大写就"搜不到"，极难察觉。
        vlow = variant.lower()
        for t, w in _terms_weighted(variant):
            for feat in terms.get(t, ()):
                bump(feat, min(w, 4.0) * w_scale, "term:" + t)
        for feat in features:
            if vlow and vlow in feat.lower():
                bump(feat, 3.0 * w_scale, "feature")
        for name, node in commands.items():
            if node.get("generic"):
                continue                  # 通用命令不指向任何 feature，别给它投票
            if name == vlow:
                bump(str(node.get("feature") or ""), 6.0 * w_scale, "command")
            elif name.startswith(vlow) and len(vlow) >= 3:
                bump(str(node.get("feature") or ""), 2.0 * w_scale, "command-prefix")

    out = []
    for feat, sc in sorted(score.items(), key=lambda kv: (-kv[1], len(kv[0])))[:top_k]:
        node = features.get(feat) or {}
        out.append({"feature": feat, "score": round(sc, 2),
                    "why": why.get(feat, [])[:4],
                    "file": node.get("file", ""),
                    "n_commands": len(node.get("commands") or [])})
    return out


def _resolve(v: str, idx: dict, term: str, max_bytes: int, weak: bool = True) -> dict:
    """把**单个**查询词走完"技术名 → 通用命令 → 索引 → 正文"四步。

    命中返回字段片段（由 `explain()` 合并），没命中返回 `{}`。

    抽出来是因为 `explain()` 要拿**同一个词**反复试（原词 / 同义词 / 跨厂商等价写法），
    早先每试一次就把这四步抄一遍，改一处漏两处。现在只有这一份实现。

    `weak=False` 时**不做正文级检索**，只认索引级命中（技术名/命令/feature 名包含）。
    为什么要分强弱（踩过坑）：正文级检索很宽松，`防环` 这种词一扫正文就能命中某个
    "恰好提过几次"的分类，于是**抢在**同义词（`环路`）和跨厂商别名（`display X`）之前
    返回了一个错答案。正确顺序是：**先把所有索引级可能试完**（含同义词、别名），
    都不中才退到正文级。
    """
    feat, how = _match_feature_name(idx, term)

    # 通用命令（`configure terminal`/`interface`）：**不硬指** feature，回命令卡片 + 候选分类
    if not feat:
        g = generic_command(idx, term)
        if g:
            cands = []
            for f in (g.get("features") or []):
                node = (idx.get("features") or {}).get(f) or {}
                cands.append({"feature": f, "score": 0.0, "why": ["通用命令的出现处"],
                              "file": node.get("file", ""),
                              "n_commands": len(node.get("commands") or [])})
            return {
                "available": True, "source": "tech", "how": "command-generic",
                "command": str(g.get("command") or term),
                "syntax": str(g.get("syntax") or ""),
                "view": str(g.get("view") or ""),
                "markdown": _command_card(g),
                "candidates": cands,
            }

    if not feat:
        feat, how = _match_feature(idx, term, weak=weak)

    if feat:
        doc = _read_feature(v, feat, max_bytes=max_bytes)
        cmd_node = (idx.get("commands") or {}).get(str(term).strip().lower()) or {}
        # 挂上该 feature 的 AI 整理卡片（若已生成）。**两个都给**，由界面决定先展示哪个：
        #   · 用户问的是**技术**（`环路检测` 是什么）⇒ 他要的是"解释"，AI 卡片更合适，
        #     所以 prefer="ai"，原文作为可展开的依据；
        #   · 用户问的是**命令**（`acl number` 怎么用）⇒ 他要的是原文里的参数与缺省值，
        #     AI 卡片是二手归纳，反而可能丢细节，所以 prefer="vendor"。
        ai = ai_for_feature(v, feat)
        res = {
            "available": bool(doc["markdown"]),
            "source": "tech", "how": how, "feature": feat, "file": doc["file"],
            "markdown": doc["markdown"], "truncated": doc["truncated"],
            # 命令字段（命中方式本身是命令时才有意义，但填了不亏）
            "command": str(cmd_node.get("command") or ""),
            "syntax": str(cmd_node.get("syntax") or ""),
            "view": str(cmd_node.get("view") or ""),
            # AI 整理卡片（可能为空 —— 表示这个 feature 还没做结构化整理）
            "ai_markdown": str(ai.get("markdown") or ""),
            "ai_file": str(ai.get("file") or ""),
            "prefer": ("ai" if (ai.get("available") and not how.startswith("command"))
                       else "vendor"),
        }
        if not res["available"]:
            res["reason"] = "命中了 feature %r，但它的正文文件读不到（%s）" % (feat, doc["file"])
        return res

    if not weak:
        return {}

    # 正文级检索（**仍然是厂商原文**，只是从"按索引找"退到"扫正文找"）。
    # 索引只登记 feature 名与命令名，`防环`/`主备倒换` 这类只在正文出现的词必然漏。
    content = search_content(v, term, top_k=5)
    if not content:
        return {}
    best = content[0]
    cands = [{"feature": c["feature"], "score": float(c["count"]),
              "why": ["正文命中 %d 处" % c["count"]],
              "file": c["file"], "n_commands": 0} for c in content]
    # 命中是否**显著集中**在一个分类上？不集中 ⇒ 这是个跨分类概念，
    # 不能拿"提得最多的那篇"冒充它的技术解释（见 _cross_card 的说明）。
    if len(content) == 1 or best["count"] >= 2 * max(1, content[1]["count"]):
        doc = _read_feature(v, best["feature"], max_bytes=max_bytes)
        ai = ai_for_feature(v, best["feature"])
        return {"available": bool(doc["markdown"]), "source": "tech", "how": "content",
                "feature": best["feature"], "file": doc["file"],
                "markdown": doc["markdown"], "truncated": doc["truncated"],
                "ai_markdown": str(ai.get("markdown") or ""),
                "ai_file": str(ai.get("file") or ""),
                "prefer": "ai" if ai.get("available") else "vendor",
                "candidates": cands}
    return {"available": True, "source": "tech", "how": "content-cross",
            "feature": "", "file": "", "markdown": _cross_card(term, content),
            "candidates": cands}


def explain(vendor: str, term: str, context: str = "",
            max_bytes: int = MAX_RETURN_BYTES) -> dict:
    """**代码检索技术解释**（本模块的主入口）。

    返回：
      {"available": bool,          # 代码检索到了吗
       "need_ai": bool,            # 没检索到 ⇒ 调用方可以上 AI（本模块自己不上）
       "source": "tech"|"ai"|"none",
       "how": str,                 # 命中方式，便于验收与排查：
                                   #   feature-leaf / feature / command / command-generic
                                   #   feature-contains / command-prefix / term
                                   #   content / content-cross
                                   #   以上均可带后缀 -synonym / -vendor-alias
       "note": str,                # 跨厂商写法映射的说明（如"你敲的是华为写法"）
       "reason": str,              # available=False 时一定有内容（不静默）
       "vendor","term","feature","file","command","syntax","view",
       "markdown": str,            # **厂商原文**正文（有界）。语义与旧版一致，未改。
       "ai_markdown": str,         # AI 整理卡片（`tools/gen_tech_summary.py` 产出，可能为空）
       "ai_file": str,             # AI 卡片路径（空 = 还没整理过这个 feature）
       "prefer": "ai"|"vendor",    # 建议界面先展示哪个：
                                   #   问技术（`环路检测` 是什么）→ "ai"，原文作依据可展开
                                   #   问命令（`acl number` 怎么配）→ "vendor"，要原文的参数与缺省值
       "truncated": bool,
       "candidates": [...]}        # 没精确命中时给的备选，便于界面兜底展示

    零 AI、只读、**不抛异常**（跑在交互路径上，异常会让界面白屏）。
    """
    out = {
        "available": False, "need_ai": False, "source": "none", "how": "",
        "reason": "", "note": "", "vendor": str(vendor or ""), "term": str(term or ""),
        "feature": "", "file": "", "command": "", "syntax": "", "view": "",
        "markdown": "", "truncated": False, "candidates": [],
        # AI 整理卡片（与 markdown 并列返回，见 _resolve 里 prefer 的说明）
        "ai_markdown": "", "ai_file": "", "prefer": "vendor",
    }
    v = str(vendor or "").strip().lower()
    q = str(term or "").strip()
    if not v or not q:
        out["reason"] = "缺少厂商或查询词"
        return out

    idx = _index(v)
    if not idx.get("features"):
        out["need_ai"] = True
        out["reason"] = ("该厂商还没有技术资料层（knowledge/%s/tech/ 为空）。"
                         "先跑 tools/build_tech_docs.py 用代码从厂商文档提取。" % v)
        return out

    # ① 厂商原文（代码提取）—— **优先**，AI 内容不得盖过厂商原文。
    #    同一个查询会按"原词 → 同义词 → 跨厂商等价写法"依次试，且**先强后弱**：
    #    第一轮只认索引级命中，第二轮才允许正文级检索。顺序不能反（见 _resolve 的说明）。
    aliases = vendor_aliases(v, q)
    syns = expand_terms(q)[1:]
    alias_note = dict(aliases)

    strong = [(q, "")] + [(x, "-synonym") for x in syns] \
             + [(e, "-vendor-alias") for e, _ in aliases]
    # 弱命中轮：**别名排在最前**。有别名说明用户用的是别家的词汇，
    # 原词做正文扫描只会得到噪声（`no vlan 10` 在 h3c 上被扫成了 FCoE）。
    weak = ([(e, "-vendor-alias") for e, _ in aliases] if aliases else []) \
           + [(q, "")] + [(x, "-synonym") for x in syns]

    res = {}
    for pool, is_weak in ((strong, False), (weak, True)):
        for cand, suffix in pool:
            if not cand:
                continue
            res = _resolve(v, idx, cand, max_bytes, weak=is_weak)
            if res:
                if suffix:
                    res["how"] = str(res.get("how") or "") + suffix
                if suffix == "-vendor-alias":
                    res["note"] = alias_note.get(cand, "")
                break
        if res:
            break

    if not res and aliases:
        # 别名存在但本厂商资料里没有对应条目 ⇒ 如实说"这是别家写法"。
        out["need_ai"] = True
        out["note"] = aliases[0][1]
        out["candidates"] = search(v, q, top_k=5)
        out["reason"] = ("「%s」是其它厂商的写法（%s），本厂商的技术资料里没有对应条目"
                         % (q, aliases[0][1]))
        return out

    if res and res.get("available"):
        out.update(res)
        if out.get("note"):
            # 跨厂商写法映射命中的：把"你敲的是别家写法"写在正文最前面，
            # 否则用户会以为文档写错了。
            out["markdown"] = "> ℹ %s。\n\n" % out["note"] + out["markdown"]
        return out

    # ② 已沉淀的 AI 条目（**仍是代码读取**：它已经是盘上的文件，不需要再调模型）
    ai = lookup_ai(v, q)
    if ai.get("available"):
        body = _strip_ai_header(ai["markdown"])
        out.update({
            "available": True, "source": "ai", "how": "ai-cache",
            "file": ai["file"], "markdown": body,
            "ai_markdown": body, "ai_file": ai["file"], "prefer": "ai",
            "feature": "", "reason": "",
        })
        return out

    # ③ 都没有 → 明确告诉调用方"可以上 AI"（本模块自己**永不**调模型）
    cands = search(v, q, top_k=5)
    out["candidates"] = cands
    out["need_ai"] = True
    out["reason"] = ("技术资料里没有与 %r 对应的讲解（已检索 %d 个 feature / %d 条命令、"
                     "扫过正文、AI 补充层也没有）"
                     % (q, idx.get("n_features", 0), idx.get("n_commands", 0)))
    if res and res.get("reason"):
        out["reason"] = res["reason"]           # 命中了但文件读不到，如实说
    if cands:
        out["reason"] += "；相近的有：%s" % "、".join(c["feature"] for c in cands[:3])
    return out


# ---------------------------------------------------------------- AI 回写
def learn(vendor: str, term: str, markdown: str, question: str = "",
          model: str = "", source: str = "ai", aliases=None) -> dict:
    """把 AI 的回答**回写**成可检索的技术条目（下次就走代码，不再调 AI）。

    与厂商原文**物理隔离**：写进 `tech/ai/`，并在 `tech/ai/index.json` 里登记。
    这样任何时候都能分清"哪句是厂商文档、哪句是模型生成的" —— 这是可信度的底线。

    `aliases`：同一份回答的**别名**（如 feature 全名 `三层技术-IP路由/OSPF` 与叶子名 `OSPF`），
    别名只写索引、不重复写文件 —— 用户敲哪个都能命中同一条。

    只做文件写入，不调模型（调用方负责拿回答）。
    返回 {"ok": bool, "file": str, "reason": str}。
    """
    out = {"ok": False, "file": "", "reason": ""}
    v = str(vendor or "").strip().lower()
    q = str(term or "").strip()
    body = str(markdown or "").strip()
    if not v or not q or not body:
        out["reason"] = "缺少厂商/查询词/正文"
        return out
    slug = re.sub(r"[\\/:*?\"<>|\s]+", "-", q)[:80].strip("-") or "entry"
    rel = "%s/%s.md" % (AI_SUBDIR, slug)
    path = os.path.join(tech_dir(v), rel)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# %s · %s（AI 补充）\n\n" % (v.upper(), q))
            fh.write("> ⚠ **本文件由 AI 生成**，不是厂商文档原文。厂商原文见 `tech/` 下的其它文件。\n")
            if question:
                fh.write("> 原始提问：%s\n" % question)
            if model:
                fh.write("> 生成模型：%s\n" % model)
            fh.write("> 生成时间：%s\n\n---\n\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            fh.write(body + "\n")
        ai_idx_path = os.path.join(tech_dir(v), AI_SUBDIR, "index.json")
        data = {}
        try:
            with open(ai_idx_path, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except (OSError, ValueError):
            data = {}
        entry = {"term": q, "file": "tech/" + rel, "question": question,
                 "model": model, "source": source,
                 "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        data[q.lower()] = dict(entry)
        for a in (aliases or []):
            a = str(a or "").strip()
            if a and a.lower() != q.lower():
                data[a.lower()] = dict(entry, term=a, alias_of=q)
        with open(ai_idx_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        out.update(ok=True, file=rel)
    except OSError as exc:
        out["reason"] = "写入失败：%s" % exc
    return out


def _strip_ai_header(text: str) -> str:
    """剥掉 AI 卡片文件头里的元信息，**保留**"本文件由 AI 生成"那句免责声明。

    为什么要留免责声明：它是这个层的可信度底线 —— 用户必须一眼看出这段不是厂商原文。
    为什么要剥掉其它行（H1 标题、原始提问、模型名、生成时间）：那是给人翻文件看的，
    塞进界面正文只会占地方。
    """
    lines = str(text or "").splitlines()
    out, disclaimer, seen_sep = [], [], False
    for ln in lines:
        s = ln.strip()
        if not seen_sep:
            if s.startswith("#") or s.startswith("> 原始提问") or s.startswith("> 生成模型") \
                    or s.startswith("> 生成时间") or s == "---":
                if s == "---":
                    seen_sep = True
                continue
            if s.startswith(">"):
                disclaimer.append(s)
                continue
            if not s:
                continue
            seen_sep = True          # 没有分隔线的老文件，遇到正文即停
        out.append(ln)
    body = "\n".join(out).strip()
    if disclaimer:
        body = "\n".join(disclaimer) + "\n\n" + body
    return body


def ai_for_feature(vendor: str, feature: str) -> dict:
    """取某个 feature 的 AI 整理卡片（先按叶子名查，再按全名查）。

    为什么要按**叶子名**查：`gen_tech_summary.py` 是用叶子名（`环路检测`）登记的，
    全名（`二层技术-以太网交换/环路检测`）只作为别名 —— 因为用户敲的是叶子名。
    """
    if not feature:
        return {}
    for key in (_leaf(feature), feature):
        hit = lookup_ai(vendor, key)
        if hit.get("available"):
            hit = dict(hit)
            hit["markdown"] = _strip_ai_header(hit.get("markdown", ""))
            return hit
    return {}


def lookup_ai(vendor: str, term: str) -> dict:
    """查 AI 回写层（`tech/ai/index.json`）。厂商原文命中不了时才该走到这里。"""
    v = str(vendor or "").strip().lower()
    q = str(term or "").strip().lower()
    out = {"available": False, "markdown": "", "file": "", "question": "", "at": ""}
    if not v or not q:
        return out
    path = os.path.join(tech_dir(v), AI_SUBDIR, "index.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (OSError, ValueError):
        return out
    node = data.get(q) or {}
    rel = str(node.get("file") or "")
    if not rel:
        return out
    try:
        with open(os.path.join(knowledge_dir(), v, rel), "r", encoding="utf-8") as fh:
            out["markdown"] = fh.read()
    except OSError:
        return out
    out.update(available=True, file=rel, question=str(node.get("question") or ""),
               at=str(node.get("at") or ""))
    return out
