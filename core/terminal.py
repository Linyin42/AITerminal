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
        # 本批 feed 是否改写了"已渲染的当前行"（\r 行内重写 / \b 删到上一批的字符）。
        # 为 True 时界面必须整行覆盖，否则旧字符会残留在屏幕上造成重复错乱。
        self.current_line_stale = False
        # 上一批数据以裸 \r 结尾、已按换行处理过。串口按字节分包时
        # \r 与 \n 会落在两个包里，若下一批开头的 \n 再算一次换行，
        # 一次回车就会多跳一个空行 —— 用这个标记把重复的 \n 吞掉。
        self._cr_newline_pending = False
        # 当前行**不许退到比它更短**的字符数：这是设备回显出来的那一段
        # （提示符 + 设备补全），退格只删得动用户自己敲进去的尾巴。不设这道线，
        # 按住退格就会把设备已经回显的提示符一个字符一个字符吃掉
        # （2026-10-19 用户反馈：屏幕上出现 "Ruijie(config)#" → "Ruijie(con"）。
        # 界面层按 `len(提示符) + len(用户还留着的输入)` 设置它。
        self.protected_prefix_len = 0

    def set_protected_prefix_len(self, count: int):
        """指定当前行不许被退格删到比它更短（见 protected_prefix_len 注释）"""
        try:
            self.protected_prefix_len = max(0, int(count or 0))
        except (TypeError, ValueError):
            self.protected_prefix_len = 0

    def _erase_char(self) -> bool:
        """当前行退掉一个字符；只剩受保护前缀时不动。返回是否真的删掉了。"""
        line = self._lines[-1]
        if len(line) <= self.protected_prefix_len:
            return False
        self._lines[-1] = line[:-1]
        return True

    # ------------------------------------------------------------ 核心逻辑
    @staticmethod
    def strip_ansi(text: str) -> str:
        """移除 ANSI 转义序列"""
        return _ANSI_RE.sub('', text)

    def feed(self, data: str, collapse_newline: bool = False) -> str:
        """
        送入设备原始输出，返回可直接追加显示的纯文本

        参数:
            collapse_newline: 把开头的换行当成"回到行首重写"而不是新起一行。
                网络设备（锐捷/H3C 等）响应 Tab 补全时发的是
                "\\r\\n + 提示符 + 补全后的整行"，按真实终端语义会在屏上
                堆出一行行重复的提示符；控制台场景下更希望就地改写当前行。

        处理规则:
            \\r\\n -> 换行
            \\r    -> 回到行首（本行已输出的内容一并从返回值中撤回，避免进度条刷屏）
            \\b    -> 删除一个字符
            \\x07  -> 忽略（响铃）
        """
        if not data:
            return ""

        self.current_line_stale = False
        text = self.strip_ansi(data)
        text = _BELL_RE.sub('', text)
        if not text:
            return ""

        # 上一批以裸 \r 结尾已换行，本批开头的 \n 属于同一个 CRLF，吞掉避免多跳一行
        if self._cr_newline_pending:
            if text[0] == '\n':
                text = text[1:]
            self._cr_newline_pending = False
            if not text:
                self._trim()
                return ""

        out = []
        line_start = 0  # 当前行在 out 中的起始下标
        i = 0
        n = len(text)

        # Tab 补全的整行重画：吃掉开头的换行、清空当前行，改为就地改写
        collapsed = False
        if collapse_newline:
            for prefix in ("\r\n", "\n", "\r"):
                if text.startswith(prefix):
                    text = text[len(prefix):]
                    break
            self._lines[-1] = ""
            collapsed = True
            n = len(text)
        while i < n:
            ch = text[i]
            if ch == '\r':
                if i + 1 < n and text[i + 1] == '\n':
                    self._lines.append("")
                    out.append('\n')
                    line_start = len(out)
                    self.current_line_stale = False
                    i += 2
                elif i + 1 >= n:
                    # 裸 \r 出现在本批数据**末尾** —— 这是网络设备（H3C / 锐捷 /
                    # 华为等）最常见的行尾写法。必须按换行处理，**不能当"回到行首"清掉**，
                    # 否则设备回显的整行命令会被整个吞掉（曾因此出现"输入的命令不显示"）。
                    # 同时记下标记：若 \r\n 被拆到下一批，则吞掉下一批开头的 \n
                    self._lines.append("")
                    out.append('\n')
                    line_start = len(out)
                    self.current_line_stale = False
                    self._cr_newline_pending = True
                    i += 1
                else:
                    # 裸 \r 后面**还有内容**。这里必须先分清是哪种"还有内容"：
                    #   · 后面跟可打印字符（`\r---- More ----`、`\r进度条`）才是
                    #     进度条式的"回到行首重写"，清掉本行已输出的内容，避免刷屏；
                    #   · 后面跟控制字符（`\r\x00`、`\r\r`）说明这只是行尾那串
                    #     CR NUL / CR LF 的一部分，按换行处理。搞反了就是"设备每打
                    #     一行都被自己抹掉"——用户看到"设备有数据、界面没内容"
                    #     （2026-10-19，H3C 每条输出都是 \r\x00\r\n）。
                    if text[i + 1].isprintable():
                        self._lines[-1] = ""
                        del out[line_start:]
                        self.current_line_stale = True
                    else:
                        self._lines.append("")
                        out.append('\n')
                        line_start = len(out)
                        self.current_line_stale = False
                        self._cr_newline_pending = True
                    i += 1
            elif ch == '\n':
                self._lines.append("")
                out.append('\n')
                line_start = len(out)
                self.current_line_stale = False
                i += 1
            elif ch == '\b':
                # 只退得动"用户自己敲进去的那一段"：设备回显的提示符是它的内容，
                # 退格删到它就是屏幕在吃掉设备已经打出来的东西（见 __init__ 注释）
                if self._erase_char():
                    if len(out) > line_start:
                        out.pop()
                    else:
                        # 删掉的是上一批已渲染的字符，屏幕需要整行重画
                        self.current_line_stale = True
                i += 1
            else:
                line = self._lines[-1] + ch
                if len(line) > self.max_chars:
                    line = line[-self.max_chars:]
                self._lines[-1] = line
                out.append(ch)
                i += 1

        if collapsed:
            # 就地改写了当前行：界面需要用整行覆盖，而不是追加
            self.current_line_stale = True
        self._trim()
        return "".join(out)

    def local_write(self, text: str):
        """本地回显：把用户输入写进当前行（与显示区保持镜像）"""
        for ch in text:
            if ch == '\n':
                self._lines.append("")
                continue
            line = self._lines[-1] + ch
            if len(line) > self.max_chars:
                line = line[-self.max_chars:]
            self._lines[-1] = line
        self.current_line_stale = False

    def local_backspace(self) -> bool:
        """本地回显：退格删掉当前行最后一个字符

        返回**是否真的删掉了**：只剩受保护前缀（设备回显的提示符）时返回 False，
        界面据此决定"显示区那个字符也别删"—— 否则本地回显会把设备回显的内容
        从屏幕上挖掉（2026-10-19 用户反馈）。
        """
        erased = self._erase_char()
        self.current_line_stale = False      # 本地回显是直接改显示区，不需要整行重画
        return erased

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
        self.current_line_stale = False
        self._cr_newline_pending = False

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


# 网络设备提示符的常见结尾：Ruijie> / <H3C> / Router# / Switch] / user:
_PROMPT_TAIL_CHARS = ">#]:$"


def looks_like_prompt(line: str) -> bool:
    """
    粗略判断这一行是不是"纯提示符"

    界面上用它记住当前提示符（如 "Ruijie>"），设备重画当前行时才能据此
    反推出用户实际输入的内容，让输入缓冲跟着同步。
    """
    text = (line or "").rstrip()
    return bool(text) and len(text) <= 32 and text[-1] in _PROMPT_TAIL_CHARS


# 各厂商 show/display version 回显里的型号写法（每个厂商内部按"越具体越靠前"排）
_MODEL_PATTERNS = (
    ("ruijie", (
        r'\b(RG-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)\b',
        # 锐捷常把型号写在描述后面：Access Switch (S2928G-E) By Ruijie Networks
        r'\b([A-Z]\d{4}[A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)\b',
    )),
    ("cisco", (
        r'\b(WS-C[A-Za-z0-9\-]+)\b',          # 交换机整机型号最具体
        r'\b(Nexus\s?\d{3,4}[A-Za-z0-9\-]*)\b',   # NX-OS
        r'\b(ISR\d+[A-Za-z0-9\-]*)\b',
        r'\b(C\d{4}[A-Za-z0-9\-]*)\b',        # 兜底：镜像名里的 C2960 之类
    )),
    ("h3c", (
        r'\b(S\d{4}[A-Za-z0-9\-]*)\b',
        r'\b(MSR\d+[A-Za-z0-9\-]*)\b',
    )),
    ("huawei", (
        r'\b(S\d{4}[A-Za-z0-9\-]*)\b',
        r'\b(CE\d{4}[A-Za-z0-9\-]*)\b',
        r'\b(AR\d{4}[A-Za-z0-9\-]*)\b',
        r'\b(NE\d{4}[A-Za-z0-9\-]*)\b',
    )),
)

#: 括号内容：华为版本行把**系列名**写在括号里（5.160 (S12700 ...)），不是整机型号
_PAREN_RE = re.compile(r'\([^)]*\)')

#: 镜像/启动文件行：文件名里也带型号串（如 S7500X-CMW710-BOOT-R7639P03.bin），
#: 2026-10-19 真机测试实测：H3C 的 display version 必带这些行，取最长匹配会把型号认成镜像名。
_IMAGE_LINE_RE = re.compile(r'(\.bin\b|flash:|image)', re.IGNORECASE)


def _uptime_text(text: str) -> str:
    """只留 ``uptime is`` 那几行 —— 整机型号几乎总写在这一行

    ``HUAWEI S12708 Terabit Routing Switch uptime is ...`` /
    ``H3C S7503X uptime is ...``；锐捷写在 System description 里，没有这种行，返回空串。
    """
    return "\n".join(ln for ln in (text or "").splitlines()
                     if "uptime is" in ln.lower())


def _model_source_text(text: str) -> str:
    """去掉镜像/启动文件行之后的全文（型号不写在文件名里）"""
    return "\n".join(ln for ln in (text or "").splitlines()
                     if not _IMAGE_LINE_RE.search(ln))


# 版本号本体：10.4(2b12) Release(154722) / 11.4(1)B70P1 / 15.0(2)SE11
_VERSION_VALUE = (r'([0-9][A-Za-z0-9.()_]*'
                  r'(?:[,\s]+Release\s*\(?[A-Za-z0-9.()_]+\)?)?)')

# 优先取"软件版本"那一行：锐捷/华为的版本信息里 hardware version 也在
# "version:" 后面，直接抓通用的会先把硬件版本 1.05 抓走
_SOFTWARE_VERSION_RE = re.compile(
    r'(?:software|sw|firmware)\s*,?\s*version\s*:?\s*(?:RGOS\s+|VRP\s+|IOS\s+)?'
    + _VERSION_VALUE, re.IGNORECASE)

# 通用写法：Version 15.0(2)SE11 / Version 7.1.070, Release 6126P01
_OS_VERSION_RE = re.compile(
    r'[Vv]ersion\s*:?\s*(?:RGOS\s+|VRP\s+)?' + _VERSION_VALUE)


def _clean_version(text: str) -> str:
    return " ".join(text.replace(",", " ").split())


def _pick_model(text: str, patterns) -> str:
    """
    在一组模式里挑型号：按顺序试，哪个模式先命中就用哪个；
    同一模式命中多处时取最长的 —— 版本行里也会写型号（如 "5.170 (S5720 ...)"），
    而那条通常比开机行里的整机型号短，取长的才不容易认错。

    先在"括号外"找，找不到再退回全文：华为的版本行写成 ``5.160 (S12700 V200R007C00SPC500)``，
    括号里是**系列名**，括号外（``HUAWEI S12708 ... uptime is``）才是整机型号。
    """
    for source in (_PAREN_RE.sub(" ", text), text):
        for pattern in patterns:
            matches = [m.group(1) for m in re.finditer(pattern, source)]
            if matches:
                return max(matches, key=len)
    return ""


def extract_device_info(output: str) -> dict:
    """
    从 show/display version 的回显里提取设备型号与系统版本

    返回: {"vendor": ..., "model": ..., "os_version": ...}
    取不到的字段是空字符串 —— 命令建议靠这几项内容去查/生成，取不到就不折腾。
    """
    info = {"vendor": "unknown", "model": "", "os_version": ""}
    text = output or ""
    if not text.strip():
        return info

    vendor = detect_vendor(text)
    info["vendor"] = vendor

    # 版本：先找"软件版本"，找不到再用通用的 Version 写法
    for pattern in (_SOFTWARE_VERSION_RE, _OS_VERSION_RE):
        match = pattern.search(text)
        if match:
            info["os_version"] = _clean_version(match.group(1))
            break

    # 型号：先按识别出的厂商找；认不出厂商就按各厂商特征逐个试。
    # 候选源按可靠度从高到低试三档：
    #   ① uptime is 行  —— 整机型号几乎总写在这里（最可靠）
    #   ② 去掉镜像行的全文 —— 免得 S7500X-CMW710-*.bin 这类文件名冒充型号
    #   ③ 原始全文      —— 兜底（锐捷的型号在 System description 的括号里，靠这档）
    order = [vendor] if vendor != "unknown" else [name for name, _ in _MODEL_PATTERNS]
    patterns_of = dict(_MODEL_PATTERNS)
    sources = [s for s in (_uptime_text(text), _model_source_text(text), text) if s.strip()]
    for body in sources:
        for name in order:
            patterns = patterns_of.get(name)
            if not patterns:
                continue
            model = _pick_model(body, patterns)
            if model:
                info["model"] = model
                if info["vendor"] == "unknown":
                    info["vendor"] = name
                return info
    return info
