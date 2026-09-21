"""
命令模板 - 带变量的常用配置/巡检命令组

模板里的 {变量名} 会在界面上生成输入框，填写后渲染成实际命令。
"""
import re
from typing import Dict, List

_VAR_RE = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}')

COMMAND_TEMPLATES: Dict[str, dict] = {
    # ---------------------------------------------------------- 配置类
    "创建VLAN并划分端口": {
        "desc": "创建VLAN并把指定接口以access方式划入（H3C/华为语法）",
        "vendor": "h3c",
        "variables": {
            "vlan_id": "10",
            "vlan_name": "USERS",
            "interface": "GigabitEthernet1/0/1",
        },
        "commands": [
            "system-view",
            "vlan {vlan_id}",
            "name {vlan_name}",
            "quit",
            "interface {interface}",
            "port link-type access",
            "port default vlan {vlan_id}",
            "quit",
            "return",
        ],
    },
    "配置接口IP地址": {
        "desc": "给三层接口配置IP并启用（H3C/华为语法）",
        "vendor": "h3c",
        "variables": {
            "interface": "Vlan-interface10",
            "ip": "192.168.10.1",
            "mask": "255.255.255.0",
        },
        "commands": [
            "system-view",
            "interface {interface}",
            "ip address {ip} {mask}",
            "undo shutdown",
            "quit",
            "return",
        ],
    },
    "配置静态路由": {
        "desc": "配置一条静态路由（H3C/华为语法）",
        "vendor": "h3c",
        "variables": {
            "network": "10.10.0.0",
            "mask": "255.255.0.0",
            "next_hop": "192.168.10.254",
        },
        "commands": [
            "system-view",
            "ip route-static {network} {mask} {next_hop}",
            "return",
        ],
    },
    "配置SNMP只读团体名": {
        "desc": "开启SNMPv2c只读，供网管采集",
        "vendor": "h3c",
        "variables": {
            "community": "monitor_ro",
            "acl": "2000",
        },
        "commands": [
            "system-view",
            "snmp-agent",
            "snmp-agent sys-info version v2c",
            "snmp-agent community read {community} acl {acl}",
            "return",
        ],
    },
    "Cisco: 创建VLAN并划分端口": {
        "desc": "Cisco IOS 语法",
        "vendor": "cisco",
        "variables": {
            "vlan_id": "10",
            "vlan_name": "USERS",
            "interface": "GigabitEthernet0/1",
        },
        "commands": [
            "configure terminal",
            "vlan {vlan_id}",
            "name {vlan_name}",
            "exit",
            "interface {interface}",
            "switchport mode access",
            "switchport access vlan {vlan_id}",
            "exit",
            "end",
        ],
    },
    "Cisco: 配置接口IP": {
        "desc": "Cisco IOS 语法",
        "vendor": "cisco",
        "variables": {
            "interface": "GigabitEthernet0/0",
            "ip": "192.168.10.1",
            "mask": "255.255.255.0",
        },
        "commands": [
            "configure terminal",
            "interface {interface}",
            "ip address {ip} {mask}",
            "no shutdown",
            "exit",
            "end",
        ],
    },
    # ---------------------------------------------------------- 巡检类
    "设备基础巡检": {
        "desc": "版本、接口、CPU/内存、日志（H3C/华为语法）",
        "vendor": "h3c",
        "variables": {},
        "commands": [
            "display version",
            "display interface brief",
            "display cpu-usage",
            "display memory",
            "display logbuffer",
        ],
    },
    "接口错包与流量检查": {
        "desc": "排查接口丢包、CRC 错包",
        "vendor": "h3c",
        "variables": {},
        "commands": [
            "display interface brief | include UP|DOWN",
            "display interface",
            "display counters inbound interface",
            "display counters outbound interface",
        ],
    },
    "备份当前配置": {
        "desc": "输出全量配置，可用于快照比对",
        "vendor": "h3c",
        "variables": {},
        "commands": [
            "display current-configuration",
        ],
    },
    "Cisco: 基础巡检": {
        "desc": "Cisco IOS 语法",
        "vendor": "cisco",
        "variables": {},
        "commands": [
            "show version",
            "show ip interface brief",
            "show processes cpu",
            "show logging",
        ],
    },
    # ---------------------------------------------------------- 保存类
    "保存配置（H3C/华为）": {
        "desc": "把当前配置写入启动配置",
        "vendor": "h3c",
        "variables": {},
        "commands": [
            "save force",
        ],
    },
    "保存配置（Cisco）": {
        "desc": "写入 NVRAM",
        "vendor": "cisco",
        "variables": {},
        "commands": [
            "write memory",
        ],
    },
}


def extract_variables(commands: List[str]) -> List[str]:
    """从命令列表中提取变量名（保持出现顺序、去重）"""
    seen = []
    for command in commands:
        for name in _VAR_RE.findall(command):
            if name not in seen:
                seen.append(name)
    return seen


def render_template(commands: List[str], variables: Dict[str, str]) -> List[str]:
    """
    渲染模板

    参数:
        commands: 模板命令列表
        variables: 变量取值
    返回:
        渲染后的命令列表（未提供的变量保持 {name} 原样，便于人工补全）
    """

    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    safe = _SafeDict({k: (v if v is not None else "") for k, v in (variables or {}).items()})
    rendered = []
    for command in commands:
        try:
            rendered.append(command.format_map(safe))
        except Exception:
            rendered.append(command)
    return rendered


def template_commands(name: str, variables: Dict[str, str] = None) -> List[str]:
    """按模板名渲染命令"""
    tpl = COMMAND_TEMPLATES.get(name)
    if not tpl:
        return []
    return render_template(tpl["commands"], variables or {})


def save_config_commands(vendor: str) -> List[str]:
    """按厂商返回保存配置命令"""
    vendor = (vendor or "").lower()
    if vendor == "cisco":
        return ["write memory"]
    return ["save force"]
