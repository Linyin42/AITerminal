# -*- coding: utf-8 -*-
"""应用侧的**向量 RAG 适配层**：让 `ai.ask` 用上 local_llm 那套向量检索。

为什么要有这一层（背景）：
  `local_llm/retrieve.py` 那套（结构切片 + bge-m3 向量 + numpy 余弦）已经建好并实测通过
  （10/10 命中、30/30 出处真实、注入 ≤1200 字），但应用侧 `ai.ask` 原先只走旧词法层
  `core/knowledge.py`（返回 `source`+`chunk_index`，**没有 小节**），并且用一段两句话的
  通用 system 提示词 —— 也就是说向量 RAG 与 `prompt_local_assistant.txt` 一直没进应用。
  这一层负责把两边接上，并且**只做接线**：检索/预算/格式都在 retrieve.py 里（单一真源）。

设计红线（交付机上这套东西必须"坏不了"）：
  · **全程不抛异常**：任何一步失败（没装 numpy、索引没建、嵌入服务没起、切片缺列）
    都返回空/None，由调用方退回旧词法层。宁可少一点召回，也不能让 AI 面板报错。
  · **懒加载**：numpy/retrieve 只在真正用到时导入，不让应用启动变慢。
  · 只读；不启动任何服务（嵌入服务由 local_llm 自己的脚本管）。
"""
import json
import os
import sys
import threading
import time

#: <仓库根>/local_llm —— 与 core/local_model.py 同一套定位方式
LOCAL_LLM_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_llm")
PROMPT_FILE = os.path.join(LOCAL_LLM_DIR, "prompt_local_assistant.txt")

_lock = threading.Lock()
_cache = {"retrieve": None, "template": None, "vendors": None}


def _retrieve():
    """惰性导入 local_llm/retrieve.py；失败返回 None（不抛）。"""
    with _lock:
        if _cache["retrieve"] is not None:
            return _cache["retrieve"] or None
        try:
            if LOCAL_LLM_DIR not in sys.path:
                sys.path.insert(0, LOCAL_LLM_DIR)
            import retrieve                                   # noqa: WPS433
            _cache["retrieve"] = retrieve
            return retrieve
        except Exception:                                     # noqa: BLE001
            _cache["retrieve"] = False                        # 记"试过了，不可用"
            return None


def index_dir():
    r = _retrieve()
    return getattr(r, "INDEX_DIR", None) if r else None


def vendors():
    """有向量库的厂商列表（必须有 chunks + f16 + meta 三件）。"""
    r = _retrieve()
    if not r:
        return []
    with _lock:
        if _cache["vendors"] is not None:
            return _cache["vendors"]
    out = []
    try:
        idx = r.INDEX_DIR
        for name in sorted(os.listdir(idx)):
            if not name.endswith("_emb.meta.json"):
                continue
            v = name[:-len("_emb.meta.json")]
            if (os.path.exists(os.path.join(idx, f"{v}_chunks.jsonl"))
                    and os.path.exists(os.path.join(idx, f"{v}_emb.f16"))):
                out.append(v)
    except Exception:                                         # noqa: BLE001
        out = []
    with _lock:
        _cache["vendors"] = out
    return out


def available():
    return bool(vendors())


#: 检索失败留痕：这里失败会被上层当成"没检索到"而**静默**退回词法检索 ——
#: 表现是"应用能用、但答得不准"，现场没有任何线索可查（2026-10-17 加）。
_DEGRADED = {"last": "", "seen": set()}


def _note_degraded(vendor, err):
    """记一次向量检索失败（不抛）：内存留最近一条，落盘时同 (厂商, 异常类型) 只记一次。"""
    why = f"{type(err).__name__}: {err}"
    key = (vendor, type(err).__name__)
    with _lock:
        _DEGRADED["last"] = f"{vendor} — {why}"
        if key in _DEGRADED["seen"]:
            return
        _DEGRADED["seen"].add(key)
    try:
        from core import app_paths                            # noqa: WPS433
        with open(os.path.join(app_paths.base_dir(), "aiterminal-rag.log"),
                  "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {vendor} 向量检索失败：{why}\n")
    except Exception:                                         # noqa: BLE001
        pass


def search(query, topk=3, vendor=""):
    """向量检索。vendor 留空则**跨厂商合并**按分数排序。

    跨厂商可比的前提：向量已 L2 归一化 ⇒ 分数就是余弦相似度，与厂商无关。
    返回 [{score, file, section, source, text}]；任何失败返回 []。
    """
    r = _retrieve()
    if not r:
        return []
    targets = [vendor] if vendor else vendors()
    hits = []
    for v in targets:
        try:
            hits.extend(r.search(v, query, topk=topk))
        except (SystemExit, Exception) as e:                  # noqa: BLE001
            # ⚠ 必须连 SystemExit 一起接：`retrieve.load_index` 对"向量库不存在"
            # 是 `raise SystemExit(...)`，而 SystemExit 继承自 BaseException、
            # **不会**被 `except Exception` 捕获 ⇒ 会一路抛穿 ai.ask 把界面搞报错。
            # 单厂商失败（如某厂商还没嵌入）也不该拖垮整次问答。
            _note_degraded(v, e)
            continue
    seen, out = set(), []
    for h in sorted(hits, key=lambda x: -(x.get("score") or 0)):
        key = (h.get("file"), h.get("section"))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= topk:
            break
    return out


def build_context(hits, query=""):
    """按 local_llm 的模板拼注入文本（含出处标签 + 字数预算）。

    `query` 透传给 `retrieve.build_context`：切片长于注入预算时，它按查询挑**相关窗口**
    而不是只取头部（否则长切片里的答案永远进不了上下文）。
    """
    r = _retrieve()
    if not r or not hits:
        return ""
    try:
        return r.build_context(hits, query=query)
    except Exception:                                         # noqa: BLE001
        return ""


def system_prompt(context, vendor="", model=""):
    """套用 `prompt_local_assistant.txt`（含 `{context}/{vendor}/{model}` 占位）。

    这份提示词是专门为小模型调的（编号规则、必须标来源、【未查证】出口、命令纪律）。
    读不到文件时返回空串，由调用方决定回退。
    """
    with _lock:
        tpl = _cache["template"]
    if tpl is None:
        try:
            with open(PROMPT_FILE, "r", encoding="utf-8") as fh:
                tpl = fh.read()
        except Exception:                                     # noqa: BLE001
            tpl = ""
        with _lock:
            _cache["template"] = tpl
    if not tpl:
        return ""
    try:
        return (tpl.replace("{context}", context or "（无可用的厂商资料）")
                   .replace("{vendor}", vendor or "未指定")
                   .replace("{model}", model or "未指定"))
    except Exception:                                         # noqa: BLE001
        return ""


def stat():
    """给界面/日志用的一行状态（不抛）。"""
    vs = vendors()
    if not vs:
        return "向量 RAG 不可用（未找到向量库或依赖缺失）"
    if _DEGRADED["last"]:
        return f"向量 RAG 部分失败（{_DEGRADED['last']}；详见程序目录 aiterminal-rag.log）；厂商 {vs}"
    return f"向量 RAG 可用：厂商 {vs}"
