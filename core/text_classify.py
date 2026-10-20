"""终端文本语义分类 —— **C++ `core/TextClassify.h` 的 Python 镜像**（同一语义、逐条对齐）。

⚠ 用途说明：**供未来的非 C++ UI / 引擎侧复用，不进渲染路径**。
   现行渲染路径的判定在 C++（`cpp/src/core/TextClassify.cpp`，进程内调用，零往返）；
   本镜像只用于：① 非 C++ 宿主经 `text.classify` RPC 取同一口径；
   ② 与 C++ 侧做**逐条一致性检查**（`_automation/check_classify_parity.py`，33 条语料、0 差异）。
   两侧任一侧漂移都会让 parity 变红 —— 这是防止"两份实现各说各话"的防线。

返回的是**语义标签**（prompt/ok/iface/neutral），不是颜色；颜色归 UI。
"""
from __future__ import annotations

import re

PROMPT_RE = re.compile(
    r"^\s*(?:<[^<>\s]{1,64}>|\[[^\[\]\s]{1,64}\]|[A-Za-z0-9._()\-]{1,32}[>#])")
IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")
IPV6_RE = re.compile(
    # lookahead-guard: 纯数字冒号串（如 8:8:43:37）不得当 IPv6，必须含至少一个 a-f
    r"\b(?=[0-9A-Fa-f:]*[A-Fa-f])(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b"
    r"|\b(?:[0-9A-Fa-f]{1,4}:){1,7}:"
    r"|::(?:[0-9A-Fa-f]{1,4}:){0,6}[0-9A-Fa-f]{1,4}")
MAC_RE = re.compile(
    r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"
    r"|\b[0-9A-Fa-f]{4}(?:-[0-9A-Fa-f]{4}){2}\b"
    r"|\b[0-9A-Fa-f]{4}(?:\.[0-9A-Fa-f]{4}){2}\b")
CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
DATE_RE = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{4}/\d{1,2}/\d{1,2}\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\s+\d{4}\b",
    re.IGNORECASE)
IFACE_RE = re.compile(
    r"\b(?:GigabitEthernet|Ten-GigabitEthernet|M-GigabitEthernet|XGE|FGE|GE|MEth|"
    r"Eth-Trunk|Bridge-Aggregation|Route-Aggregation|BAGG|RAGG|Vlan-interface|Vlanif|"
    r"Vlan|LoopBack|Loopback|NULL|Tunnel|Serial|Cellular)\s?\d+(?:[/\-]\d+)*\b"
    r"|\b[A-Za-z][A-Za-z\-]{1,20}\d+/\d+(?:/\d+)*\b",
    re.IGNORECASE)
OK_RE = re.compile(
    r"Line\s+con\d+\s+is\s+available|Press\s+ENTER|"
    r"(?<![A-Za-z0-9_-])logged\s+in(?![A-Za-z0-9_-])"
    r"|(?<![A-Za-z0-9_-])Done(?![A-Za-z0-9_-])"
    r"|(?<![A-Za-z0-9_-])Saved(?![A-Za-z0-9_-])"
    r"|(?<![A-Za-z0-9_-])success(?:ful(?:ly)?)?(?![A-Za-z0-9_-])"
    r"|(?<![A-Za-z0-9_-])OK(?![A-Za-z0-9_-])"
    r"|已连接|已保存|已成功",
    re.IGNORECASE)

NEUTRAL, PROMPT, OK, IFACE = "neutral", "prompt", "ok", "iface"


def looks_like_address(text: str) -> bool:
    """这段文本里有没有真的 IP/MAC（时钟/日期逐个候选排除）。"""
    if IPV4_RE.search(text):
        return True
    for m in IPV6_RE.finditer(text):
        hit = m.group(0)
        if not CLOCK_RE.match(hit) and not DATE_RE.search(hit):
            return True
    for m in MAC_RE.finditer(text):
        if not DATE_RE.search(m.group(0)):
            return True
    return False


def classify_run(run_text: str, line_before: str = "", device_colored: bool = False) -> str:
    """与 C++ `core::classifyRun` 同序：deviceColored -> Neutral；ok(body) -> Ok；
    prompt/地址/lineBefore -> Prompt；ok(run) -> Ok；iface -> Iface；否则 Neutral。"""
    if device_colored:
        return NEUTRAL
    m = PROMPT_RE.match(run_text)
    body = run_text[m.end():] if m else run_text
    if OK_RE.search(body):
        return OK
    if m or looks_like_address(run_text) or PROMPT_RE.match(line_before or ""):
        return PROMPT
    if OK_RE.search(run_text):
        return OK
    if IFACE_RE.search(run_text):
        return IFACE
    return NEUTRAL
