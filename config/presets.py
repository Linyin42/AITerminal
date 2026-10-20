"""
预设连接配置 - 常用厂商设备的默认参数与常用命令
"""

# 各协议的默认参数
PROTOCOL_DEFAULTS = {
    "SSH": {"port": 22},
    "Telnet": {"port": 23},
    "Serial": {"port": 0, "baudrate": 9600},
    # eNSP Pro 平台固定 8443（设备命令行走 HTTP+STOMP，不是 TCP 端口）
    "eNSP": {"port": 8443},
}

# 快速预设（连接对话框"快速预设"下拉使用）
# 只保留 SSH / Telnet / Console 三项；主机、串口、凭据由用户自行填写，
# 不再硬编码 COM 口 / 用户名（硬编码 COM3 曾导致"连了错误串口、终端无输出"）
PRESET_CONNECTIONS = {
    "SSH 连接": {
        "protocol": "SSH",
        "port": 22,
    },
    "Telnet 连接": {
        "protocol": "Telnet",
        "port": 23,
    },
    "Console 连接": {
        "protocol": "Serial",
    },
    "eNSP Pro 连接": {
        "protocol": "eNSP",
        "port": 8443,
    },
}

# 各厂商常用排查/配置命令，供 AI 面板"快捷命令"与人工参考
VENDOR_COMMANDS = {
    "huawei": {
        "查看版本": "display version",
        "接口状态": "display interface brief",
        "当前配置": "display current-configuration",
        "路由表": "display ip routing-table",
        "ARP表": "display arp all",
        "MAC表": "display mac-address",
        "CPU/内存": "display cpu-usage",
        "日志": "display logbuffer",
        "进入配置视图": "system-view",
        "保存配置": "save",
    },
    "h3c": {
        "查看版本": "display version",
        "接口状态": "display interface brief",
        "当前配置": "display current-configuration",
        "路由表": "display ip routing-table",
        "ARP表": "display arp all",
        "MAC表": "display mac-address",
        "CPU/内存": "display cpu-usage",
        "日志": "display logbuffer",
        "进入配置视图": "system-view",
        "保存配置": "save force",
    },
    "cisco": {
        "查看版本": "show version",
        "接口状态": "show ip interface brief",
        "当前配置": "show running-config",
        "路由表": "show ip route",
        "ARP表": "show arp",
        "MAC表": "show mac address-table",
        "CPU/内存": "show processes cpu",
        "日志": "show logging",
        "进入配置视图": "configure terminal",
        "保存配置": "write memory",
    },
    "ruijie": {
        "查看版本": "show version",
        "接口状态": "show interfaces status",
        "当前配置": "show running-config",
        "路由表": "show ip route",
        "ARP表": "show arp",
        "MAC表": "show mac-address-table",
        "日志": "show logging",
        "进入配置视图": "configure terminal",
        "保存配置": "write",
    },
}

# 常用任务模板，填充 AI 面板的"任务描述"
TASK_TEMPLATES = [
    "检查设备版本和接口状态",
    "排查某个接口不通的原因",
    "查看设备CPU和内存使用率",
    "检查路由表和ARP表是否正常",
    "备份设备当前配置",
    "配置VLAN并划分端口",
    "配置接口IP地址并启用",
    "排查网络环路与广播风暴",
]


def get_vendor_commands(vendor: str) -> dict:
    """按厂商返回常用命令，未知厂商回退到 h3c"""
    return VENDOR_COMMANDS.get((vendor or "").lower(), VENDOR_COMMANDS["h3c"])


# 巡检绝不能改设备配置：这几条要剔掉（用户 2026-10-17：巡检要"自动的调用相关的模板"）
PATROL_SKIP_LABELS = ("进入配置视图", "保存配置")


def get_patrol_commands(vendor: str) -> list:
    """按厂商返回**巡检用**的只读命令（show/display 那几条）"""
    vendor = (vendor or "").lower()
    if vendor not in VENDOR_COMMANDS:
        return []                       # 认不出厂家就不猜，交给用户手动选模板
    return [command for label, command in VENDOR_COMMANDS[vendor].items()
            if label not in PATROL_SKIP_LABELS]


def get_patrol_commands_for(vendors) -> list:
    """多台不同厂家的设备一起巡检：按厂家顺序合并，命令去重"""
    merged: list = []
    for vendor in vendors or []:
        for command in get_patrol_commands(vendor):
            if command not in merged:
                merged.append(command)
    return merged


# ======================================================================
# Console / 串口相关选项与预设
# ======================================================================

# 常用波特率（含设备 Console 常见档位）
BAUDRATES = [
    1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600,
    115200, 128000, 230400, 460800, 921600,
]

# (显示文本, 值)
PARITY_OPTIONS = [
    ("无校验 N", "N"),
    ("偶校验 E", "E"),
    ("奇校验 O", "O"),
    ("Mark M", "M"),
    ("Space S", "S"),
]

STOPBIT_OPTIONS = [
    ("1", 1),
    ("1.5", 1.5),
    ("2", 2),
]

DATABITS_OPTIONS = [5, 6, 7, 8]

FLOW_OPTIONS = [
    ("无流控", "none"),
    ("RTS/CTS 硬件流控", "rtscts"),
    ("XON/XOFF 软件流控", "xonxoff"),
    ("DSR/DTR 流控", "dsrdtr"),
]

# Console 行结束符：多数网络设备只认 CR
LINE_ENDING_OPTIONS = [
    ("CR (\\r) — 网络设备 Console", "\r"),
    ("LF (\\n) — Linux 主机", "\n"),
    ("CRLF (\\r\\n) — Windows 主机", "\r\n"),
    ("不追加", ""),
]

# 常用 Console 预设：覆盖绝大多数网络设备
CONSOLE_PRESETS = {
    "Cisco / 通用网络设备 (9600-8-N-1)": {
        "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
        "flow": "none", "line_ending": "\r", "char_delay_ms": 0,
        "note": "Cisco 路由器/交换机、大部分国产设备出厂默认",
    },
    "华为 / H3C (9600-8-N-1)": {
        "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
        "flow": "none", "line_ending": "\r", "char_delay_ms": 0,
        "note": "华为/H3C 交换机路由器 Console 默认参数",
    },
    "Juniper (9600-8-N-1)": {
        "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
        "flow": "none", "line_ending": "\r", "char_delay_ms": 0,
        "note": "Juniper 设备 Console 默认",
    },
    "高速 Console (115200-8-N-1)": {
        "baudrate": 115200, "bytesize": 8, "parity": "N", "stopbits": 1,
        "flow": "none", "line_ending": "\r", "char_delay_ms": 0,
        "note": "部分新型设备、服务器 BMC、单板 Console 使用 115200",
    },
    "老设备 / 慢速链路 (9600-8-N-1 + 字符间隔)": {
        "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
        "flow": "none", "line_ending": "\r", "char_delay_ms": 8,
        "note": "老设备或长线缆容易丢字符，逐字符发送更稳",
    },
    "需要 DTR 拉低才能进 BootROM": {
        "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
        "flow": "none", "line_ending": "\r", "char_delay_ms": 0,
        "dtr": False,
        "note": "部分设备把 DTR 拉低视为 BootROM 触发条件",
    },
    "7 数据位 + 偶校验 (7-E-1)": {
        "baudrate": 9600, "bytesize": 7, "parity": "E", "stopbits": 1,
        "flow": "none", "line_ending": "\r", "char_delay_ms": 0,
        "note": "少数老式设备/终端服务器使用 7E1",
    },
}

# Console 排查提示，显示在连接对话框串口页
CONSOLE_HINTS = [
    "端口被占用：先关掉 MobaXterm / SecureCRT / PuTTY 等占用同一串口的软件。",
    "没有任何输出：按一下回车；仍无输出就检查波特率是否匹配（常见 9600 / 115200）。",
    "输出乱码：波特率或校验位不对，逐个试 9600 / 115200，校验多为 N。",
    "只能看到一半字符 / 丢字符：把“字符发送间隔”调到 5~10ms。",
    "设备刚上电想看启动日志：接好串口后再给设备上电，避免漏掉开头。",
    "进 BootROM：设备上电瞬间连续点“发送 Break”，或把 DTR 拉低。",
    "USB 转串口被拔掉：勾选“自动重连”，插回后会自动恢复会话。",
]
