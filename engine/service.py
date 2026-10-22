"""引擎的方法实现：把界面的 JSON-RPC 请求翻译成 core/ 里的调用。

三条实测得来的硬约束，决定了下面所有写法的形态：

1. baseConnector 继承 QObject → 必须能 import PyQt6，但不需要 QApplication。
   所以所有 core 导入都做成**惰性**的，import 失败时让 engine.hello 仍能回答，
   界面才能把"引擎起来了但 core 不可用"这个事实显示给用户。
2. get_output() 是读后清空的破坏性读取 → 引擎必须是唯一消费者。
   界面永远不轮询，只收通知。好处是走的是 UI 那条已验证过的路径。
3. create_session / send_command 都是阻塞的（Telnet 登录最多四段等待）→
   一律丢到工作线程，请求本身立刻返回 accepted。
"""

import os
import threading
import time

from engine.rpc import Emitter, log

# core 只认大小写敏感的这三个词，界面上传进来的写法五花八门，这里统一收口
_PROTOCOLS = {
    "ssh": "SSH",
    "telnet": "Telnet",
    "serial": "Serial",
    "console": "Serial",
    "com": "Serial",
    # eNSP Pro 仿真平台：设备 Console 走 HTTP + STOMP，不是 TCP，所以单列一条协议
    "ensp": "eNSP",
    "ensp-pro": "eNSP",
    "ensppro": "eNSP",
    "ensp pro": "eNSP",
}

# Serial 专有参数：只有这些才允许透传给 create_session，
# 免得传进它不认识的键、在 connect() 里炸 TypeError
_SERIAL_KEYS = (
    "bytesize", "parity", "stopbits", "flow", "dtr", "rts",
    "line_ending", "char_delay_ms", "auto_reconnect",
    "reconnect_interval", "max_reconnect", "local_echo",
)


def _normalize_protocol(value: str) -> str:
    key = (value or "ssh").strip().lower()
    if key not in _PROTOCOLS:
        raise ValueError("不支持的协议: %s" % value)
    return _PROTOCOLS[key]


class _SessionHandle:
    """界面 key ↔ SessionManager 数字 id 的映射，外加轮询用的状态位。"""

    __slots__ = ("key", "sid", "host", "name", "connected", "state")

    def __init__(self, key: str, sid: int, host: dict, name: str):
        self.key = key
        self.sid = sid
        self.host = host
        self.name = name
        self.connected = False
        self.state = "connecting"


class EngineService:
    """所有 RPC 方法的实现与注册表。"""

    #: 轮询间隔。和原 PyQt6 界面的 QTimer 30ms 保持一致 —— 肉眼无感，
    #: 又足以让进度条/\r 覆盖看起来是连续的。
    POLL_INTERVAL = 0.03

    #: 会**阻塞十几秒**的方法 —— 交给 JsonRpcServer 走工作线程派发。
    #:
    #: 为什么必须有这张表：`engine/rpc.py` 的读循环是单线程的，同步派发时
    #: 一个阻塞 handler 会把**后面所有请求**一起卡住。实测（2026-10-05 Eve-NG）：
    #: 探针 t=16s 发起 `ai.ask`（内部 `client.chat()` 是阻塞 HTTP），t=26s 请求
    #: `usage.list`，引擎到 t=35s 才处理 —— 界面「使用总结」于是渲染出构造时的
    #: 空快照，用户看到的是"记录丢了"。
    #:
    #: ⚠ **绝不要**把 `session.write` / `session.command` 放进来：
    #:   敲命令必须保持严格时序，异步化会让两条命令的发送顺序不再确定。
    SLOW_METHODS = (
        "ai.ask",             # 阻塞 HTTP 等大模型返回（实测 0.6s ~ 20s）
        "ai.optimize",        # 同类 HTTP：优化提示词（AiPanel「优化提示词」按钮）
        "ai.test",            # 同类 HTTP 连通性测试
        "usage.summarize",    # 顶部一句话归纳，同样走 AI
        "patrol.run",         # 多设备巡检，几十秒
        "knowledge.index",    # 重建检索索引
        "knowledge.import",   # 导入文档
    )

    def __init__(self, emitter: Emitter):
        self._emit = emitter
        self._lock = threading.RLock()
        self._sessions = {}          # key -> _SessionHandle
        self._manager = None
        self._settings = None
        self._boot_error = None
        self._stop = threading.Event()

        self.methods = {
            "engine.hello": self.m_hello,
            "engine.status": self.m_hello,
            "hosts.list": self.m_hosts_list,
            "hosts.save": self.m_hosts_save,
            "hosts.delete": self.m_hosts_delete,
            "safety.check": self.m_safety_check,
            "session.open": self.m_session_open,
            "session.write": self.m_session_write,
            "session.command": self.m_session_command,
            "session.close": self.m_session_close,
            "session.resize": self.m_session_resize,
            "session.list": self.m_session_list,
            # 串口（Console）：枚举端口（带 VID/PID/描述，供界面"USB 优先"自动选口）
            # + 记住上次成功连过的端口 + 报"本程序当前正占用的口"。
            # 全部是确定性事实，不做任何 AI 推断。
            "serial.ports": self.m_serial_ports,
            "serial.remember": self.m_serial_remember,
            # 使用总结（本机记录，确定性归类；只存本机、不上传）
            "usage.state": self.m_usage_state,
            # 使用总结的展示文案（S4 DTO）：新 UI 直接渲染，不必自己拼分页名/表头
            "usage.labels": self.m_usage_labels,
            "usage.list": self.m_usage_list,
            "usage.setMonitor": self.m_usage_set_monitor,
            "usage.mark": self.m_usage_mark,
            "usage.merge": self.m_usage_merge,
            "usage.split": self.m_usage_split,
            "usage.clear": self.m_usage_clear,
            "usage.candidate": self.m_usage_candidate,
            "usage.summarize": self.m_usage_summarize,
            # A1：切换生效的 AI 提供方（"检测到 X 已配置 → 切换并重试"用）
            "ai.setProvider": self.m_ai_set_provider,
            # 资料库：后端 core/knowledge.py 已完整实现并验证过
            # （26,286 篇华为文档、检索 3/3、AI 接地不幻觉），
            # 这里只是把它暴露给 C++ 界面。
            "knowledge.stats": self.m_knowledge_stats,
            "knowledge.list": self.m_knowledge_list,
            "knowledge.search": self.m_knowledge_search,
            "knowledge.index": self.m_knowledge_index,
            "knowledge.import": self.m_knowledge_import,
            "knowledge.delete": self.m_knowledge_delete,
            # AI 助手：core/llm.py 已验证可用（DeepSeek，0.6s 响应），
            # 并且能跟资料库组合成 RAG —— 提问先检索本地文档再接地作答。
            "ai.status": self.m_ai_status,
            "ai.ask": self.m_ai_ask,
            # 优化提示词：把输入框里的问题交给模型改写得更清晰具体（只改写不回答）。
            # AiPanel 的「优化提示词」按钮走这条；同样不查资料库。
            "ai.optimize": self.m_ai_optimize,
            # 多设备巡检：core/patrol.py 已在真机验证（eNSP + Telnet 并发 2/2）
            "patrol.run": self.m_patrol_run,
            # AI 设置：提供方 / 密钥 / 模型 / 接口地址。
            # 没有这组方法时，AI 面板只能提示"请去工具→AI 设置"，
            # 而那个入口并不存在 —— 整条 AI 链路对用户是断的。
            "ai.getSettings": self.m_ai_get_settings,
            "ai.setSettings": self.m_ai_set_settings,
            "ai.test": self.m_ai_test,
            # 配置差异比对：core/diff.py 是纯函数，无外部依赖
            "diff.compare": self.m_diff_compare,
            # 命令自动补全：三层（token / next / sequences），
            # 数据是 _vendor_cmd/{h3c,huawei,ruijie}/commands.jsonl，
            # core/command_suggest.py 里懒加载 + 缓存，单次查询毫秒级。
            "commands.suggest": self.m_commands_suggest,
            # Ctrl+点击命令 → 知识库：把一段文本（光标下的 token / 整行）定位到
            # **一条命令**，连同语法/视图/特性/例子与厂商技术解释（没有就是空状态）。
            # 匹配复用 core/command_suggest.py 的索引；讲解读 core/vendor_knowledge.py
            # （批次三建的表 + chapters/*.md + index.json），**全程无 AI**。
            "knowledge.command": self.m_knowledge_command,
            # **技术解释**（按"技术/命令名"检索，不要求正好是一条命令）。
            # 数据是 knowledge/<vendor>/tech/*.md（tools/build_tech_docs.py 用代码从
            # 厂商原始文档提取的正文）+ tech/ai/*.md（AI 整理稿，物理隔离）。
            # 检索全程**零 AI**：命中就返回，命中不了返回 need_ai=True，
            # 由调用方决定要不要 `useAI=1` 上模型（上完会把结果沉淀回 tech/ai/，
            # 下次就变成代码命中）。详见 core/tech_knowledge.py。
            "knowledge.explain": self.m_knowledge_explain,
            # 终端文本语义分类（prompt/ok/iface/neutral）。
            # ⚠ 供**未来的非 C++ UI** 复用同一口径；**不进渲染路径** ——
            #    现行 C++ 界面在进程内调用 core/TextClassify，无 RPC 往返。
            "text.classify": self.m_text_classify,
        }

        self._poller = threading.Thread(target=self._poll_loop,
                                        name="engine-poll", daemon=True)
        self._poller.start()

    # ------------------------------------------------------------ 分发
    def dispatch(self, method: str, params: dict):
        handler = self.methods.get(method)
        if handler is None:
            raise KeyError(method)
        return handler(params or {})

    def shutdown(self):
        self._stop.set()
        with self._lock:
            keys = list(self._sessions.keys())
        for key in keys:
            try:
                self._close(key)
            except Exception:
                pass

    # ------------------------------------------------------------ 惰性初始化
    def _make_settings(self):
        """构造（或复用）配置对象。

        支持 AIT_CONFIG_FILE 指定另一份配置文件，用于隔离测试或多套环境切换。
        注意 Settings 是 __new__ 单例，**谁先构造谁决定路径**，所以这里必须
        先于 SessionManager() 建立它 —— SessionManager 内部那句 Settings()
        随后只会拿到同一个实例。
        """
        from config.settings import Settings                # noqa: WPS433
        override = os.environ.get("AIT_CONFIG_FILE") or ""
        if override:
            log("info", "使用自定义配置文件: %s" % override)
            return Settings(override)
        return Settings()

    def _ensure_manager(self):
        """按需导入并构造 SessionManager。导入失败会抛出，由调用方转成错误响应。"""
        with self._lock:
            if self._manager is not None:
                return self._manager
            from core.session import SessionManager       # noqa: WPS433

            self._settings = self._make_settings()
            self._manager = SessionManager()
            # SessionManager 自己会建一个 Settings 单例；显式覆盖一次，
            # 保证引擎和界面看到的是同一份配置对象
            try:
                self._manager.settings = self._settings
            except Exception:
                pass
            log("info", "core 加载成功，配置目录: %s"
                % getattr(self._settings, "config_file", "?"))
            return self._manager

    def _trace(self, event: str, **fields) -> None:
        """四层追踪（AIT_USAGE_TRACE=<文件> 时才写；零开销、零副作用）。"""
        path = os.environ.get("AIT_USAGE_TRACE") or ""
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                extra = " ".join("%s=%s" % (k, v) for k, v in fields.items())
                fh.write("%s [L2-rpc] %s %s\n"
                         % (time.strftime("%H:%M:%S"), event, extra))
        except OSError:
            pass

    def _usage(self):
        """使用总结记录器（进程内单例）。monitor 开关持久化在配置里（默认关）。

        ⚠ 必须加锁：`ai.ask` 现在跑在工作线程上（见 SLOW_METHODS），它会调
        `note_question` → `_usage()`；同时轮询线程在调 `note_output`。两个线程
        同时看到 `_usage_log is None` 会各建一个实例，于是"记进去的"和"查出来的"
        不是同一个对象 —— 表现为记录凭空消失。`self._lock` 是 RLock，
        与 `_settings_obj()` 的嵌套获取不冲突。
        """
        with self._lock:
            if getattr(self, "_usage_log", None) is None:
                from core.usage_log import UsageLog       # noqa: WPS433
                monitor = False
                try:
                    monitor = bool(self._settings_obj().get("usage_monitor", False))
                except Exception:                          # noqa: BLE001
                    monitor = False
                self._usage_log = UsageLog(monitor=monitor)
            return self._usage_log

    def _settings_obj(self):
        with self._lock:
            if self._settings is None:
                self._settings = self._make_settings()
            return self._settings

    def _session_obj(self, sid: int):
        """拿到 SessionManager 内部的 Session 对象。

        不用公开 API 是因为 SessionManager 没有暴露 get_session()，
        而 _sessions 就是它的存储本体（只读访问，不修改）。
        """
        sessions = getattr(self._manager, "_sessions", None)
        if isinstance(sessions, dict):
            return sessions.get(sid)
        getter = getattr(self._manager, "get_session", None)
        if callable(getter):
            try:
                return getter(sid)
            except Exception:
                return None
        return None

    # ------------------------------------------------------------ 引擎元信息
    def m_hello(self, params: dict) -> dict:
        import platform
        import sys

        info = {
            "name": "NetOpsTerminal Engine",
            "version": "0.8.0",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "protocols": ["ssh", "telnet", "serial", "ensp"],
            "core_ready": False,
            "core_error": "",
        }
        try:
            self._ensure_manager()
            info["core_ready"] = True
        except Exception as exc:
            info["core_error"] = "%s: %s" % (type(exc).__name__, exc)
            self._boot_error = info["core_error"]
        return info

    # ------------------------------------------------------------ 设备库
    def m_hosts_list(self, params: dict) -> dict:
        settings = self._settings_obj()
        return {"hosts": list(settings.get_vault_devices() or [])}

    def m_hosts_save(self, params: dict) -> dict:
        settings = self._settings_obj()
        host = dict(params.get("host") or {})
        if not host:
            raise TypeError("host 不能为空")

        # 编辑场景下地址可能变了，去重键随之改变：先按旧键删掉，再按新键写入。
        # add_vault_device 对已存在的键是「合并更新」语义，正好当 upsert 用。
        original = params.get("original")
        if isinstance(original, dict) and original:
            try:
                settings.remove_vault_device(original)
            except Exception:
                pass
        try:
            settings.update_vault_device(host)
        except Exception:
            pass
        settings.add_vault_device(host)
        return {"hosts": list(settings.get_vault_devices() or [])}

    def m_hosts_delete(self, params: dict) -> dict:
        settings = self._settings_obj()
        host = dict(params.get("host") or {})
        if not host:
            raise TypeError("host 不能为空")
        settings.remove_vault_device(host)
        return {"hosts": list(settings.get_vault_devices() or [])}

    # ------------------------------------------------------------ 安全闸门
    def m_safety_check(self, params: dict) -> dict:
        command = str(params.get("command") or "")
        result = {"command": command, "read_only": True, "config": False,
                  "notes": [], "describe": ""}
        if not command.strip():
            return result
        try:
            from core import safety
            commands = [command]
            result["read_only"] = bool(safety.is_read_only(command))
            result["config"] = bool(safety.has_config_command(commands))
            result["notes"] = list(safety.risk_notes(commands) or [])
            result["describe"] = safety.describe(commands) or ""
        except Exception as exc:
            log("warn", "安全闸门不可用: %s" % exc)
        # ---- S5 决策 DTO（只加不改）：新 UI 只渲染，不必自己判断放行/确认，也不必自写文案
        _ro = bool(result.get("read_only", True))
        result["decision"] = "allow" if _ro else "confirm"      # 预留 "block"
        result["reason_fallback"] = "该命令会修改设备配置。"
        result["confirm_title"] = "安全闸门 · 需要确认"
        result["confirm_body_template"] = "即将下发配置类命令：\n\n%1\n\n%2"
        result["blocked_note_template"] = "已拦截配置命令：%1"
        result["status_note"] = "安全闸门拦下一条配置命令"

        return result

    # ------------------------------------------------------------ 会话
    def m_session_open(self, params: dict) -> dict:
        key = str(params.get("key") or "").strip()
        if not key:
            raise TypeError("缺少 key")
        host = dict(params.get("host") or {})
        if not host:
            raise TypeError("缺少 host")

        with self._lock:
            existing = self._sessions.get(key)
        if existing is not None:
            return {"accepted": True, "sid": existing.sid, "reused": True}

        password = params.get("password") or ""
        worker = threading.Thread(target=self._open_worker,
                                  args=(key, host, password),
                                  name="engine-open-%s" % key, daemon=True)
        worker.start()
        return {"accepted": True, "key": key}

    def _open_worker(self, key: str, host: dict, password: str):
        self._emit.notify("session.state", {
            "key": key, "state": "connecting",
            "detail": "正在连接 %s …" % (host.get("host") or host.get("serial_port") or ""),
        })
        try:
            manager = self._ensure_manager()
        except Exception as exc:
            self._emit.notify("session.state", {
                "key": key, "state": "error",
                "detail": "引擎无法加载 core：%s: %s" % (type(exc).__name__, exc),
            })
            return

        try:
            protocol = _normalize_protocol(host.get("protocol"))
            name = (host.get("name") or host.get("host")
                    or host.get("serial_port") or "设备")
            kwargs = {
                "name": name,
                "protocol": protocol,
                "username": host.get("username") or "",
                "password": password or "",
            }
            if protocol == "Serial":
                kwargs["serial_port"] = host.get("serial_port") or host.get("host") or ""
                kwargs["port"] = 0
                # 波特率：界面写 `serial_baud`（用户口径的键名），历史/引擎内部用 `baudrate`
                try:
                    kwargs["baudrate"] = int(host.get("serial_baud")
                                             or host.get("baudrate") or 9600)
                except Exception:
                    kwargs["baudrate"] = 9600
                # 数据位/校验/停止位/流控：同样两套键名都认。
                # 界面写的是 serial_bytesize / serial_parity / serial_stopbits / serial_flow，
                # 而 connector.connect() 收的是裸名 —— 这里做一次确定性映射。
                for bare, alias in (("bytesize", "serial_bytesize"),
                                    ("parity", "serial_parity"),
                                    ("stopbits", "serial_stopbits"),
                                    ("flow", "serial_flow")):
                    value = host.get(alias)
                    if value is None or value == "":
                        value = host.get(bare)
                    if value is None or value == "":
                        continue
                    kwargs[bare] = value
                for extra in _SERIAL_KEYS:
                    if extra in host and extra not in kwargs:
                        kwargs[extra] = host[extra]
            elif protocol == "eNSP":
                kwargs["host"] = host.get("host") or ""
                try:
                    kwargs["port"] = int(host.get("port") or 8443)
                except Exception:
                    kwargs["port"] = 8443
                kwargs["sandbox_id"] = int(host.get("sandbox_id") or 0)
                kwargs["node_id"] = int(host.get("node_id") or 0)
                if host.get("cookie"):
                    kwargs["cookie"] = host["cookie"]
            else:
                kwargs["host"] = host.get("host") or ""
                default_port = 22 if protocol == "SSH" else 23
                try:
                    kwargs["port"] = int(host.get("port") or default_port)
                except Exception:
                    kwargs["port"] = default_port
                if host.get("line_ending"):
                    kwargs["line_ending"] = host["line_ending"]

            sid = manager.create_session(**kwargs)
        except Exception as exc:
            log("error", "创建会话失败: %s: %s" % (type(exc).__name__, exc))
            self._emit.notify("session.state", {
                "key": key, "state": "error",
                "detail": "%s: %s" % (type(exc).__name__, exc),
            })
            return

        if sid is None or int(sid) < 0:
            # 串口这类"打开就失败"的场景，connector 已经给出了人话版原因
            # （被占用 / 不存在 / 驱动异常**文案不同**）——优先用它，别吞成一句通用失败。
            specific = ""
            try:
                specific = str(getattr(manager, "last_connect_error", "") or "")
            except Exception:                                   # noqa: BLE001
                specific = ""
            self._emit.notify("session.state", {
                "key": key, "state": "error",
                "detail": specific or "连接失败（认证失败 / 端口不通 / 设备无响应）",
            })
            return

        handle = _SessionHandle(key, int(sid), host, name)
        handle.connected = True
        handle.state = "connected"
        with self._lock:
            self._sessions[key] = handle

        self._emit.notify("session.state", {
            "key": key, "state": "connected", "sid": handle.sid,
            "detail": "已连接", "host": host,
        })
        # 连接期间累积的登录横幅与首个提示符，交给轮询线程首轮就吐出来，
        # 界面不需要为此做任何特殊处理
        log("info", "会话已建立: %s -> sid=%d" % (name, handle.sid))

    # ------------------------------------------------------------ 使用总结
    def m_usage_labels(self, params):
        """使用总结的**展示文案 DTO**（S4）：分页名 / 表头 / 按钮 / 占位符。

        值逐字对齐原 UI（cpp/src/ui/UsageSummaryDialog.cpp）——搬运前 UI 自己拼这些字面量；
        搬运后任何新 UI 直接渲染本字段即可（缺字段时旧 UI 仍回退到原字面量）。
        """
        return {"labels": {
            "window_title": "使用总结",
            "summary_placeholder": "（未归纳）原文见下方列表。",
            "search_placeholder": "按现象搜索（命令原文 / 回显）",
            "search_button": "搜索",
            "summarize_button": "AI 归纳",
            "summarize_tooltip": "用 AI 把原文归纳成一句话；没有配置 AI 时退化为纯列表",
            "group_titles": {"questions": "我问过的问题", "wrong": "命令写错",
                             "dead": "疑似不通", "ok": "可用示例"},
            "chain_tab": "排错链",
            "chain_headers": ["#", "时间", "触发命令", "后续步数", "状态"],
            "chain_resolve": "标记已解决",
            "chain_merge": "把选中记录并入该链",
            "chain_split": "把选中记录拆出链",
        }}

    def m_usage_state(self, params):
        log = self._usage()
        st = log.state()
        return st

    def m_usage_list(self, params):
        log = self._usage()
        keyword = str(params.get("search") or "")
        groups = log.groups()
        chains = log.chains()
        st = log.state()
        # 「界面显示 ≠ 引擎记录」时**唯一**的对齐点：把每次 usage.list 的载荷规模写进 trace
        # （只在 AIT_USAGE_TRACE 打开时写）。这个 RPC 的结果没有别的落点，所以界面少显示时
        # 无法区分"引擎没给"还是"界面没画" —— 2026-10-05 在 Eve-NG 上就是靠这条定位的。
        self._trace("usage.list", search=repr(keyword),
                    q=len(groups.get("questions") or []),
                    w=len(groups.get("wrong") or []),
                    d=len(groups.get("dead") or []),
                    ok=len(groups.get("ok") or []),
                    chains=len(chains), total=st.get("total"))
        return {
            "groups": groups,
            "chains": chains,
            "hits": log.search(keyword) if keyword else [],
            "state": st,
            "markdown": log.to_markdown(),
        }

    def m_usage_set_monitor(self, params):
        enabled = bool(params.get("enabled"))
        log = self._usage()
        log.set_monitor(enabled)
        try:
            settings = self._settings_obj()
            settings.set("usage_monitor", enabled)
            settings.save()
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        return {"ok": True, "state": log.state()}

    def m_usage_mark(self, params):
        ok = self._usage().mark_resolved(int(params.get("chain") or 0),
                                        bool(params.get("resolved", True)))
        return {"ok": ok, "chains": self._usage().chains()}

    def m_usage_merge(self, params):
        ok = self._usage().merge(int(params.get("chain") or 0),
                                 int(params.get("entry") or 0))
        return {"ok": ok, "chains": self._usage().chains()}

    def m_usage_split(self, params):
        ok = self._usage().split(int(params.get("entry") or 0))
        return {"ok": ok, "chains": self._usage().chains()}

    def m_usage_clear(self, params):
        n = self._usage().clear()
        return {"ok": True, "cleared": n, "state": self._usage().state()}

    def m_usage_candidate(self, params):
        return self._usage().write_candidate(int(params.get("entry") or 0))

    def m_usage_summarize(self, params):
        """顶部一句话归纳（**唯一**用到 AI 的地方，且只归纳已有原文）。

        没有可用 AI 时明确返回 ok=False —— 界面据此退化成纯列表（不报错、不空屏）。
        """
        provider, model, base_url, key = self._ai_config()
        if not key:
            return {"ok": False, "error": "未配置 AI，已退化为纯列表（原文见下）",
                    "provider": provider}
        log = self._usage()
        groups = log.groups()
        lines = []
        for key_name, title in (("questions", "我问过的问题"), ("wrong", "命令写错"),
                                ("dead", "疑似不通"), ("ok", "可用示例")):
            for e in (groups.get(key_name) or [])[:20]:
                lines.append("[%s] %s %s %s" % (title, e["time"], e["text"], e["echo"][:80]))
        if not lines:
            return {"ok": False, "error": "本次还没有可归纳的原文"}
        from core.llm import LLMClient, LLMError        # noqa: WPS433
        client = LLMClient(provider=provider, api_key=key, model=model, base_url=base_url)
        system = ("你在给运维人员做**会话复盘**。下面是他本机的原文记录。"
                  "请用**一句话**（40 字内）归纳这次主要在做什么、卡在哪；"
                  "只能用记录里出现过的事实，不要补充任何新信息。")
        try:
            text = client.chat(system=system, user="\n".join(lines[:60]))
        except LLMError as exc:
            return {"ok": False, "error": "%s" % exc, "provider": provider}
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        return {"ok": True, "summary": (text or "").strip(), "provider": provider,
                "label": "AI 归纳，原文见下"}

    # ------------------------------------------------------------ 串口（Console）
    def _serial_in_use(self):
        """本程序**当前正占用**的串口（供界面自动选中并标"正在使用"）。

        只读遍历会话：SerialConnector 的 `_port` 就是它打开的那个口。
        读不到就返回空列表 —— 这个信息是"锦上添花"，绝不能因为它抛异常。
        """
        used = []
        try:
            sessions = getattr(getattr(self, "_manager", None), "_sessions", None) or {}
            for sess in list(sessions.values()):
                conn = getattr(sess, "connector", None)
                port = getattr(conn, "_port", None)
                if port:
                    used.append(str(port))
        except Exception:                                       # noqa: BLE001
            return []
        return sorted(set(used))

    def m_serial_ports(self, params):
        """枚举串口（确定性）：给界面做下拉 + 自动选口依据。

        返回：
          {"ports": [{device,description,hwid,manufacturer,vid_pid,is_usb,label}],
           "in_use": ["COM5"], "last_port": "COM5", "count": N, "usb_count": M}
        """
        try:
            from core.serial_connector import SerialConnector       # noqa: WPS433
            ports = SerialConnector.classify_ports()
        except Exception as exc:                                    # noqa: BLE001
            return {"ports": [], "in_use": self._serial_in_use(), "last_port": "",
                    "count": 0, "usb_count": 0,
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        last = ""
        try:
            last = str(self._settings_obj().get("last_serial_port") or "")
        except Exception:                                           # noqa: BLE001
            last = ""
        return {
            "ports": ports,
            "in_use": self._serial_in_use(),
            "last_port": last,
            "count": len(ports),
            "usb_count": sum(1 for p in ports if p.get("is_usb")),
        }

    def m_serial_remember(self, params):
        """记住"上次成功连过的串口"（供自动选口第 4 优先级使用）。"""
        port = str(params.get("port") or "").strip()
        if not port:
            return {"ok": False, "error": "port 为空"}
        try:
            settings = self._settings_obj()
            settings.set("last_serial_port", port)
            settings.save()
        except Exception as exc:                                    # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        return {"ok": True, "last_port": port}

    # ------------------------------------------------------------ 资料库
    def _knowledge(self):
        """懒加载并复用同一个 KnowledgeBase。

        必须复用：知识库首次检索要建内存索引（26,286 篇约 13s），
        每个请求都新建实例的话每次都要重建一遍，界面会卡到不可用。
        """
        kb = getattr(self, "_kb", None)
        if kb is None:
            from core.knowledge import KnowledgeBase
            kb = KnowledgeBase()
            kb.ensure_dir()
            self._kb = kb
        return kb

    def m_knowledge_stats(self, params):
        return self._knowledge().stats()

    def m_knowledge_list(self, params):
        docs = self._knowledge().list_documents()
        try:
            limit = int(params.get("limit") or 300)
        except (TypeError, ValueError):
            limit = 300
        # 只回界面需要的字段：完整条目里带 error/chunks 等，回传体积没必要
        items = [{
            "name": d.get("name"),
            "category": d.get("category") or "",
            "size": d.get("size") or 0,
            "chunks": d.get("chunks") or 0,
            "error": d.get("error") or "",
        } for d in docs[:max(limit, 0)]]
        return {"documents": items, "total": len(docs)}

    def m_knowledge_search(self, params):
        query = str(params.get("query") or "").strip()
        if not query:
            return {"hits": [], "query": ""}
        try:
            top_k = int(params.get("top_k") or 5)
        except (TypeError, ValueError):
            top_k = 5
        hits = self._knowledge().search(query, top_k=top_k)
        # 只回片段：整段正文可能上千字，界面只用来预览
        return {"query": query, "hits": [{
            "source": h.get("source") or "",
            "name": os.path.basename(h.get("source") or ""),
            "score": h.get("score") or 0,
            "matched": h.get("matched") or [],
            "snippet": (h.get("text") or "")[:600],
        } for h in hits]}

    def m_knowledge_index(self, params):
        return self._knowledge().build_index(force=bool(params.get("force")))

    def m_knowledge_import(self, params):
        paths = params.get("paths") or []
        if not paths:
            return {"added": [], "index": {}}
        kb = self._knowledge()
        res = kb.import_paths([str(p) for p in paths],
                              recursive=bool(params.get("recursive", True)))
        return {"added": res.get("added") or [],
                "errors": res.get("errors") or [],
                "index": kb.build_index(force=True)}

    def m_knowledge_delete(self, params):
        name = str(params.get("name") or "").strip()
        if not name:
            return {"removed": False}
        return {"removed": bool(self._knowledge().remove_document(name))}

    # ------------------------------------------------------------ AI 助手
    def _ai_config(self):
        """当前提供方、模型与密钥。"""
        settings = self._settings_obj()
        provider = settings.ai_provider
        cfg = settings.get_provider_config(provider) or {}
        key = settings.api_key or ""
        if not key:
            # 免密钥提供方（本地模型 provider=local）即便用户没填 key 也必须能用。
            # 不兜这一下，`ai.ask` 会在 :832 直接回"还没有配置 local 的 API 密钥" ——
            # 而本地根本不需要密钥，用户看到这句话只会以为功能坏了。
            # 单一真源是 core/llm.py 的 provider_defaults()[no_key/default_key]，
            # 与 LLMClient.__init__ 的兜底保持一致（同一条规则不在两处各写一遍）。
            try:
                from core.llm import provider_defaults  # noqa: WPS433
                defaults = provider_defaults(provider)
                if defaults.get("no_key"):
                    key = defaults.get("default_key") or ""
            except Exception as exc:                # noqa: BLE001
                # **不要静默 pass**：这一处曾因导入路径写错（`engine.llm` 根本不存在）
                # 被吞掉，表现成"本地模型永远报没配密钥"，排查一整轮才定位到。
                log("warn", "免密钥提供方兜底失败 %s: %s" % (provider, exc))
        return provider, (cfg.get("model") or ""), (cfg.get("base_url") or ""), key

    def m_ai_status(self, params):
        provider, model, base_url, key = self._ai_config()
        # 走查 P2-7：界面拿到的是原始 id（anthropic），而显示名在 core/llm.py 里
        # 已有单一真源 provider_label() —— 带出去给前端用，别让前端自己拼。
        try:
            # ⚠ 这里原本写的是 `from .llm import …`，而 `engine/llm.py` **并不存在**
            # ⇒ ImportError 被下面的 except 吞掉、静默降级成原始 id，于是"显示名走
            # core/llm.py 单一真源"这条注释里的意图一直没生效。全文件 4 处同款写法
            # 都已改为 `core.llm`（2026-10-16 实测定位）。
            from core.llm import provider_label  # noqa: WPS433
            label = provider_label(provider)
        except Exception:  # noqa: BLE001
            label = provider
        # A1：除了"当前生效的配没配"，还要告诉界面"**别的**提供方里有没有配好的"
        # —— 否则用户只会看到"还没有配置 anthropic 的密钥"，而不知道自己其实
        # 已经配好了 deepseek（这正是走查里那条影响真体验的缺陷）。
        configured, suggested, suggested_label = [], "", ""
        try:
            from core.llm import PROVIDERS as _ALL     # noqa: WPS433
        except Exception:                          # noqa: BLE001
            _ALL = ()
        for name in list(_ALL) + ["anthropic", "deepseek", "openai_compat"]:
            name = str(name)
            if name in configured:
                continue
            try:
                has = bool(self._settings_obj().get_api_key_for(name))
            except Exception:                      # noqa: BLE001
                has = False
            if has:
                configured.append(name)
        for name in configured:
            if name != provider:
                suggested = name
                try:
                    from core.llm import provider_label as _lbl   # noqa: WPS433
                    suggested_label = _lbl(name)
                except Exception:                  # noqa: BLE001
                    suggested_label = name
                break
        return {
            "provider": provider,
            "providerLabel": label,
            "model": model,
            "ready": bool(key),
            "activeHasKey": bool(key),
            "configuredProviders": configured,
            "suggestedProvider": suggested,
            "suggestedLabel": suggested_label or suggested,
            # **绝不回传密钥本身**：界面只需要知道"配没配"。
            # 一旦把密钥送到前端，它就会出现在日志、截图和崩溃转储里。
            "keyHint": (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("已配置" if key else ""),
        }

    def m_ai_set_provider(self, params):
        """A1 的一键切换：把生效提供方换成"已配置密钥"的那个。"""
        name = str(params.get("provider") or "").strip()
        if not name:
            return {"ok": False, "error": "provider 为空"}
        try:
            settings = self._settings_obj()
            settings.ai_provider = name
            settings.save()
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        return {"ok": True, "provider": name}

    def m_ai_ask(self, params):
        question = str(params.get("question") or "").strip()
        if not question:
            return {"answer": "", "error": "问题为空"}
        try:
            # 触发①：开始问 AI → 开记录窗口（问答期间记录）。
            # 指示已经在状态栏接通（可见），所以这里**无条件**记 —— 用户看得见。
            self._usage().note_question(question)
        except Exception:                              # noqa: BLE001
            pass

        provider, model, base_url, key = self._ai_config()
        if not key:
            return {"answer": "",
                    "error": f"还没有配置 {provider} 的 API 密钥（菜单 工具 → AI 设置）"}

        # 先检索本地资料库做接地。这一步是产品价值所在：
        # 让 AI 基于**用户自己下载的官方文档**回答，而不是凭记忆编。
        blocks, sources = [], []
        try:
            top_k = int(params.get("top_k") or 3)
        except (TypeError, ValueError):
            top_k = 3
        is_local = (provider == "local")
        use_knowledge = bool(params.get("useKnowledge", True))
        vendor = str(params.get("vendor") or "")

        # 本地模型走**向量 RAG + 专用提示词**，而不是那句两句话的通用提示词：
        #   ① 向量检索带真正的「文件 · 小节 · 原始出处」，模型才标得出可核验的来源；
        #   ② prompt_local_assistant.txt 是为小模型调的（编号规则 / 必须标来源 /
        #      【未查证】出口 / 命令纪律）—— 通用提示词会让 4B 直接开始凭记忆编。
        # 拿不到向量库（没建索引 / 没装依赖 / 嵌入服务没起）就**静默退回**下面的词法层，
        # 界面绝不能因为检索层不可用而报错。
        system_local = ""
        if is_local and use_knowledge:
            try:
                from core import local_rag                # noqa: WPS433
                hits = local_rag.search(question, topk=top_k, vendor=vendor)
                if hits:
                    context = local_rag.build_context(hits, query=question)
                    system_local = local_rag.system_prompt(context, vendor=vendor, model=model)
                    if system_local:
                        blocks = [context]        # 仅用于标记"本次有资料"
                        sources = [{"name": h.get("file") or "",
                                    "section": h.get("section") or "",
                                    "score": h.get("score") or 0} for h in hits]
            except Exception as exc:                  # noqa: BLE001
                log(f"向量 RAG 本次不可用，退回词法检索: {exc}")

        user_local = question
        if not blocks and use_knowledge:
            # 注入预算：本地模型跑在交付机上，首字延迟的大头就是 prompt eval
            # （实测 ≈100 tok/s，1200 字上下文约 12s）。所以本地按 local_llm 那套
            # 定好的预算来（单片段 ≤800 字、总量 ≤1200 字，与 local_llm/retrieve.py 一致）；
            # 云端给足即可 —— 多给资料只会更准，不占用户的等待时间。
            per_block = 800 if is_local else 900
            total_budget = 1200 if is_local else 0        # 0 = 不限总量
            used = 0
            try:
                for h in self._knowledge().search(question, top_k=top_k):
                    text = (h.get("text") or "")[:per_block]
                    if not text:
                        continue
                    if total_budget and used + len(text) > total_budget:
                        break       # 预算用完就停：宁可少给一条资料，也别堆首字延迟
                    used += len(text)
                    blocks.append(f"【资料{len(blocks) + 1}】{text}")
                    sources.append({
                        "name": os.path.basename(h.get("source") or ""),
                        "score": h.get("score") or 0,
                    })
            except Exception as exc:  # noqa: BLE001
                # 检索失败不该让问答整体失败 —— 退化成不带资料作答，并在日志里留痕
                log(f"资料库检索失败，本次改为无资料作答: {exc}")

        from core.llm import LLMClient, LLMError  # noqa: WPS433

        client = LLMClient(provider=provider, api_key=key,
                           model=model, base_url=base_url)
        if system_local:
            # 本地：资料已经嵌进提示词的【厂商资料】段，user 只发问题本身
            system, user = system_local, user_local
        elif blocks:
            system = ("你是网络设备助手。请**只根据下面提供的资料**回答；"
                      "资料里没有的信息就直说不知道，不要编造。回答控制在 300 字内。")
            user = "资料：\n" + "\n\n".join(blocks) + f"\n\n问题：{question}"
        else:
            system = "你是网络设备助手，回答务必简洁准确。"
            user = question

        try:
            answer = client.chat(system, [{"role": "user", "content": user}],
                                 max_tokens=800)
        except LLMError as exc:
            return {"answer": "", "error": str(exc), "sources": sources}
        except Exception as exc:  # noqa: BLE001
            return {"answer": "", "error": f"{type(exc).__name__}: {exc}",
                    "sources": sources}

        return {"answer": answer,
                "sources": sources,
                "usedKnowledge": bool(blocks)}

    def m_ai_optimize(self, params):
        """优化提示词：把用户的原始问题改写成更清晰具体的提问（只改写，不回答）。

        AiPanel 的「优化提示词」按钮用：点击后把输入框内容换成改写版，
        用户可以直接发送或继续编辑。**不检索资料库**（这是对提问本身的
        改写，跟问题内容无关），只走一次大模型。
        """
        text = str(params.get("text") or "").strip()
        if not text:
            return {"text": "", "error": "输入为空，先写点什么再优化"}

        provider, model, base_url, key = self._ai_config()
        if not key:
            return {"text": "",
                    "error": f"还没有配置 {provider} 的 API 密钥（菜单 工具 → AI 设置）"}

        from core.llm import LLMClient, LLMError  # noqa: WPS433

        client = LLMClient(provider=provider, api_key=key,
                           model=model, base_url=base_url)
        system = ("你是网络设备领域的提问优化助手。把用户输入的原始问题改写成"
                  "更清晰、更具体的提问：补全含糊的指代，点明涉及的设备/版本/"
                  "操作场景。信息不足时按最常见的场景直接补全，不要反问用户。"
                  "**只输出改写后的提问本身**，不要回答问题、不要解释改写理由、"
                  "不要加引号或前缀。保持中文，一条提问，120 字内。")
        try:
            out = client.chat(system, [{"role": "user", "content": text}],
                              max_tokens=400)
        except LLMError as exc:
            return {"text": "", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"text": "", "error": f"{type(exc).__name__}: {exc}"}

        out = (out or "").strip()
        if not out:
            return {"text": "", "error": "模型返回了空文本，请重试"}
        return {"text": out}

    # ------------------------------------------------------------ 多设备巡检
    def m_patrol_run(self, params):
        """并发巡检多台设备，返回逐台结果。

        这是一个**阻塞**调用（设备多时可能几十秒）。C++ 侧的 callAsync 是
        回调式的，界面不会因此冻住；但引擎的这条请求会占住一个分发线程。

        并发度封顶 8：巡检会为每台设备新建连接，
        设备一多就同时打开几十个 TCP/STOMP 会话，容易被设备侧限流或拖垮本机。
        """
        devices = [d for d in (params.get("devices") or []) if isinstance(d, dict)]
        commands = [str(c).strip() for c in (params.get("commands") or []) if str(c).strip()]
        if not devices:
            return {"error": "没有选择要巡检的设备", "results": []}
        if not commands:
            return {"error": "没有选择巡检命令", "results": []}

        import concurrent.futures as cf  # noqa: WPS433

        from core.patrol import patrol_device  # noqa: WPS433

        try:
            timeout = float(params.get("timeout") or 25.0)
        except (TypeError, ValueError):
            timeout = 25.0

        results = []
        with cf.ThreadPoolExecutor(max_workers=min(len(devices), 8)) as pool:
            futures = {pool.submit(patrol_device, dict(d), list(commands), timeout): i
                       for i, d in enumerate(devices)}
            for future in cf.as_completed(futures):
                index = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001
                    # 单台设备抛异常不能带走整次巡检 —— 其余设备的结果仍要交回
                    row = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                row["_index"] = index
                row["_plan"] = devices[index]
                results.append(row)

        results.sort(key=lambda r: r.get("_index", 0))
        out = []
        for row in results:
            plan = row.get("_plan") or {}
            cmds = []
            for c in (row.get("commands") or []):
                if isinstance(c, dict):
                    cmds.append({"command": c.get("command") or c.get("cmd") or "",
                                 # 输出截断：整篇 display 输出可能几十 KB，
                                 # 全量回传会把 JSON-RPC 的单行消息撑得很大
                                 "output": (c.get("output") or "")[:4000]})
            out.append({
                "name": row.get("name") or plan.get("name") or "",
                "host": row.get("host") or plan.get("host") or "",
                "protocol": row.get("protocol") or plan.get("protocol") or "",
                "ok": bool(row.get("ok")),
                "elapsed": round(float(row.get("elapsed") or 0), 2),
                "error": row.get("error") or "",
                "commands": cmds,
            })
        return {"results": out,
                "total": len(out),
                "ok": sum(1 for r in out if r["ok"])}

    # ------------------------------------------------------------ AI 设置
    def m_ai_get_settings(self, params):
        """返回当前配置 + 所有可选提供方的默认值。

        **不回传任何密钥明文**，只回"配没配"和一个掩码提示 ——
        设置界面本身也不需要看到原密钥（用户要改就直接输入新的）。
        """
        from core.llm import PROVIDERS, provider_defaults, provider_label  # noqa: WPS433

        settings = self._settings_obj()
        provider = settings.ai_provider
        cfg = settings.get_provider_config(provider) or {}
        key = settings.api_key or ""

        providers = []
        for name in PROVIDERS:
            defaults = provider_defaults(name)
            stored = settings.get_provider_config(name) or {}
            providers.append({
                "id": name,
                "label": provider_label(name),
                "model": stored.get("model") or defaults.get("model") or "",
                "base_url": stored.get("base_url") or defaults.get("base_url") or "",
                "models": defaults.get("models") or [],
                "hasKey": bool(settings.get_api_key_for(name)),
            })

        return {
            "provider": provider,
            "model": cfg.get("model") or "",
            "base_url": cfg.get("base_url") or "",
            "hasKey": bool(key),
            "keyHint": (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("已配置" if key else ""),
            "providers": providers,
        }

    def m_ai_set_settings(self, params):
        settings = self._settings_obj()

        provider = str(params.get("provider") or "").strip().lower()
        if provider:
            settings.ai_provider = provider
        target = provider or settings.ai_provider

        # 密钥为**空串时不动原值**：设置界面里密钥框是空的（不回显），
        # 用户只想改模型却把密钥清空，是最容易踩的坑。
        key = params.get("api_key")
        if isinstance(key, str) and key.strip():
            settings.set_api_key_for(target, key.strip())

        model = params.get("model")
        base_url = params.get("base_url")
        if model is not None or base_url is not None:
            settings.set_provider_config(
                target,
                model=(str(model).strip() or None) if model is not None else None,
                base_url=(str(base_url).strip() or None) if base_url is not None else None)

        settings.save()
        return {"saved": True, "provider": settings.ai_provider}

    def m_ai_test(self, params):
        """用当前配置真发一次极短的请求，验证密钥/模型/地址是否可用。

        只要求模型回两个字，把 token 消耗压到最低 ——
        用户点"测试"时不该为此付一次完整问答的费用。
        """
        provider, model, base_url, key = self._ai_config()
        if not key:
            return {"ok": False, "message": f"还没有配置 {provider} 的 API 密钥"}

        from core.llm import LLMClient  # noqa: WPS433

        client = LLMClient(provider=provider, api_key=key,
                           model=model, base_url=base_url)
        try:
            answer = client.chat("回答务必极简。",
                                 [{"role": "user", "content": "回复两个字：正常"}],
                                 max_tokens=16)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}
        return {"ok": True,
                "model": client.model,
                "message": f"连通正常（{(answer or '').strip()[:20]}）"}

    # ------------------------------------------------------------ 命令补全
    def m_commands_suggest(self, params):
        """命令自动补全：三层（token / next / sequences）。

        补全跑在**按键路径**上，所以这里绝不抛异常：引擎是 JSON-RPC 服务，
        一次抛异常会打断整个请求（界面连"没提示"都收不到），
        而补全失败最坏也只是没有提示 —— 必须降级成空结果 + error 文本。

        数据目录每次调用时读环境变量，**不缓存到模块层级**：
        这样测试可以临时指到不存在的目录来验证降级路径。
        """
        from core import command_suggest as suggest_mod  # noqa: WPS433

        try:
            limit = int(params.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        data_dir = os.environ.get("AIT_VENDOR_CMD_DIR") or suggest_mod.DEFAULT_DIR
        try:
            out = suggest_mod.suggest(
                str(params.get("typed") or ""),
                str(params.get("vendor") or ""),
                model=str(params.get("model") or ""),
                limit=limit,
                data_dir=data_dir,
                # 设备当前提示符：用它算当前视图，只推这个视图里能敲的命令
                prompt=str(params.get("prompt") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            out = {"error": "%s: %s" % (type(exc).__name__, exc),
                   "token": [], "next": [], "sequences": []}

        # ---- ★ 增量：**技术维度**的补全候选（用户敲的是技术词而不是命令前缀时用得上）
        # 场景：用户敲 `防环`/`堆叠`/`ospf`，command_suggest 的命令前缀索引一定推不出东西
        # （那三个词都不是命令开头），但技术资料层知道它们对应哪些分类、哪些命令。
        # 纯代码、读缓存索引（毫秒级）；失败只是少一类候选，绝不影响原有三层补全。
        # ⚠ 只在 typed >= 2 个字符时查，避免刚敲第一个字母就去扫全表。
        try:
            typed = str(params.get("typed") or "").strip()
            vendor = str(params.get("vendor") or "").strip().lower()
            if len(typed) >= 2 and vendor:
                from core import tech_knowledge as tk          # noqa: WPS433
                feats = tk.search(vendor, typed, top_k=5)
                tech = []
                for f in feats:
                    node = (tk._index(vendor).get("features") or {}).get(f["feature"]) or {}
                    tech.append({
                        "feature": f["feature"],
                        "score": f["score"],
                        "why": f.get("why") or [],
                        # 该分类下最值得先试的几条命令（带 view 便于界面提示"在哪敲"）
                        "commands": [{"command": c,
                                      "view": ((tk._index(vendor).get("commands") or {})
                                               .get(c.lower(), {}) or {}).get("view", "")}
                                     for c in (node.get("commands") or [])[:8]],
                    })
                out["tech"] = tech
        except Exception as exc:  # noqa: BLE001
            out["tech"] = []
            out["tech_error"] = "%s: %s" % (type(exc).__name__, exc)
        return out

    # ------------------------------------------------------------ 单条命令的知识库
    def m_text_classify(self, params: dict):
        """逐段文本 -> 语义标签（与 C++ core::TextClassify 同源口径）。

        参数：runs: [{"text": str, "lineBefore": str, "deviceColored": bool}, ...]
        返回：{"labels": ["prompt"|"ok"|"iface"|"neutral", ...]}
        ⚠ 仅供未来非 C++ UI 使用，**不进渲染路径**。
        """
        from core.text_classify import classify_run
        runs = params.get("runs") or []
        out = []
        for r in runs:
            if isinstance(r, str):
                out.append(classify_run(r))
            else:
                out.append(classify_run(str(r.get("text") or ""),
                                        str(r.get("lineBefore") or ""),
                                        bool(r.get("deviceColored"))))
        return {"labels": out}

    def m_knowledge_explain(self, params):
        """**技术解释**：代码检索优先，检索不到才（可选）上 AI。

        参数：
            vendor   厂商（h3c / huawei / ruijie）
            text     查询词 —— 技术名（`OSPF`/`防环`/`堆叠`）或命令名（`acl number`）
            maxBytes 返回正文的字节上限（默认 8 KB）
            useAI    没检索到时要不要调模型（默认 0）。**必须显式打开** ——
                     补全/浏览这类高频路径不该偷偷花钱。
            question 上 AI 时的原始提问（默认用 text）

        返回（`core/tech_knowledge.explain` 的全部字段）+ 三个增量字段：
            aiUsed  这次真的调模型了吗
            aiSaved 模型回答有没有沉淀进 `tech/ai/`（沉淀了下次就是代码命中）
            error   失败原因（有值时其它字段仍然可用，不静默）

        跑在**交互路径**上，绝不抛异常：任何异常都降级成 available=False + error。
        """
        from core import tech_knowledge as tk                # noqa: WPS433

        vendor = str(params.get("vendor") or "").strip().lower()
        text = str(params.get("text") or "").strip()
        try:
            max_bytes = int(params.get("maxBytes") or tk.MAX_RETURN_BYTES)
        except (TypeError, ValueError):
            max_bytes = tk.MAX_RETURN_BYTES
        want_ai = bool(params.get("useAI"))

        try:
            out = tk.explain(vendor, text, max_bytes=max_bytes)
        except Exception as exc:                             # noqa: BLE001
            log("warn", "knowledge.explain 检索失败: %s: %s" % (type(exc).__name__, exc))
            return {"available": False, "need_ai": False, "source": "none", "how": "",
                    "reason": "技术资料检索失败：%s" % exc, "vendor": vendor, "term": text,
                    "feature": "", "file": "", "command": "", "syntax": "", "view": "",
                    "markdown": "", "truncated": False, "candidates": [],
                    "ai_markdown": "", "ai_file": "", "prefer": "vendor",
                    "aiUsed": False, "aiSaved": False, "error": "%s: %s"
                    % (type(exc).__name__, exc)}

        out["aiUsed"] = False
        out["aiSaved"] = False
        out["error"] = ""
        if out.get("available") or not want_ai:
            return out

        # ---- 代码检索不到，且调用方明确要上 AI ----
        question = str(params.get("question") or "").strip() or text
        provider, model, base_url, key = self._ai_config()
        if not key:
            out["error"] = ("技术资料里没有对应条目，且还没配置 %s 的 API 密钥"
                            "（菜单 工具 → AI 设置）" % provider)
            return out

        # 把"最相近的几个分类"的**真实厂商原文**喂给模型做接地。
        # 为什么必须给原文：没有原文，模型就是凭记忆编；给了原文，它至少是在总结
        # 这家厂商自己的文档。找不到任何候选时**不给**（宁可不答，也不要编）。
        context = self._tech_context(vendor, out.get("candidates") or [])
        from core.llm import LLMClient                        # noqa: WPS433
        client = LLMClient(provider=provider, api_key=key, model=model, base_url=base_url)

        if context:
            system = ("你是网络设备厂商技术资料的编辑。只能依据我提供的厂商文档原文回答；"
                      "原文没有的内容一律写「原文未涉及」，不得凭自己的知识补充。"
                      "用简体中文，技术缩写保留原文写法。回答控制在 400 字内。")
            user = "厂商文档原文（唯一依据）：\n%s\n\n问题：%s" % (context, question)
        else:
            system = ("你是网络设备厂商技术资料的编辑。本厂商的资料里没有检索到与该问题"
                      "对应的条目；请说明这一点，并**只**给出你确定无误的基础说明，"
                      "不确定的部分明确写「不确定」。不要编造命令、参数或默认值。"
                      "回答控制在 300 字内。")
            user = question

        try:
            answer = client.chat(system, [{"role": "user", "content": user}],
                                 max_tokens=900, temperature=0.2)
        except Exception as exc:                             # noqa: BLE001
            out["error"] = str(exc)
            return out

        answer = (answer or "").strip()
        if not answer:
            out["error"] = "模型没有返回内容"
            return out

        out["aiUsed"] = True
        out["available"] = True
        out["source"] = "ai"
        out["how"] = "ai-live" if context else "ai-live-nocontext"
        out["markdown"] = answer
        out["ai_markdown"] = answer
        out["prefer"] = "ai"
        out["need_ai"] = False

        # **沉淀**：写进 tech/ai/，下次同样的提问就是代码命中，不再调模型。
        # 失败不影响本次回答（只是下次还要再调一次）。
        try:
            res = tk.learn(vendor, text, answer, question=question,
                           model=model, source="ai-live")
            out["aiSaved"] = bool(res.get("ok"))
            out["ai_file"] = res.get("file") or ""
            if not res.get("ok"):
                out["error"] = "回答已生成，但沉淀失败：%s" % res.get("reason")
        except Exception as exc:                             # noqa: BLE001
            out["error"] = "回答已生成，但沉淀失败：%s" % exc

        log("info", "knowledge.explain useAI vendor=%s query=%r how=%s saved=%s"
            % (vendor, text, out.get("how"), out.get("aiSaved")))
        return out

    def _tech_context(self, vendor: str, candidates: list) -> str:
        """把候选 feature 的厂商原文拼成接地上下文（**有界**）。取不到就返回空串。"""
        from core import tech_knowledge as tk                # noqa: WPS433
        blocks, budget = [], 6000
        for c in (candidates or [])[:3]:
            feat = str(c.get("feature") or "")
            if not feat or budget <= 0:
                continue
            try:
                doc = tk._read_feature(vendor, feat, max_bytes=min(2200, budget))
            except Exception:                                # noqa: BLE001
                continue
            text = (doc.get("markdown") or "").strip()
            if not text:
                continue
            blocks.append("【%s】\n%s" % (feat, text))
            budget -= len(text)
        return "\n\n".join(blocks)

    def m_knowledge_command(self, params):
        """Ctrl+点击命令 → 一条命令的完整知识（命令表 + 厂商技术解释）。

        与 commands.suggest 一样跑在**交互路径**上，绝不抛异常：
        查不到就 found=False（界面据此显示"知识库暂无该命令"，不静默）。
        """
        from core import command_suggest as suggest_mod        # noqa: WPS433
        from core import vendor_knowledge as vk                # noqa: WPS433

        vendor = str(params.get("vendor") or "").strip().lower()
        text = str(params.get("text") or "").strip()
        data_dir = os.environ.get("AIT_VENDOR_CMD_DIR") or suggest_mod.DEFAULT_DIR

        # ★ 批次三：**先查 SQLite**（knowledge/<vendor>/commands.sqlite，建库脚本产出），
        # 查不到再回退旧的 jsonl 数据源。返回里带 `layer` 标明这次数据来自哪条路，
        # 便于验收断言"确实是 SQLite 在供数"，也便于日后淘汰 jsonl。
        info = None
        try:
            hit = vk.lookup_sqlite(vendor, text)
            if hit.get("found"):
                rows = hit.get("rows") or []
                first = rows[0] if rows else {}
                info = {
                    "found": True, "layer": "sqlite", "vendor": vendor, "query": text,
                    "command": first.get("command") or text,
                    "syntax": first.get("syntax") or "",
                    "view": first.get("view") or "",
                    "feature": first.get("feature") or "",
                    "model": first.get("model") or "",
                    "source": first.get("source") or "",
                    "rows": rows,
                    "examples": hit.get("examples") or [],
                    "commands": hit.get("commands") or [],
                }
        except Exception as exc:                               # noqa: BLE001
            log("warn", "SQLite 查询失败，回退 jsonl: %s: %s" % (type(exc).__name__, exc))

        if info is None:
            try:
                info = suggest_mod.lookup(text, vendor, data_dir=data_dir,
                                          max_examples=int(params.get("examples") or 3))
                info["layer"] = "jsonl"
            except Exception as exc:                           # noqa: BLE001
                return {"found": False, "vendor": vendor, "query": text, "layer": "none",
                        "error": "%s: %s" % (type(exc).__name__, exc),
                        "examples": [], "explanation": {"available": False,
                                                        "reason": "命令数据读取失败"}}
        try:
            info["explanation"] = vk.explanation(vendor, info.get("command") or text)
        except Exception as exc:                               # noqa: BLE001
            info["explanation"] = {"available": False,
                                   "reason": "技术解释读取失败：%s" % exc}
        try:
            info["examplesCoverage"] = vk.examples_coverage(vendor)
        except Exception:                                      # noqa: BLE001
            info["examplesCoverage"] = {}
        # 定位过程写进日志（下次"右栏落错章节"一眼可查）：
        # 查的是哪条命令 → 数据来自哪层 → 落到哪个文件#锚点 → 有没有命中
        try:
            expl = info.get("explanation") or {}
            log("info", "knowledge.command layer=%s vendor=%s query=%r command=%r "
                        "chapter=%s anchor=%s available=%s"
                % (info.get("layer"), vendor, text, info.get("command"),
                   expl.get("file"), expl.get("anchor"), expl.get("available")))
        except Exception:                                      # noqa: BLE001
            pass
        # ---- 展示文案下沉（S3）：值逐字对齐 cpp/src/ui/KnowledgeDialog.cpp:317/362/370
        # 只**增量**加字段；既有 15 字段一个都不碰。
        try:
            _cov = info.get("examplesCoverage") or {}
            _n = len(info.get("examples") or [])
            _note = "例子（%d 条，取自厂商文档的配置案例）" % _n
            _x, _y = _cov.get("included"), _cov.get("total") if _cov else (None, None)
            if _cov and _x and _y and _x < _y:      # 与 UI 现状同条件：收录数少于总数才写出来
                _note += "\u3000\u00b7\u3000示例收录 %s/%s（其余未收录，见 index.json）" % (_x, _y)
            info["examples_note"] = _note
            info["empty_note"] = "该命令暂无技术解释。"
        except Exception:                              # noqa: BLE001
            pass
        # ---- ★ 技术解释（**增量**字段，既有字段一个都不碰）
        # 与上面的 `explanation` 的区别（两套数据、别混）：
        #   explanation  = vendor_knowledge 按 chapter#anchor 切的**命令字段表**切片，
        #                  实测 393 章正文合计 0 KB，所以它常年 available=False；
        #   tech         = tech_knowledge 按 feature 归集的**技术讲解正文**
        #                  （tools/build_tech_docs.py 从厂商原始文档提取），
        #                  才是真正有正文的那一层。
        # 用命令名去检索技术层：`acl number` → ACL 分类的讲解。
        try:
            from core import tech_knowledge as tk              # noqa: WPS433
            info["tech"] = tk.explain(vendor, info.get("command") or text)
        except Exception as exc:                               # noqa: BLE001
            info["tech"] = {"available": False, "need_ai": False, "source": "none",
                            "how": "", "reason": "技术资料层读取失败：%s" % exc,
                            "markdown": "", "ai_markdown": "", "prefer": "vendor",
                            "candidates": []}
        return info

    # ------------------------------------------------------------ 差异比对
    def m_diff_compare(self, params):
        """比对两段配置文本，返回逐行结果。

        两个默认开启的选项都有实际理由：
          drop_prompt  去掉 <HUAWEI> / [SW1] 这类提示符行 ——
                       从终端复制配置时必然带上，不去掉会满屏假差异
          drop_blank   去掉空行 —— 否则行号漂移会把真实差异淹掉
        """
        old = str(params.get("old") or "")
        new = str(params.get("new") or "")
        if not old.strip() and not new.strip():
            return {"error": "两段内容都是空的", "rows": []}

        from core import diff as diffmod  # noqa: WPS433

        drop_blank = bool(params.get("dropBlank", True))
        drop_prompt = bool(params.get("dropPrompt", True))
        try:
            context = int(params.get("context") or 3)
        except (TypeError, ValueError):
            context = 3

        rows = diffmod.diff_lines(old, new, context=context,
                                  drop_blank=drop_blank,
                                  drop_prompt=drop_prompt)
        added, removed = diffmod.summarize(old, new, drop_blank=drop_blank,
                                           drop_prompt=drop_prompt)
        changed = bool(added or removed)
        return {
            "rows": [{"kind": kind, "text": text} for kind, text in rows],
            "added": added,
            "removed": removed,
            "changed": changed,
            "unified": diffmod.to_unified(old, new) if changed else "",
        }

    def _handle_of(self, params: dict) -> _SessionHandle:

        key = str(params.get("key") or "").strip()
        with self._lock:
            handle = self._sessions.get(key)
        if handle is None:
            raise ValueError("会话不存在或已关闭: %s" % key)
        return handle

    def _connector_of(self, handle: _SessionHandle):
        session = self._session_obj(handle.sid)
        if session is None:
            return None
        return getattr(session, "connector", None)

    def m_session_write(self, params: dict) -> dict:
        handle = self._handle_of(params)
        data = params.get("data")
        if data is None:
            raise TypeError("缺少 data")
        self._trace("rpc.session.write", key=handle.key, data=repr(data))
        connector = self._connector_of(handle)
        if connector is None:
            return {"ok": False, "detail": "会话已失效"}
        try:
            # 交互输入在**行结束符**处才算提交（界面是逐字符实时发的，
            # 设备侧 Tab 补全必须作用在半截行上，所以这里自己攒行）
            buf = getattr(self, "_typed_buf", None)
            if buf is None:
                buf = self._typed_buf = {}
            acc = buf.get(handle.key, "")
            for ch in str(data):
                if ch in ("\r", "\n"):
                    self._trace("typed.terminator", key=handle.key, acc=acc,
                                active=self._usage().active())
                    if acc.strip():
                        try:
                            self._usage().note_command(handle.name or handle.key, acc)
                        except Exception:              # noqa: BLE001
                            pass
                    acc = ""
                elif ch == "\x7f":
                    acc = acc[:-1]
                elif ch.isprintable():
                    acc += ch
            buf[handle.key] = acc
            # send_raw 不追加换行、不带 0.3s sleep —— 交互式输入必须走这条
            connector.send_raw(str(data))
        except Exception as exc:
            return {"ok": False, "detail": "%s: %s" % (type(exc).__name__, exc)}
        return {"ok": True}

    def m_session_command(self, params: dict) -> dict:
        handle = self._handle_of(params)
        command = str(params.get("command") or "")
        if not command:
            raise TypeError("缺少 command")
        self._trace("rpc.session.command", key=handle.key, command=command)

        try:
            self._usage().note_command(handle.name or handle.key, command)
        except Exception:                              # noqa: BLE001
            pass

        def run():
            ok = False
            try:
                ok = bool(self._manager.send_command(handle.sid, command))
            except Exception as exc:
                log("error", "send_command 失败: %s" % exc)
            self._emit.notify("session.commandDone", {
                "key": handle.key, "command": command, "ok": ok,
            })

        threading.Thread(target=run, name="engine-cmd-%s" % handle.key,
                         daemon=True).start()
        return {"accepted": True}

    def m_session_close(self, params: dict) -> dict:
        handle = self._handle_of(params)
        self._close(handle.key)
        return {"ok": True}

    def _close(self, key: str):
        with self._lock:
            handle = self._sessions.pop(key, None)
        if handle is None:
            return
        try:
            if self._manager is not None:
                self._manager.close_session(handle.sid)
        except Exception as exc:
            log("warn", "关闭会话 %s 失败: %s" % (key, exc))
        self._emit.notify("session.state", {
            "key": key, "state": "closed", "detail": "会话已关闭",
        })

    def m_session_resize(self, params: dict) -> dict:
        handle = self._handle_of(params)
        connector = self._connector_of(handle)
        if connector is None:
            return {"ok": False}
        try:
            connector.resize_terminal(int(params.get("cols") or 80),
                                      int(params.get("rows") or 24))
        except Exception:
            return {"ok": False}
        return {"ok": True}

    def m_session_list(self, params: dict) -> dict:
        with self._lock:
            return {"sessions": [
                {"key": h.key, "sid": h.sid, "name": h.name,
                 "state": h.state, "host": h.host}
                for h in self._sessions.values()
            ]}

    # ------------------------------------------------------------ 输出轮询
    def _poll_loop(self):
        """唯一的输出通路：把连接器缓冲区里的内容搬给界面。

        get_output() 读后即清，引擎是进程内唯一消费者，因此不会和界面抢数据。
        顺带在同一轮里用 is_connected() 检测掉线 —— 因为断线/错误事件只走
        pyqtSignal，在没有 Qt 事件循环的进程里会静默丢失，只能靠轮询。
        """
        while not self._stop.is_set():
            with self._lock:
                handles = list(self._sessions.values())
            for handle in handles:
                if self._stop.is_set():
                    break
                if self._manager is None:
                    continue
                connector = None
                try:
                    connector = self._connector_of(handle)
                except Exception:
                    connector = None
                if connector is None:
                    continue

                try:
                    text = connector.get_output()
                except Exception:
                    text = ""
                if text:
                    try:
                        self._usage().note_output(handle.name or handle.key, text)
                    except Exception:                  # noqa: BLE001
                        pass          # 记录是附加功能，绝不能影响终端
                    self._emit.notify("session.output",
                                      {"key": handle.key, "text": text})

                try:
                    alive = bool(connector.is_connected())
                except Exception:
                    alive = False
                if alive != handle.connected:
                    handle.connected = alive
                    if alive:
                        handle.state = "connected"
                        self._emit.notify("session.state", {
                            "key": handle.key, "state": "connected",
                            "detail": "连接已恢复",
                        })
                    else:
                        handle.state = "disconnected"
                        self._emit.notify("session.state", {
                            "key": handle.key, "state": "disconnected",
                            "detail": "连接已断开",
                        })
            self._stop.wait(self.POLL_INTERVAL)
