"""
全局配置 - 读写用户配置（默认存放于 ~/.aiterminal/settings.json）

配置优先级: 环境变量 > 配置文件 > 默认值
"""
import os
import json
from typing import Any, Dict, List, Optional

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".aiterminal")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS: Dict[str, Any] = {
    "api_key": "",
    "ai_provider": "anthropic",     # anthropic / deepseek / longcat / openai_compat
    "api_base": "",                 # OpenAI 兼容协议的接口地址（留空用提供方默认）
    "provider_keys": {},            # {提供方: 密钥}，切换提供方时各存各的
    "provider_configs": {},         # {提供方: {"model":…, "base_url":…}}
    "model": "claude-sonnet-4-20250514",
    "system_prompt": "",            # 空字符串表示使用 AIAgent 内置提示词
    "theme": "dark",
    "font_family": "Consolas",
    "font_size": 10,
    "auto_mode_default": False,
    "block_dangerous": True,
    "auto_log": True,               # 会话输出自动落盘
    "log_dir": "",                  # 空 = ~/.aiterminal/logs
    "restore_sessions": True,       # 启动时询问是否恢复上次的会话
    "save_passwords": False,        # 是否在会话列表中保存密码（仅混淆，非加密）
    "control_api_enabled": False,   # 外部接管（DSH/MCP）回环控制通道：默认关
    "batch_delay_ms": 500,          # 批量下发命令间隔
    "batch_wait_mode": "prompt",    # prompt=等提示符返回 / delay=固定间隔
    "batch_prompt_timeout": 30,     # 等提示符的最长等待秒数
    "batch_auto_page": True,        # 遇到分页标记自动翻页
    "session_auto_page": True,      # 交互会话也自动应答 ---- More ----（设备停在分页
                                    # 时用户看到的就是"设备有数据、界面没内容"）
    "confirm_config_commands": True,  # 配置类命令下发前必须人工确认（安全闸门）
    # 多设备并发巡检
    "patrol_concurrency": 4,
    "patrol_timeout": 10,           # 单台设备连接超时
    "patrol_prompt_timeout": 30,    # 单条命令最长等待
    "patrol_commands": [],          # 上次使用的巡检命令
    "patrol_devices": [],           # 上次选择的巡检设备
    # 联网检索
    "search_backend": "auto",       # auto / baidu / bing / duckduckgo / searxng / off
    "searxng_url": "",              # 使用 searxng 时的实例地址
    "search_max_results": 5,
    "search_read_pages": 2,         # 每次检索抓取几篇正文
    "search_page_chars": 4000,      # 单篇正文最多取多少字符
    "ai_auto_search": True,         # 允许 AI 自主发起联网检索
    # 本地资料库
    "knowledge_dir": "",            # 空 = ~/.aiterminal/knowledge
    "knowledge_enabled": True,      # 查命令时是否检索本地资料
    "knowledge_top_k": 4,           # 本地检索返回的片段数
    "knowledge_max_chars": 4000,    # 本地检索结果最多取多少字符
    # 启动就建/加载资料库索引？默认 False：`.index.json` 本机实测 676 MB，读成
    # Python 对象后常驻内存好几 GB；不用资料库时纯属白占（要跑模拟器时尤其明显）。
    # 关掉不影响功能：打开资料管理 / 导入资料 / AI 检索时会自动建与加载。
    "knowledge_autobuild": False,
    # 应用启动后自动拉起本地模型（起在**显存**上，不占内存；Vulkan 后端）。
    # 默认关；打开后"软件一起来"本地 LLM 就在 GPU 上跑着，AI 提供方选 local 即可用。
    "local_model_autostart": False,
    # 拉起本地模型时指定的档位（`run_server.py --tier`）。空 = 自动探测（本机是 mid，
    # 即官方 Qwen3-4B）。填 "ait" 则用自训模型 Qwen3-4B-AIT-Q4_K_M.gguf —— 2026-10-22 重训后
    # 带检索的严格命中 48.2% vs 官方 32.1%，但英文手册题略差，所以**默认不选、保持 opt-in**。
    "local_tier": "",
    # 「打开资料管理先弹导入要求」：勾过"不再显示"写 True；不勾就每次弹（2026-10-17）
    "import_tip_dismissed": False,
    # Console / 串口默认参数
    "serial_defaults": {
        "baudrate": 9600,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "flow": "none",
        "line_ending": "\r",
        "char_delay_ms": 0,
        "auto_reconnect": True,
        # 网络设备（交换机/路由器）Console 口基本都自带回显，默认关掉本地回显；
        # 设备确实不回显（如部分 BootROM）时才勾选 —— 否则两边同时回显，屏幕会重影
        "local_echo": False,
    },
    "last_connection": {},
    "recent_connections": [],
    "last_sessions": [],
    "vault_devices": [],         # 设备库（用户保存的设备，不含密码，按 host+port 去重）
    "window": {"width": 1400, "height": 900},
}

# 串口默认参数（合并用）
SERIAL_DEFAULTS: Dict[str, Any] = dict(DEFAULTS["serial_defaults"])


class Settings:
    """配置管理（单例式使用）"""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self, config_file: str = None):
        if getattr(self, "_loaded", False):
            return
        self.config_file = config_file or CONFIG_FILE
        self._data: Dict[str, Any] = dict(DEFAULTS)
        self.load()
        self._load_env()
        self._loaded = True

    # ------------------------------------------------------------ 读写
    def load(self):
        """从磁盘加载配置"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._data.update(data)
        except Exception:
            # 配置损坏时回退到默认值，不影响启动
            self._data = dict(DEFAULTS)
        return self._data

    def save(self):
        """保存配置到磁盘"""
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def _load_env(self):
        """从 .env / 环境变量补充敏感配置"""
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        if os.environ.get("ANTHROPIC_API_KEY"):
            self._data["api_key"] = os.environ["ANTHROPIC_API_KEY"]
        if os.environ.get("ANTHROPIC_MODEL"):
            self._data["model"] = os.environ["ANTHROPIC_MODEL"]

    # ------------------------------------------------------------ 访问
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any):
        self._data[key] = value

    def update(self, values: Dict[str, Any]):
        self._data.update(values)

    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    # ------------------------------------------------------------ 常用项
    @property
    def api_key(self) -> str:
        """当前提供方的密钥（旧配置里只有 api_key 字段时按 Anthropic 处理）"""
        keys = self._data.get("provider_keys") or {}
        provider = self.ai_provider
        if provider in keys and keys.get(provider):
            return keys[provider]
        return self.get("api_key", "")

    @api_key.setter
    def api_key(self, value: str):
        self.set_api_key_for(self.ai_provider, value)

    # ------------------------------------------------------------ 大模型提供方
    @property
    def ai_provider(self) -> str:
        """当前提供方（非法值回落到 anthropic）

        ⚠ 白名单必须取自 `core.llm.PROVIDERS`（唯一真源），**不能硬编码**。
        踩过的坑：这里原本写死 `("anthropic", "deepseek", "openai_compat")`，
        于是把 `ai_provider` 设成新加的 `longcat` 时属性**静默**返回 `anthropic` ——
        症状是"未安装 anthropic 库，请执行 pip install anthropic"，
        你会跑去装库、查网络，而真正的原因是白名单没加。
        """
        name = (self.get("ai_provider") or "anthropic").lower()
        try:
            from core.llm import PROVIDERS          # noqa: WPS433（函数内导入，避免循环）
            known = tuple(PROVIDERS.keys())
        except Exception:                           # noqa: BLE001
            known = ("anthropic", "deepseek", "longcat", "openai_compat")
        return name if name in known else "anthropic"

    @ai_provider.setter
    def ai_provider(self, value: str):
        self.set("ai_provider", value)

    def set_api_key_for(self, provider: str, value: str):
        """按提供方分别保存密钥，切换时不会互相覆盖"""
        provider = (provider or "anthropic").lower()
        keys = dict(self._data.get("provider_keys") or {})
        keys[provider] = value or ""
        self._data["provider_keys"] = keys
        # 兼容旧字段：当前提供方同时写一份到 api_key
        if provider == self.ai_provider:
            self.set("api_key", value or "")

    def get_api_key_for(self, provider: str) -> str:
        keys = self._data.get("provider_keys") or {}
        return keys.get((provider or "").lower(), "") or (
            self.get("api_key", "") if (provider or "").lower() == self.ai_provider else "")

    @property
    def api_base(self) -> str:
        """OpenAI 兼容协议的接口地址（留空表示用提供方默认）"""
        return (self.get("api_base") or "").strip()

    @api_base.setter
    def api_base(self, value: str):
        self.set("api_base", (value or "").strip())

    def get_provider_config(self, provider: str) -> Dict[str, str]:
        """
        取某提供方的模型与接口地址（缺失项回落到该提供方默认值）

        返回: {"model": ..., "base_url": ...}
        """
        from core.llm import provider_defaults
        provider = (provider or "anthropic").lower()
        defaults = provider_defaults(provider)
        stored = (self._data.get("provider_configs") or {}).get(provider) or {}
        return {
            "model": (stored.get("model") or "").strip() or defaults["model"],
            "base_url": (stored.get("base_url") or "").strip() or defaults["base_url"],
        }

    def set_provider_config(self, provider: str, model: str = None,
                            base_url: str = None):
        """记住某提供方用的模型与接口地址"""
        provider = (provider or "anthropic").lower()
        configs = dict(self._data.get("provider_configs") or {})
        current = dict(configs.get(provider) or {})
        if model is not None:
            current["model"] = (model or "").strip()
        if base_url is not None:
            current["base_url"] = (base_url or "").strip()
        configs[provider] = current
        self._data["provider_configs"] = configs
        # 当前提供方同时同步到 model / api_base，保持旧逻辑可用
        if provider == self.ai_provider:
            if current.get("model"):
                self.set("model", current["model"])
            if current.get("base_url") is not None:
                self.set("api_base", current.get("base_url", ""))

    @property
    def model(self) -> str:
        return self.get("model", DEFAULTS["model"])

    @property
    def font_family(self) -> str:
        return self.get("font_family", "Consolas")

    @property
    def font_size(self) -> int:
        return int(self.get("font_size", 10))

    @property
    def block_dangerous(self) -> bool:
        return bool(self.get("block_dangerous", True))

    # ------------------------------------------------------------ 连接记忆
    def get_last_connection(self) -> Optional[Dict[str, Any]]:
        last = self.get("last_connection") or {}
        return last or None

    def set_last_connection(self, config: Dict[str, Any]):
        """
        记录"上次连接"。

        注意：**必须保留 remember 标志**。曾经这里把它剥掉了，导致：
        勾"记住"连接后，下次打开对话框复选框是未勾选状态 → 用户直接点连接
        → remember=False → 已存的密码又被过滤掉 → "记住"只能撑一次。
        """
        self._data["last_connection"] = dict(config)
        self.add_recent_connection(config)

    def add_recent_connection(self, config: Dict[str, Any]):
        """维护最近连接列表（最多10条，按 host+port 去重；**不存密码**）"""
        recents = list(self.get("recent_connections") or [])
        key = (config.get('protocol'), config.get('host'), config.get('port'))
        recents = [
            r for r in recents
            if (r.get('protocol'), r.get('host'), r.get('port')) != key
        ]
        recents.insert(0, {k: v for k, v in config.items() if k != 'password'})
        self._data["recent_connections"] = recents[:10]

    def get_recent_connections(self):
        return list(self.get("recent_connections") or [])

    # ------------------------------------------------------------ 设备库 Vault
    def _vault_key(self, config: Dict[str, Any]):
        """设备去重键：协议 + 主机(或串口) + 端口

        eNSP Pro 是例外：同一台服务器上所有设备的 host 与 port 完全相同，
        只用「协议+主机+端口」会导致第二台设备覆盖第一台。
        所以 eNSP 的去重键要再带上沙箱号与设备号。
        """
        host = config.get('host') or config.get('serial_port') or ''
        proto = (config.get('protocol') or '').strip().lower()
        if proto in ('ensp', 'ensp-pro', 'ensppro', 'ensp pro'):
            sandbox = config.get('sandbox_id', 0) or 0
            node = config.get('node_id', 0) or 0
            host = '%s#s%sd%s' % (host, sandbox, node)
        return (config.get('protocol'), host, config.get('port'))

    def get_vault_devices(self) -> List[Dict[str, Any]]:
        """返回设备库里保存的设备列表（不含密码）"""
        return list(self.get("vault_devices") or [])

    def find_vault_device(self, config: Dict[str, Any]):
        """按 协议+主机+端口 在设备库里查找，命中返回该设备 dict，否则 None"""
        key = self._vault_key(config)
        for device in self.get_vault_devices():
            if self._vault_key(device) == key:
                return device
        return None

    def add_vault_device(self, config: Dict[str, Any]) -> bool:
        """
        把一台设备加入设备库（连接成功后调用）。

        按 协议+主机+端口 去重：已存在则更新名称/厂商等信息，不重复添加。
        当 config 有 remember=True 或全局 save_passwords=True 时，
        用 base64 混淆存储 password 和 key_filename，否则剥离敏感字段。
        返回是否新增了设备。
        """
        if not config:
            return False
        pure = self._sanitize_vault_config(config)
        pure.setdefault('name', pure.get('host') or pure.get('serial_port') or '设备')
        devices = self.get_vault_devices()
        key = self._vault_key(pure)
        for index, device in enumerate(devices):
            if self._vault_key(device) == key:
                # 已存在：合并更新，保留原有 vendor 等字段
                merged = dict(device)
                for k, v in pure.items():
                    merged[k] = v
                devices[index] = merged
                self._data["vault_devices"] = devices
                self.save()
                return False
        devices.append(pure)
        self._data["vault_devices"] = devices
        self.save()
        return True

    def _sanitize_vault_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        把一份连接配置处理成可存入 Vault 的版本：
        - remember=True 或全局 save_passwords=True 时，用 base64 混淆存密码和密钥文件
        - 否则剥离 password / key_filename / remember
        """
        want_password = bool(
            config.get('remember')
            or self.get('save_passwords', False)
        )
        pure = {k: v for k, v in config.items()
                if k not in ('password', 'key_filename', 'remember')}
        if want_password:
            pwd = config.get('password', '')
            if pwd and not pwd.startswith('b64:'):
                pwd = self.encode_password(pwd)
            if pwd:
                pure['password'] = pwd
            kf = config.get('key_filename', '')
            if kf:
                pure['key_filename'] = kf
            pure['remember'] = True
        return pure

    def update_vault_device(self, config: Dict[str, Any],
                            old_config: Dict[str, Any] = None) -> bool:
        """更新设备库里某台设备的信息（按 key 匹配）

        old_config：编辑**之前**的原记录。必须传 —— 用户把主机/端口改掉之后，
        key 就变了，用新 key 去匹配会找不到旧记录，于是变成"新增一条、旧的还在"
        （用户反馈：改完设备库里出现重复项）。
        """
        if not config:
            return False
        pure = self._sanitize_vault_config(config)
        devices = self.get_vault_devices()
        key = self._vault_key(old_config) if old_config else self._vault_key(pure)
        for index, device in enumerate(devices):
            if self._vault_key(device) == key:
                merged = dict(device)
                for k, v in pure.items():
                    merged[k] = v
                devices[index] = merged
                self._data["vault_devices"] = devices
                self.save()
                return True
        # 没匹配到则当作新增
        devices.append(pure)
        self._data["vault_devices"] = devices
        self.save()
        return True

    def remove_vault_device(self, config: Dict[str, Any]) -> bool:
        """从设备库移除一台设备（按 key 匹配）"""
        key = self._vault_key(config)
        devices = [d for d in self.get_vault_devices()
                   if self._vault_key(d) != key]
        if len(devices) != len(self.get_vault_devices()):
            self._data["vault_devices"] = devices
            self.save()
            return True
        return False

    # ------------------------------------------------------------ 会话恢复
    @property
    def restore_sessions(self) -> bool:
        return bool(self.get("restore_sessions", True))

    @property
    def save_passwords(self) -> bool:
        return bool(self.get("save_passwords", False))

    @property
    def auto_log(self) -> bool:
        return bool(self.get("auto_log", True))

    @auto_log.setter
    def auto_log(self, value: bool):
        self.set("auto_log", bool(value))

    @property
    def log_dir(self) -> str:
        """实际使用的日志目录"""
        from core.logger import DEFAULT_LOG_DIR
        return self.get("log_dir") or DEFAULT_LOG_DIR

    @property
    def batch_delay_ms(self) -> int:
        try:
            return int(self.get("batch_delay_ms", 500))
        except (TypeError, ValueError):
            return 500

    @property
    def batch_wait_mode(self) -> str:
        """批量下发等待方式: prompt=等提示符 / delay=固定间隔"""
        mode = (self.get("batch_wait_mode") or "prompt").lower()
        return mode if mode in ("prompt", "delay") else "prompt"

    @batch_wait_mode.setter
    def batch_wait_mode(self, value: str):
        self.set("batch_wait_mode", value)

    @property
    def batch_prompt_timeout(self) -> float:
        try:
            return float(self.get("batch_prompt_timeout", 30))
        except (TypeError, ValueError):
            return 30.0

    @property
    def batch_auto_page(self) -> bool:
        return bool(self.get("batch_auto_page", True))

    @property
    def session_auto_page(self) -> bool:
        """交互会话要不要自动应答分页标记（---- More ----）"""
        return bool(self.get("session_auto_page", True))

    @property
    def confirm_config_commands(self) -> bool:
        """
        配置类命令下发前是否需要人工确认

        默认开启：配置命令会改设备，必须先展示命令清单让人确认。
        """
        return bool(self.get("confirm_config_commands", True))

    @confirm_config_commands.setter
    def confirm_config_commands(self, value: bool):
        self.set("confirm_config_commands", bool(value))

    # ------------------------------------------------------------ 巡检参数
    @property
    def patrol_concurrency(self) -> int:
        try:
            return max(1, min(int(self.get("patrol_concurrency", 4)), 32))
        except (TypeError, ValueError):
            return 4

    @property
    def patrol_timeout(self) -> float:
        try:
            return float(self.get("patrol_timeout", 10))
        except (TypeError, ValueError):
            return 10.0

    @property
    def patrol_prompt_timeout(self) -> float:
        try:
            return float(self.get("patrol_prompt_timeout", 30))
        except (TypeError, ValueError):
            return 30.0

    def get_patrol_commands(self) -> list:
        commands = self.get("patrol_commands") or []
        return list(commands) if isinstance(commands, list) else []

    def set_patrol_commands(self, commands):
        self._data["patrol_commands"] = list(commands or [])

    def get_patrol_devices(self) -> list:
        devices = self.get("patrol_devices") or []
        return list(devices) if isinstance(devices, list) else []

    def set_patrol_devices(self, devices):
        self._data["patrol_devices"] = list(devices or [])

    @property
    def system_prompt(self) -> str:
        """用户自定义的系统提示词（空表示用内置）"""
        return (self.get("system_prompt") or "").strip()

    def get_last_sessions(self):
        """取出上次关闭时保存的会话列表"""
        sessions = self.get("last_sessions") or []
        return sessions if isinstance(sessions, list) else []

    def set_last_sessions(self, sessions):
        """保存会话列表（按需混淆密码）"""
        self._data["last_sessions"] = list(sessions or [])

    # ------------------------------------------------------------ 密码混淆
    @staticmethod
    def encode_password(password: str) -> str:
        """
        对密码做 base64 混淆

        注意: 这只是"不让人一眼看到明文"，不是加密，不能抵御有意读取。
        默认不保存密码，只有用户显式勾选"保存密码"时才会用到。
        """
        if not password:
            return ""
        try:
            import base64
            return "b64:" + base64.b64encode(password.encode('utf-8')).decode('ascii')
        except Exception:
            return ""

    @staticmethod
    def decode_password(value: str) -> str:
        """还原被混淆的密码"""
        if not value:
            return ""
        if not value.startswith("b64:"):
            return value
        try:
            import base64
            return base64.b64decode(value[4:].encode('ascii')).decode('utf-8')
        except Exception:
            return ""

    # ------------------------------------------------------------ 联网检索
    @property
    def search_backend(self) -> str:
        return (self.get("search_backend") or "auto").lower()

    @property
    def searxng_url(self) -> str:
        return self.get("searxng_url") or ""

    @property
    def search_max_results(self) -> int:
        try:
            return int(self.get("search_max_results", 5))
        except (TypeError, ValueError):
            return 5

    @property
    def search_read_pages(self) -> int:
        try:
            return int(self.get("search_read_pages", 2))
        except (TypeError, ValueError):
            return 2

    @property
    def search_page_chars(self) -> int:
        try:
            return int(self.get("search_page_chars", 4000))
        except (TypeError, ValueError):
            return 4000

    @property
    def ai_auto_search(self) -> bool:
        """是否允许 AI 自主发起联网检索"""
        return bool(self.get("ai_auto_search", True))

    # ------------------------------------------------------------ 本地资料库
    @property
    def knowledge_dir(self) -> str:
        """本地资料库目录（空表示用默认目录）"""
        from core.knowledge import DEFAULT_KNOWLEDGE_DIR
        return self.get("knowledge_dir") or DEFAULT_KNOWLEDGE_DIR

    @property
    def knowledge_enabled(self) -> bool:
        """查命令时是否检索本地资料"""
        return bool(self.get("knowledge_enabled", True))

    @knowledge_enabled.setter
    def knowledge_enabled(self, value: bool):
        self.set("knowledge_enabled", bool(value))

    @property
    def knowledge_top_k(self) -> int:
        try:
            return int(self.get("knowledge_top_k", 4))
        except (TypeError, ValueError):
            return 4

    @property
    def knowledge_max_chars(self) -> int:
        try:
            return int(self.get("knowledge_max_chars", 4000))
        except (TypeError, ValueError):
            return 4000

    # ------------------------------------------------------------ 串口默认值
    def serial_defaults(self) -> Dict[str, Any]:
        """串口默认参数（配置项缺失时回落到内置默认）"""
        merged = dict(SERIAL_DEFAULTS)
        stored = self.get("serial_defaults") or {}
        if isinstance(stored, dict):
            merged.update({k: v for k, v in stored.items() if v is not None})
        return merged

    def set_serial_defaults(self, values: Dict[str, Any]):
        """记住用户上次使用的串口参数"""
        merged = self.serial_defaults()
        merged.update(values or {})
        self._data["serial_defaults"] = merged
