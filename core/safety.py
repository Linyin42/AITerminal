"""
命令安全分级 - 区分"只读命令"与"配置命令"

重要约定（用户要求，长期有效）:
    配置类命令在下发前**必须先展示命令清单并等用户确认**，
    程序不允许自动下发配置。

判定原则:
    白名单式判定 —— 只有明确属于"只读查询"的命令才算安全，
    其余一律按配置命令处理（未知命令宁可多问一次）。
"""

import re

#: 只读查询类命令前缀（H3C / 华为 / Cisco 通用）
READ_ONLY_PREFIXES = (
    "display", "show", "ping", "tracert", "traceroute", "dir", "more", "type",
    "help", "?", "screen-length", "terminal length", "terminal datadump",
    "terminal monitor", "quit", "exit", "return", "end", "who", "whoami",
    "display", "test", "verify",
)

_READ_ONLY_RE = re.compile(
    r'^(' + '|'.join(re.escape(prefix) for prefix in
                     sorted(set(READ_ONLY_PREFIXES), key=len, reverse=True)) + r')\b',
    re.IGNORECASE
)

#: 这些命令一旦出现，要在确认框里给出更明确的警告
RISK_NOTES = (
    (re.compile(r'^\s*(system-view|configure\s+terminal|conf\s+t)\b', re.I),
     "设备将进入配置视图"),
    (re.compile(r'^\s*(save|write\s+memory|wri|copy\s+run)', re.I),
     "会把配置保存到启动配置，重启后仍然生效"),
    (re.compile(r'^\s*(undo|no|delete|erase|reset|clear)\b', re.I),
     "会删除或关闭已有配置项"),
    (re.compile(r'^\s*(reboot|reload|shutdown)\b', re.I),
     "会导致设备重启或端口关闭"),
    (re.compile(r'^\s*(vlan|interface|ip\s+address|port|name|snmp-agent)\b', re.I),
     "会新增或修改配置"),
)


def is_read_only(command: str) -> bool:
    """判断是否为只读查询命令"""
    cmd = (command or "").strip()
    if not cmd:
        return True
    return bool(_READ_ONLY_RE.match(cmd))


def is_config_command(command: str) -> bool:
    """
    判断是否为配置类命令

    只要不是明确的只读命令，就按配置命令处理（未知命令宁可多问一次）。
    """
    cmd = (command or "").strip()
    if not cmd:
        return False
    return not is_read_only(cmd)


def classify(commands) -> dict:
    """
    把命令列表分成只读与配置两类

    返回: {"read_only": [...], "config": [...], "total": n}
    """
    read_only, config = [], []
    for command in commands or []:
        text = str(command).strip()
        if not text:
            continue
        (read_only if is_read_only(text) else config).append(text)
    return {"read_only": read_only, "config": config,
            "total": len(read_only) + len(config)}


def has_config_command(commands) -> bool:
    """命令列表里是否包含配置类命令"""
    return bool(classify(commands)["config"])


def risk_notes(commands) -> list:
    """给出这批命令的风险提示（去重、保持顺序）"""
    notes = []
    for command in commands or []:
        text = str(command).strip()
        for pattern, note in RISK_NOTES:
            if pattern.match(text) and note not in notes:
                notes.append(note)
    return notes


def notes_for(command: str) -> list:
    """**单条**命令命中的风险提示（与 risk_notes() 同一张表，只是按条返回）。

    为什么要有：risk_notes() 是"这批命令合起来"的提示，看不出是哪一条危险；
    确认框逐条核对时，用户需要知道"是这两条里的哪一条会导致设备重启"。
    """
    text = str(command or "").strip()
    if not text:
        return []
    return [note for pattern, note in RISK_NOTES if pattern.match(text)]


def review(commands) -> dict:
    """结构化审查一批命令 —— "生成 → 安全闸门 → 确认 → 下发"里那个闸门的产物。

    与 `classify()` 的分工：classify 只分"只读/配置"两类；这里**逐条**带上风险标注，
    并直接给出"是否必须先人工确认"的结论（用户长期要求：配置类命令下发前必须先确认）。

    返回:
        items        : [{cmd, read_only, config, risk:[…]}]，保持输入顺序
        config       : [cmd…]  会修改设备的命令
        read_only    : [cmd…]  只读查询
        total        : 条数
        needs_confirm: bool —— 有配置类命令就是 True（只读全放行）
        summary      : str  —— 可直接给确认框/日志的结论
    """
    items, read_only, config = [], [], []
    for command in commands or []:
        text = str(command).strip()
        if not text:
            continue
        is_ro = is_read_only(text)
        items.append({"cmd": text, "read_only": is_ro, "config": not is_ro,
                      "risk": notes_for(text)})
        (read_only if is_ro else config).append(text)

    total = len(items)
    if not total:
        summary = "没有可执行的命令"
    else:
        summary = (f"共 {total} 条命令：只读 {len(read_only)} 条，配置 {len(config)} 条"
                   + ("；必须先人工确认" if config else "；均为只读，可直接执行"))
        notes = risk_notes(commands)
        if notes:
            summary += "｜风险：" + "；".join(notes)
    return {"items": items, "config": config, "read_only": read_only, "total": total,
            "needs_confirm": bool(config), "summary": summary}


def describe(commands) -> str:
    """生成一段可读的命令说明（用于确认框与日志）"""
    result = classify(commands)
    lines = [f"共 {result['total']} 条命令："
             f"只读 {len(result['read_only'])} 条，"
             f"配置 {len(result['config'])} 条"]
    if result["config"]:
        lines.append("")
        lines.append("配置类命令（会修改设备）:")
        lines.extend(f"  {index}. {cmd}" for index, cmd in enumerate(result["config"], 1))
    if result["read_only"]:
        lines.append("")
        lines.append("只读命令:")
        lines.extend(f"  {index}. {cmd}" for index, cmd in enumerate(result["read_only"], 1))
    notes = risk_notes(commands)
    if notes:
        lines.append("")
        lines.append("风险提示:")
        lines.extend(f"  ! {note}" for note in notes)
    return "\n".join(lines)
