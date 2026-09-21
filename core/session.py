"""
会话管理 - 管理多个设备连接会话
"""
import time
import threading
from typing import Dict, Optional, List, Tuple

from PyQt6.QtCore import QObject, pyqtSignal

from .ssh_connector import SSHConnector
from .telnet_connector import TelnetConnector
from .serial_connector import SerialConnector
from .ai_agent import AIAgent
from .logger import SessionLogger
from .safety import is_config_command
from .terminal import detect_vendor
from .web_search import WebSearchClient
from .knowledge import KnowledgeBase

try:
    from config.settings import Settings
except ImportError:  # 允许脱离项目根目录单独导入
    Settings = None


class SessionInfo:
    """会话信息数据类"""

    MAX_RECENT = 20000  # 保留给 AI 分析的最近输出字符数
    MAX_SNAPSHOTS = 10  # 每个会话最多保留的配置快照数

    def __init__(self, session_id: int, name: str, protocol: str):
        self.session_id = session_id
        self.name = name
        self.protocol = protocol
        self.connector = None
        self.ai_agent = None
        self.ai_enabled = False
        self.ai_task = ""
        self.ai_auto = False          # 自动模式：AI 生成命令后直接下发
        self.ai_busy = False          # 防止 AI 步骤重入
        self.config: dict = {}        # 原始连接参数，便于保存/重连
        self.recent_output = ""       # 最近输出（供 AI 分析）
        self.logger = None            # 会话日志写入器
        self.snapshots = []           # 配置快照 [(标签, 文本, 时间戳)]
        self.vendor = "unknown"       # 识别出的设备厂商
        self.capture_buffer = None    # 采集模式下的输出暂存区
        self.created_at = time.time()
        self.last_activity = time.time()

    # ------------------------------------------------------------ 输出记录
    def record_output(self, text: str):
        """记录设备输出，仅保留最近 MAX_RECENT 个字符，并写入日志"""
        if not text:
            return
        self.recent_output += text
        if len(self.recent_output) > self.MAX_RECENT:
            self.recent_output = self.recent_output[-self.MAX_RECENT:]
        self.last_activity = time.time()
        if self.capture_buffer is not None:
            self.capture_buffer.append(text)
        if self.logger:
            self.logger.write(text)

    # ------------------------------------------------------------ 输出采集
    def start_capture(self):
        """开始采集输出（用于配置快照等场景）"""
        self.capture_buffer = []

    def capture_size(self) -> int:
        """返回当前采集到的字符数"""
        if self.capture_buffer is None:
            return 0
        return sum(len(t) for t in self.capture_buffer)

    def stop_capture(self) -> str:
        """结束采集并返回采集到的内容"""
        if self.capture_buffer is None:
            return ""
        text = "".join(self.capture_buffer)
        self.capture_buffer = None
        return text

    def consume_output(self) -> str:
        """取出并清空最近输出（供 AI 分析使用）"""
        data = self.recent_output
        self.recent_output = ""
        return data

    def peek_output(self) -> str:
        """查看最近输出但不清空"""
        return self.recent_output

    # ------------------------------------------------------------ 配置快照
    def add_snapshot(self, label: str, text: str):
        """保存一份配置快照"""
        self.snapshots.append((label, text, time.time()))
        if len(self.snapshots) > self.MAX_SNAPSHOTS:
            self.snapshots = self.snapshots[-self.MAX_SNAPSHOTS:]

    def last_snapshot(self):
        """返回最近一份快照 (标签, 文本, 时间戳)，无则 None"""
        return self.snapshots[-1] if self.snapshots else None

    def previous_snapshot(self):
        """返回倒数第二份快照，无则 None"""
        return self.snapshots[-2] if len(self.snapshots) >= 2 else None

    # ------------------------------------------------------------ 日志
    def open_log(self, log_dir: str = None) -> bool:
        """开启日志记录"""
        if self.logger is None:
            self.logger = SessionLogger(
                self.name, self.config.get('host', ''), log_dir=log_dir
            )
        return self.logger.open()

    def close_log(self):
        """关闭日志记录"""
        if self.logger:
            self.logger.close()


class SessionManager(QObject):
    """
    会话管理器

    管理多个设备连接会话
    支持创建、切换、关闭会话
    """

    # 信号
    session_created = pyqtSignal(int, str)      # session_id, name
    session_closed = pyqtSignal(int)             # session_id
    session_activated = pyqtSignal(int)          # session_id
    session_error = pyqtSignal(int, str)         # session_id, error
    ai_output = pyqtSignal(int, str)             # session_id, text   AI 面板消息
    ai_command = pyqtSignal(int, str)            # session_id, command  AI 下发的命令
    ai_step_done = pyqtSignal(int)               # session_id 一轮 AI 分析结束
    ai_research_ready = pyqtSignal(int, str, list)  # session_id, 回答, 来源列表
    connection_info = pyqtSignal(int, str)       # session_id, 提示信息（重连等）

    def __init__(self):
        super().__init__()
        self._sessions: Dict[int, SessionInfo] = {}
        self._next_id = 1
        self._active_session_id: Optional[int] = None
        self._ai_api_key = ""
        self._ai_model = ""
        self._ai_provider = ""
        self._ai_base_url = ""
        self._ai_lock = threading.Lock()
        self.settings = Settings() if Settings else None
        self._search_client = None
        self._knowledge = None

    def _log_dir(self):
        return self.settings.log_dir if self.settings else None

    # ------------------------------------------------------------ 本地资料库
    def _ensure_knowledge(self):
        """构建（或复用）本地资料库实例"""
        if not self.settings:
            return None
        if self._knowledge is None:
            self._knowledge = KnowledgeBase(
                root_dir=self.settings.knowledge_dir,
                enabled=self.settings.knowledge_enabled,
            )
        return self._knowledge

    @property
    def knowledge_base(self):
        """供界面使用的资料库实例"""
        return self._ensure_knowledge()

    def refresh_knowledge(self, rebuild: bool = False):
        """
        资料库目录/开关变化后刷新

        参数:
            rebuild: 是否立即重建索引（导入文件后建议 True）
        """
        kb = self._ensure_knowledge()
        if kb is None:
            return None
        kb.set_root(self.settings.knowledge_dir)
        kb.set_enabled(self.settings.knowledge_enabled)

        if rebuild:
            kb.build_index()

        # 同步到已启用 AI 的会话
        for session in self._sessions.values():
            if not session.ai_agent:
                continue
            if self.settings.knowledge_enabled:
                session.ai_agent.set_knowledge_base(kb)
                session.ai_agent.configure_knowledge(
                    self.settings.knowledge_top_k, self.settings.knowledge_max_chars
                )
            else:
                session.ai_agent.set_knowledge_base(None)
        return kb

    # ------------------------------------------------------------ 联网检索
    def _ensure_search_client(self):
        """按当前设置构建（或复用）检索客户端"""
        if not self.settings:
            return None
        backend = (self.settings.search_backend or "auto").lower()
        if backend in ("off", "none", "disable", "disabled"):
            return None
        if self._search_client is None:
            self._search_client = WebSearchClient(
                backend=backend,
                searxng_url=self.settings.searxng_url,
                max_results=self.settings.search_max_results,
            )
        return self._search_client

    def refresh_search_client(self):
        """设置变化后重建检索客户端（同时刷新本地资料库）"""
        self._search_client = None
        client = self._ensure_search_client()
        knowledge = self._ensure_knowledge() if (
            self.settings and self.settings.knowledge_enabled) else None
        for session in self._sessions.values():
            if not session.ai_agent:
                continue
            if client is not None and self.settings.ai_auto_search:
                session.ai_agent.set_search_client(client)
                session.ai_agent.configure_search(
                    self.settings.search_read_pages,
                    self.settings.search_page_chars,
                    self.settings.search_max_results,
                )
            else:
                session.ai_agent.set_search_client(None)
            session.ai_agent.set_knowledge_base(knowledge)
            session.ai_agent.configure_knowledge(
                self.settings.knowledge_top_k, self.settings.knowledge_max_chars
            )
        return client

    # ------------------------------------------------------------ 会话生命周期
    def create_session(self, name: str, protocol: str,
                       host: str = "", username: str = "",
                       password: str = "", port: int = 22,
                       serial_port: str = "", baudrate: int = 9600,
                       **kwargs) -> int:
        """
        创建新会话

        参数:
            name: 会话名称
            protocol: 协议类型 (SSH/Telnet/Serial)
            host: 主机地址
            username: 用户名
            password: 密码
            port: 端口号
            serial_port: 串口号
            baudrate: 波特率
        返回:
            会话ID
        """
        session_id = self._next_id
        self._next_id += 1

        session = SessionInfo(session_id, name, protocol)
        session.config = {
            'name': name, 'protocol': protocol, 'host': host,
            'username': username, 'password': password, 'port': port,
            'serial_port': serial_port, 'baudrate': baudrate,
        }

        # 创建连接器
        if protocol == "SSH":
            session.connector = SSHConnector()
            success = session.connector.connect(
                host=host, username=username,
                password=password, port=port,
                key_filename=kwargs.get('key_filename'),
                timeout=kwargs.get('timeout', 10)
            )
        elif protocol == "Telnet":
            session.connector = TelnetConnector()
            success = session.connector.connect(
                host=host, username=username,
                password=password, port=port
            )
        elif protocol == "Serial":
            serial_opts = {
                'bytesize': kwargs.get('bytesize', 8),
                'parity': kwargs.get('parity', 'N'),
                'stopbits': kwargs.get('stopbits', 1),
                'flow': kwargs.get('flow', 'none'),
                'dtr': kwargs.get('dtr'),
                'rts': kwargs.get('rts'),
                'line_ending': kwargs.get('line_ending', '\r'),
                'char_delay_ms': kwargs.get('char_delay_ms', 0),
                'auto_reconnect': kwargs.get('auto_reconnect', True),
                'reconnect_interval': kwargs.get('reconnect_interval', 2.0),
                'max_reconnect': kwargs.get('max_reconnect', 0),
            }
            session.config.update(serial_opts)
            session.config['local_echo'] = bool(kwargs.get('local_echo', False))
            session.connector = SerialConnector()
            success = session.connector.connect(
                port=serial_port, baudrate=baudrate, **serial_opts
            )
        else:
            self.session_error.emit(session_id, f"不支持的协议: {protocol}")
            return -1

        if success:
            self._sessions[session_id] = session
            # 行结束符（Console 场景常用 CR）
            ending = kwargs.get('line_ending')
            if ending is not None:
                try:
                    session.connector.set_line_ending(ending)
                except Exception:
                    pass
            # 自动记录日志（可在"会话"菜单里开关）
            if self.settings and self.settings.auto_log:
                session.open_log(self._log_dir())
            # 记录输出供 AI 分析与配置快照使用。
            # 这里用非 Qt 的输出监听器而不是 output_received 信号：
            # 信号连到普通函数时是"排队投递"的，必须靠事件循环才能送达，
            # 会滞后甚至（无事件循环时）完全收不到；监听器在读线程里同步回调，最可靠。
            session.connector.add_output_listener(
                lambda text, sid=session_id: self._on_output(sid, text)
            )
            session.connector.error_occurred.connect(
                lambda msg, sid=session_id: self.session_error.emit(sid, msg)
            )
            # 串口自动重连的进度提示
            if hasattr(session.connector, 'reconnecting'):
                session.connector.reconnecting.connect(
                    lambda n, sid=session_id:
                        self.connection_info.emit(sid, f"串口重连中（第 {n} 次尝试）…")
                )
                session.connector.reconnected.connect(
                    lambda sid=session_id:
                        self.connection_info.emit(sid, "串口已自动重连成功")
                )
                session.connector.reconnect_failed.connect(
                    lambda msg, sid=session_id: self.session_error.emit(sid, msg)
                )
            self.session_created.emit(session_id, name)
            return session_id
        else:
            self.session_error.emit(session_id, "连接失败")
            return -1

    def close_session(self, session_id: int):
        """关闭会话"""
        if session_id in self._sessions:
            session = self._sessions[session_id]
            session.close_log()
            if session.connector:
                try:
                    session.connector.disconnect()
                except Exception:
                    pass
            del self._sessions[session_id]
            if self._active_session_id == session_id:
                self._active_session_id = None
            self.session_closed.emit(session_id)

    def get_session(self, session_id: int) -> Optional[SessionInfo]:
        """获取会话信息"""
        return self._sessions.get(session_id)

    def get_connector(self, session_id: int):
        """获取会话的连接器"""
        session = self._sessions.get(session_id)
        return session.connector if session else None

    def get_all_sessions(self) -> Dict[int, SessionInfo]:
        """获取所有会话"""
        return self._sessions.copy()

    def get_active_session_id(self) -> Optional[int]:
        """获取当前活动会话ID"""
        return self._active_session_id

    def set_active_session(self, session_id: int):
        """设置当前活动会话"""
        if session_id in self._sessions:
            self._active_session_id = session_id
            self.session_activated.emit(session_id)

    def cleanup(self):
        """清理所有会话"""
        for session_id in list(self._sessions.keys()):
            self.close_session(session_id)

    # ------------------------------------------------------------ 会话导出
    def export_sessions(self, include_password: bool = False):
        """
        导出当前会话列表（用于下次启动恢复）

        参数:
            include_password: 是否包含密码字段（调用方自行决定是否混淆）
        """
        result = []
        for session in self._sessions.values():
            cfg = dict(session.config)
            cfg['name'] = session.name
            if not include_password:
                cfg.pop('password', None)
            result.append(cfg)
        return result

    def set_auto_log(self, enabled: bool):
        """运行时开关自动日志：已开会话立即生效"""
        for session in self._sessions.values():
            if enabled and not (session.logger and session.logger.is_open):
                session.open_log(self._log_dir())
            elif not enabled:
                session.close_log()

    # ------------------------------------------------------------ 命令下发
    def send_command(self, session_id: int, command: str) -> bool:
        """向指定会话下发一条命令"""
        session = self._sessions.get(session_id)
        if not session or not session.connector:
            return False
        if not session.connector.is_connected():
            self.session_error.emit(session_id, "连接已断开，无法发送命令")
            return False
        session.connector.send(command)
        session.last_activity = time.time()
        if session.logger:
            session.logger.mark(f"用户下发: {command}")
        return True

    # ------------------------------------------------------------ AI 集成
    def _on_output(self, session_id: int, text: str):
        session = self._sessions.get(session_id)
        if not session:
            return
        session.record_output(text)
        # 首次识别出厂商后固定下来，供 AI 使用正确语法
        if session.vendor == "unknown":
            vendor = detect_vendor(session.recent_output)
            if vendor != "unknown":
                session.vendor = vendor
                if session.ai_agent:
                    session.ai_agent.set_vendor(vendor)

    def set_ai_credentials(self, api_key: str, model: str = "",
                           provider: str = "", base_url: str = ""):
        """设置全局 AI 凭据，之后启用 AI 的会话自动继承"""
        self._ai_api_key = api_key
        self._ai_model = model
        if provider:
            self._ai_provider = provider
        if base_url:
            self._ai_base_url = base_url

    def enable_ai(self, session_id: int, api_key: str = "", task: str = "",
                  model: str = None, auto: bool = False,
                  provider: str = "", base_url: str = ""):
        """为会话启用AI（按设置自动挂上联网检索能力）"""
        session = self._sessions.get(session_id)
        if not session:
            return
        system_prompt = self.settings.system_prompt if self.settings else ""

        # 提供方 / 接口地址 / 模型：优先用调用方传的，否则取设置
        provider = provider or self._ai_provider or (
            self.settings.ai_provider if self.settings else "anthropic")
        base_url = base_url or self._ai_base_url or (
            self.settings.api_base if self.settings else "")
        model = model or self._ai_model or (
            self.settings.get("model") if self.settings else None)
        # 密钥：调用方传入 → 全局凭据 → 设置里该提供方的密钥
        # （最后这层兜底很重要：界面之外的调用不会主动传密钥）
        key = api_key or self._ai_api_key or (
            self.settings.get_api_key_for(provider) if self.settings else "")

        # 是否允许 AI 自主联网检索
        search_client = None
        if self.settings and self.settings.ai_auto_search:
            search_client = self._ensure_search_client()
        # 本地资料库（默认启用）
        knowledge = None
        if self.settings and self.settings.knowledge_enabled:
            knowledge = self._ensure_knowledge()

        session.ai_agent = AIAgent(
            key, model, system_prompt=system_prompt, search_client=search_client,
            knowledge_base=knowledge, provider=provider, base_url=base_url
        )
        if search_client is not None:
            session.ai_agent.configure_search(
                self.settings.search_read_pages,
                self.settings.search_page_chars,
                self.settings.search_max_results,
            )
        if knowledge is not None:
            session.ai_agent.configure_knowledge(
                self.settings.knowledge_top_k, self.settings.knowledge_max_chars
            )
        # 把检索结果转发给主窗口
        session.ai_agent.research_ready.connect(
            lambda answer, sources, sid=session_id:
                self.ai_research_ready.emit(sid, answer, sources)
        )
        if session.vendor and session.vendor != "unknown":
            session.ai_agent.set_vendor(session.vendor)
        session.ai_enabled = True
        session.ai_task = task
        session.ai_auto = auto
        if task:
            session.ai_agent.set_task(task)

    def disable_ai(self, session_id: int):
        """关闭会话AI"""
        session = self._sessions.get(session_id)
        if session:
            session.ai_enabled = False
            session.ai_auto = False

    def is_ai_busy(self, session_id: int) -> bool:
        session = self._sessions.get(session_id)
        return bool(session and session.ai_busy)

    def ai_step(self, session_id: int, auto_execute: bool = False):
        """
        执行一轮 AI 分析（在后台线程中调用）

        参数:
            session_id: 会话ID
            auto_execute: 是否自动下发生成的命令
        返回:
            (命令列表, 被拦截的命令列表, 状态)
            状态取值: ok=生成了命令 / done=任务完成或需人工确认 /
                      skip=无新输出或正在忙 / stopped=连接已断开
        """
        session = self._sessions.get(session_id)
        if not session or not session.ai_enabled or not session.ai_agent:
            return [], [], "skip"
        if not session.connector or not session.connector.is_connected():
            self.ai_output.emit(session_id, "[系统] 连接已断开，AI 已停止")
            session.ai_enabled = False
            session.ai_auto = False
            return [], [], "stopped"

        with self._ai_lock:
            if session.ai_busy:
                return [], [], "skip"
            session.ai_busy = True
        try:
            output = session.consume_output()
            if not output.strip():
                return [], [], "skip"

            reply = session.ai_agent.analyze(output, session.ai_task)

            # AI 主动请求检索资料时，先本地资料库再联网，最多两轮
            search_hops = 0
            while reply and search_hops < 2:
                query = session.ai_agent.parse_search_request(reply)
                if not query:
                    break
                search_hops += 1
                self.ai_output.emit(session_id, f"[检索] 正在查找资料: {query}")
                context = session.ai_agent.lookup_context(query)
                reply = session.ai_agent.analyze(context, session.ai_task)

            if not reply:
                self.ai_output.emit(
                    session_id,
                    "[AI] 未生成新命令（任务完成或需要人工确认）"
                )
                return [], [], "done"

            commands, blocked = session.ai_agent.filter_commands(reply)
            for cmd in blocked:
                self.ai_output.emit(session_id, f"[安全拦截] {cmd}（危险命令，需人工确认）")

            # 安全闸门：配置类命令一律不自动下发，转入待执行队列等人工确认
            config_commands = [c for c in commands if is_config_command(c)]
            if auto_execute and config_commands:
                for cmd in commands:
                    self.ai_command.emit(session_id, cmd)
                self.ai_output.emit(
                    session_id,
                    f"[安全] 检测到 {len(config_commands)} 条配置类命令，"
                    "已放入「待执行」队列等待你确认，自动模式已暂停。\n"
                    "　　　配置命令必须人工核对后再下发（这是程序默认行为）。"
                )
                return commands, blocked, "queued"

            for cmd in commands:
                self.ai_command.emit(session_id, cmd)
                if auto_execute:
                    session.connector.send(cmd)
                    time.sleep(0.5)
            return commands, blocked, "ok"
        finally:
            session.ai_busy = False
            self.ai_step_done.emit(session_id)

    def ai_analyze_once(self, session_id: int, task: str = "") -> str:
        """手动触发一次分析，返回 AI 回复（后台线程中调用）"""
        session = self._sessions.get(session_id)
        if not session or not session.ai_agent:
            return ""
        output = session.consume_output() or session.peek_output()
        if task:
            session.ai_task = task
            session.ai_agent.set_task(task)

        reply = session.ai_agent.analyze(output, session.ai_task)
        hops = 0
        while reply and hops < 2:
            query = session.ai_agent.parse_search_request(reply)
            if not query:
                break
            hops += 1
            self.ai_output.emit(session_id, f"[检索] 正在查找资料: {query}")
            reply = session.ai_agent.analyze(
                session.ai_agent.lookup_context(query), session.ai_task)
        return reply

    def ai_explain(self, session_id: int, question: str = "") -> str:
        """让 AI 用自然语言解释当前输出"""
        session = self._sessions.get(session_id)
        if not session or not session.ai_agent:
            return ""
        output = session.peek_output() or session.consume_output()
        return session.ai_agent.explain(output, question)

    def ai_research(self, session_id: int, question: str) -> str:
        """
        联网检索并回答问题（供 AI 面板的"联网查询"使用）

        回答通过 ai_research_ready 信号回传，便于界面渲染来源列表。
        """
        session = self._sessions.get(session_id)
        if not session or not session.ai_agent:
            return ""
        return session.ai_agent.research(question, session.peek_output())
