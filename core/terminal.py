"""
终端模拟器 - 负责设备输出的清洗与屏幕缓冲

这一层不依赖 Qt，可单独测试：
    - 剥离 ANSI 转义序列（颜色、光标控制）
    - 处理 \\r 回车覆盖、\\b 退格、\\x07 响铃
    - 维护带行数上限的屏幕缓冲，供 UI 渲染
"""
import re

# ANSI CSI / OSC / 单字符转义
_ANSI_RE = re.compile(
    r'\x1B(?:'
    r'\[[0-?]*[ -/]*[@-~]'      # CSI
    r'|\][^\x07\x1B]*(?:\x07|\x1B\\)'  # OSC
    r'|[@-Z\\-_]'               # 单字符转义
    r')'
)
_BELL_RE = re.compile(r'[\x07\x00]')


class TerminalBuffer:
    """
    终端屏幕缓冲

    参数:
        max_lines: 最多保留的行数，超出后丢弃最旧的行
        max_chars: 单行最大字符数，防止超长行拖慢渲染
    """

    def __init__(self, max_lines: int = 5000, max_chars: int = 4000):
        self.max_lines = max_lines
        self.max_chars = max_chars
        self._lines = [""]

    # ------------------------------------------------------------ 核心逻辑
    @staticmethod
    def strip_ansi(text: str) -> str:
        """移除 ANSI 转义序列"""
        return _ANSI_RE.sub('', text)

    def feed(self, data: str) -> str:
        """
        送入设备原始输出，返回可直接追加显示的纯文本

        处理规则:
            \\r\\n -> 换行
            \\r    -> 回到行首（本行已输出的内容一并从返回值中撤回，避免进度条刷屏）
            \\b    -> 删除一个字符
            \\x07  -> 忽略（响铃）
        """
        if not data:
            return ""

        text = self.strip_ansi(data)
        text = _BELL_RE.sub('', text)

        out = []
        line_start = 0  # 当前行在 out 中的起始下标
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == '\r':
                if i + 1 < n and text[i + 1] == '\n':
                    self._lines.append("")
                    out.append('\n')
                    line_start = len(out)
                    i += 2
                elif i + 1 >= n:
                    # 裸 \r 出现在本批数据**末尾** —— 这是网络设备（H3C / 锐捷 /
                    # 华为等）最常见的行尾写法。必须按换行处理，**不能当"回到行首"清掉**，
                    # 否则设备回显的整行命令会被整个吞掉（曾因此出现"输入的命令不显示"）。
                    self._lines.append("")
                    out.append('\n')
                    line_start = len(out)
                    i += 1
                else:
                    # 裸 \r 后面**还有内容** —— 这才是进度条式的"回到行首重写"，
                    # 清掉本行已输出的内容，避免刷屏
                    self._lines[-1] = ""
                    del out[line_start:]
                    i += 1
            elif ch == '\n':
                self._lines.append("")
                out.append('\n')
                line_start = len(out)
                i += 1
            elif ch == '\b':
                if self._lines[-1]:
                    self._lines[-1] = self._lines[-1][:-1]
                    if len(out) > line_start:
                        out.pop()
                i += 1
            else:
                line = self._lines[-1] + ch
                if len(line) > self.max_chars:
                    line = line[-self.max_chars:]
                self._lines[-1] = line
                out.append(ch)
                i += 1

        self._trim()
        return "".join(out)

    def _trim(self):
        if len(self._lines) > self.max_lines:
            self._lines = self._lines[-self.max_lines:]

    # ------------------------------------------------------------ 查询接口
    def text(self) -> str:
        """返回整个屏幕内容"""
        return "\n".join(self._lines)

    def last_line(self) -> str:
        """返回最后一行（通常是提示符所在行）"""
        return self._lines[-1] if self._lines else ""

    def clear(self):
        """清屏"""
        self._lines = [""]

    def __len__(self):
        return len(self._lines)


# 设备"自身标识"的强特征：出现在版本信息里，可靠
_STRONG_VENDOR_PATTERNS = (
    ("ruijie", r'(ruijie|锐捷)'),
    ("h3c", r'(h3c\s+comware|comware\s+software|comware\s+platform|comware\s+v\d|'
            r'hp\s+comware|h3c\s+technologies)'),
    ("huawei", r'(huawei\s+versatile\s+routing\s+platform|versatile\s+routing\s+platform|'
               r'huawei\s+technologies|quidway)'),
    ("cisco", r'(cisco\s+ios\s+software|cisco\s+internetwork\s+operating\s+system|'
              r'cisco\s+ios\s+xe)'),
)

# 描述/备注类行：里面常出现"对端是华为/思科设备"之类的字样，不能作为厂商依据
_NOISE_LINE_RE = re.compile(
    r'^\s*(description|remark|remark|alias|snmp-agent\s+sys-contact|'
    r'#|!|//|;)\s*',
    re.IGNORECASE
)


def _strip_noise_lines(text: str) -> str:
    """剔除描述、备注、注释行，避免"对端设备名"被误当成厂商依据"""
    kept = []
    for line in (text or '').splitlines():
        if _NOISE_LINE_RE.match(line):
            continue
        kept.append(line)
    return '\n'.join(kept)


def detect_vendor(output: str) -> str:
    """
    从设备输出中粗略识别厂商，用于给 AI 补充上下文

    判定顺序:
        1. 先匹配"设备自身标识"（版本信息里的 Comware / VRP / IOS 等强特征）
        2. 再剔除 description / 注释行后做弱关键词匹配
           —— 否则接口描述里写"对端是华为"就会把 H3C 设备误判成华为

    返回: huawei / h3c / cisco / ruijie / unknown
    """
    text = output or ""
    low = text.lower()

    for vendor, pattern in _STRONG_VENDOR_PATTERNS:
        if re.search(pattern, low):
            return vendor

    cleaned = _strip_noise_lines(text).lower()
    if "ruijie" in cleaned or "锐捷" in text:
        return "ruijie"
    if "h3c" in cleaned or "comware" in cleaned:
        return "h3c"
    if "huawei" in cleaned or re.search(r'\bvrp\b', cleaned):
        return "huawei"
    if "cisco" in cleaned or "ios software" in cleaned:
        return "cisco"
    return "unknown"


def clean_prompt_tail(text: str) -> str:
    """
    去掉输出尾部的提示符与空行，便于送给 AI 分析

    例如 "<H3C>display version\n...\n<H3C>" -> 只保留中间正文
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        tail = lines[-1].strip()
        if tail and len(tail) < 64 and ('>' in tail or '#' in tail or ']' in tail):
            lines.pop()
    return "\n".join(lines)
