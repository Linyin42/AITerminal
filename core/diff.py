"""
配置差异比对 - 用于对比设备配置快照（如两次 display current-configuration）

不依赖 Qt，可单独测试。
"""
import difflib
import re
from typing import List, Tuple

# 匹配设备提示符行，如 <H3C>、[H3C-GigabitEthernet0/0/1]、switch#、Router(config)#
_PROMPT_RE = re.compile(
    r'^\s*(<[^<>]{1,64}>|\[[^\[\]]{1,64}\]|[A-Za-z0-9_.\-]{1,64}[#>])\s*$'
)


def split_lines(text: str) -> List[str]:
    """按行切分并统一换行符、去掉行尾空白"""
    if not text:
        return []
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    return [line.rstrip() for line in normalized.split('\n')]


def strip_prompt_lines(lines: List[str]) -> List[str]:
    """去掉纯提示符行"""
    return [ln for ln in lines if not _PROMPT_RE.match(ln)]


def prepare(text: str, drop_blank: bool = True, drop_prompt: bool = True) -> List[str]:
    """把原始文本整理成可比较的行列表"""
    lines = split_lines(text)
    if drop_prompt:
        lines = strip_prompt_lines(lines)
    if drop_blank:
        lines = [ln for ln in lines if ln.strip()]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def diff_lines(old_text: str, new_text: str, context: int = 3,
               drop_blank: bool = True, drop_prompt: bool = True) -> List[Tuple[str, str]]:
    """
    生成结构化差异

    返回 [(标记, 行内容)]，标记含义:
        ' '  相同行（上下文）
        '+'  新增行
        '-'  删除行
        '@'  省略提示（相同内容过多时折叠）
    """
    old = prepare(old_text, drop_blank, drop_prompt)
    new = prepare(new_text, drop_blank, drop_prompt)

    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    result: List[Tuple[str, str]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            block = old[i1:i2]
            if len(block) <= context * 2 + 1:
                result.extend((' ', line) for line in block)
            else:
                result.extend((' ', line) for line in block[:context])
                result.append(('@', f"…… 省略 {len(block) - context * 2} 行相同内容 ……"))
                result.extend((' ', line) for line in block[-context:])
        elif tag == 'replace':
            result.extend(('-', line) for line in old[i1:i2])
            result.extend(('+', line) for line in new[j1:j2])
        elif tag == 'delete':
            result.extend(('-', line) for line in old[i1:i2])
        elif tag == 'insert':
            result.extend(('+', line) for line in new[j1:j2])

    return result


def summarize(old_text: str, new_text: str, drop_blank: bool = True,
              drop_prompt: bool = True) -> Tuple[int, int]:
    """统计新增、删除行数"""
    added = removed = 0
    for tag, _line in diff_lines(old_text, new_text, context=0,
                                 drop_blank=drop_blank, drop_prompt=drop_prompt):
        if tag == '+':
            added += 1
        elif tag == '-':
            removed += 1
    return added, removed


def has_changes(old_text: str, new_text: str, **kwargs) -> bool:
    """是否存在差异"""
    added, removed = summarize(old_text, new_text, **kwargs)
    return added > 0 or removed > 0


def to_unified(old_text: str, new_text: str, fromfile: str = "基线",
               tofile: str = "对比", context: int = 3,
               drop_blank: bool = True, drop_prompt: bool = True) -> str:
    """生成标准 unified diff 文本（便于导出/存档）"""
    old = prepare(old_text, drop_blank, drop_prompt)
    new = prepare(new_text, drop_blank, drop_prompt)
    return "\n".join(difflib.unified_diff(
        old, new, fromfile=fromfile, tofile=tofile, lineterm='', n=context
    ))


def to_text(diff: List[Tuple[str, str]]) -> str:
    """把结构化差异转成纯文本"""
    return "\n".join(
        (line if tag == '@' else f"{tag}{line}") for tag, line in diff
    )
