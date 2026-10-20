"""
终端输出着色器 —— 程序员风格的轻量语法高亮

目的：设备回显不再是一片白字，而是把关键信息用"克制的颜色"标出来，
方便快速扫读（找错误、看 UP/DOWN、找 IP/MAC）。

设计原则（对齐"不花哨、有高级感"）：
  · 只标 6 类关键 token，其余保持默认色，不做整行染色
  · 配色沿用主题青为主强调色，其余用 GitHub Dark 的低饱和色
  · 错误红 / UP 绿 / DOWN 灰 —— 这是运维扫读时真正需要的信号
  · 接口名橙色、IP 蓝、MAC 紫 —— 便于在长输出里定位对象
"""

from PyQt6.QtCore import QRegularExpression
from PyQt6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

# ---------------------------------------------------------------- 配色
C_PROMPT = "#E6EAF2"   # 提示符 <hostname> —— 白（加粗，不用紫色）
C_ERROR = "#F85149"    # 错误 —— 红
C_UP = "#3FB950"       # UP / 成功 —— 绿
C_DOWN = "#93A0B4"     # DOWN / 关闭 —— 灰（刻意弱化，避免和错误混）
C_IFACE = "#d19a66"    # 接口名 —— 橙
C_IP = "#4C8DFF"       # IP —— 天蓝
C_MAC = "#9CC2FF"      # MAC —— 粉


def _fmt(color: str, bold: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Weight.Bold)
    return f


class OutputHighlighter(QSyntaxHighlighter):
    """
    对设备输出做语法着色。

    规则按"先泛后精"排列：后面的规则会覆盖前面命中区域的格式，
    所以把更具体的（MAC/IP/接口名）放在通用词（UP/DOWN）之后，
    避免把 IP 里的数字或接口名里的关键字染错。
    """

    # (正则, 颜色, 加粗)
    RULES = [
        # 1) 错误：H3C/华为以 % 开头的整行报错，或常见英文错误词
        (r"^\s*%.*$", C_ERROR, False),
        (r"\b(Error|Failed|Invalid|Denied|Wrong|Unrecognized|Refused|"
         r"Timed out|Timeout|Unreachable)\b", C_ERROR, False),
        # 2) 状态（整词，避免匹配到 "Uptime" 之类）
        (r"\bUP\b", C_UP, True),
        (r"\b(DOWN|ADM)\b", C_DOWN, False),
        # 3) 接口名（H3C/Cisco/华为常见写法）
        (r"\b(?:GE|XGE|Ten-GigabitEthernet|GigabitEthernet|Ethernet|Eth|"
         r"FastEthernet|Vlan|Vlan-interface|BAGG|Bridge-Aggregation|"
         r"Serial|Tunnel|LoopBack|Loopback|NULL|M-GE|MEth)\s*\d+"
         r"(?:[:/]\d+)*\b", C_IFACE, False),
        # 4) MAC：H3C 用 - 分隔，Cisco 用 . 分隔，标准用 : 分隔
        (r"\b[0-9a-fA-F]{4}[-.][0-9a-fA-F]{4}[-.][0-9a-fA-F]{4}\b", C_MAC, False),
        (r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", C_MAC, False),
        # 5) IP（含可选的 /掩码）
        (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?\b", C_IP, False),
        # 6) 提示符：<hostname> 或 [hostname]
        (r"<[^<>\s]+>", C_PROMPT, True),
        (r"\[[^\[\]\s]+\]", C_PROMPT, True),
    ]

    def __init__(self, document):
        super().__init__(document)
        self._rules = []
        for pattern, color, bold in self.RULES:
            regex = QRegularExpression(pattern)
            if not regex.isValid():
                # 正则写错时跳过，不能让终端打不开
                continue
            self._rules.append((regex, _fmt(color, bold)))

    def highlightBlock(self, text: str):
        for regex, fmt in self._rules:
            it = regex.globalMatch(text)
            while it.hasNext():
                m = it.next()
                start = m.capturedStart()
                length = m.capturedLength()
                if length > 0:
                    self.setFormat(start, length, fmt)