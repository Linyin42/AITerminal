"""
主窗口 - NetOpsTerminal主界面
"""
import os
import re
import sys
import time
import subprocess
import threading

from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QStackedWidget, QSplitter,
    QStatusBar, QMessageBox, QApplication, QLabel, QProgressDialog,
    QFileDialog, QWidget, QPushButton, QToolButton, QTabBar, QSizePolicy,
    QHBoxLayout, QVBoxLayout
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction

from core.session import SessionManager
from core.terminal import detect_vendor, extract_device_info
from core.knowledge import (EXPERIENCE_CATEGORY, even_sample, extract_commands,
                            extract_text, find_command_candidates, read_text_file)
from core.ai_agent import AIAgent
from core.batch import BatchRunner
from ui.session_tab import SessionTab
from ui.ai_panel import AIPanel
from ui.ai_settings_dialog import AISettingsDialog
from ui.chat_panel import ChatView
from ui.vault_panel import VaultPanel
from ui.nav_rail import NavRail
from ui.connect_dialog import ConnectDialog, SerialPortScanner
from ui.template_dialog import TemplateDialog
from ui.diff_dialog import DiffDialog
from ui.prompt_dialog import PromptDialog
from ui.serial_dialog import SerialControlDialog
from ui.knowledge_dialog import KnowledgeDialog, build_file_filter
from ui.note_dialog import NoteDialog
from ui.patrol_dialog import PatrolDialog
from ui.command_confirm import ConfigConfirmDialog
from ui.theme import QSS as THEME_QSS
from core.safety import has_config_command
from config.settings import Settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConnectWorker(QThread):
    """在工作线程里跑 `SessionManager.create_session` —— 连接握手可能几十秒。

    为什么必须在线程里：`create_session` 内部是**同步 connect()**。SSH 默认
    timeout=10s、Telnet 还要等登录提示、串口要等驱动把端口打开；在 GUI 线程里
    跑就是"点一下连接，界面直接卡死，过一会才进去"（用户 2026-10-17 的反馈）。

    连接结果不需要这里返回：`SessionManager` 自己的 `session_created` /
    `session_error` 信号会送到主线程（Qt 跨线程自动排队），标签页照旧由
    `on_session_created` 建。本类只额外记录耗时与异常，供状态栏/慢日志用。
    """

    def __init__(self, manager, kwargs: dict, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._kwargs = kwargs
        self.session_id = -1
        self.error = ""
        self.cost = 0.0

    def run(self):
        started = time.monotonic()
        try:
            self.session_id = self._manager.create_session(**self._kwargs)
        except Exception as exc:                              # noqa: BLE001
            # 线程里抛异常会静默丢掉（界面只看到"没反应"），必须自己接住并上报
            self.error = f"{type(exc).__name__}: {exc}"
        self.cost = time.monotonic() - started


class MainWindow(QMainWindow):
    """NetOpsTerminal主窗口"""

    #: 资料库后台索引完成（从工作线程发回主线程，避免跨线程操作界面）
    knowledge_index_done = pyqtSignal(dict)
    #: 后台把一轮交互整理成了经验总结，交给主线程落库（写文件 + 更新界面）
    experience_ready = pyqtSignal(dict)
    #: 后台整理完一份导入文档（交给主线程落库，并进命令提示）
    digest_ready = pyqtSignal(dict)
    #: 工作线程 -> GUI 线程：调用 self.chat_view 上的方法（QWidget 只能主线程访问）
    chat_ui_call = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.session_manager = SessionManager()
        self.tabs = {}                 # session_id -> SessionTab
        # 掉线后"输入没发出去"只提示一次的会话集合（见 on_raw_input）
        self._input_dropped_notified = set()
        # 交互会话自动应答分页：session_id -> 最近一段输出（标记常被切成两半）
        self._page_tail = {}
        self.ai_auto_sessions = set()  # 处于自动模式的会话
        self.batch_runner = None       # 当前批处理任务
        self.progress_dialog = None    # 批处理进度对话框
        self.version_probes = {}       # session_id -> 正在等回显的版本探测
        self.version_tried = {}        # session_id -> 已试过的版本命令（防死循环）
        self._connect_task = None      # 正在跑的连接线程（见 create_session）
        self.knowledge_index_done.connect(self.on_knowledge_indexed)
        self.experience_ready.connect(self.on_experience_ready)
        self.digest_ready.connect(self.on_digest_ready)
        self.init_ui()
        self.init_signals()
        self.init_timer()
        self.restore_geometry()
        # 稍后询问是否恢复上次会话，确保窗口先显示出来
        QTimer.singleShot(400, self.maybe_restore_sessions)
        # 启动后后台把资料库索引与内存索引都准备好，这样一提到老需求就能立刻匹配。
        # 推迟到 5s 再开始：启动头几秒用户就在点"连接设备 / 翻设备库"，而索引构建是
        # 纯 Python 的 CPU 活（攥着 GIL），太早开始会让界面"点什么都卡一下"
        # （2026-10-17 用户反馈）。
        self.update_knowledge_status()
        # 资料库索引**不再"启动即建"**：`.index.json` 本机实测 676 MB，读成 Python
        # 对象后常驻内存好几 GB，而用户多数时候并不用资料库 —— 要跑 EVE-NG / eNSP
        # 这类模拟器时，这些内存就是白占（实测主进程曾到 7.9 GB）。
        # 默认改成"用到才建"：打开资料管理、导入资料、AI 检索时都会自动建/加载。
        # 想恢复"启动就建"：设置里 knowledge_autobuild = true。
        if self.settings.get("knowledge_autobuild", False):
            QTimer.singleShot(5000, lambda: self.build_knowledge_index_bg(force=False))
        # 串口枚举要走 Windows setupapi（插着 USB 转串口时首次几百毫秒），提前在后台
        # 扫一遍：用户点"连接设备"时下拉框是现成的，不在打开对话框时同步枚举
        self.serial_scanner = SerialPortScanner(self)
        QTimer.singleShot(1200, self.serial_scanner.refresh)
        # 外部接管（DSH / MCP）：默认关闭；开了的机器启动后自动把回环控制服务拉起来
        self.control_server = None
        if self.settings.get("control_api_enabled", False):
            QTimer.singleShot(1500, lambda: self.set_control_enabled(True, remember=False))
        # 本地模型：默认**不**自动起（只标注档位）。要"软件一起来就在 GPU 上跑着"，
        # 就把设置里的 local_model_autostart 打开 —— 模型是起在**显存**上的
        # （Vulkan 后端，实测 RTX 3060 Laptop：VRAM 1051 → 4094 MiB，不占内存），
        # AI 提供方选 local 时立刻可用。
        if self.settings.get("local_model_autostart", False):
            QTimer.singleShot(6000, self.start_local_model_bg)

    # ------------------------------------------------------------ 外部接管（DSH/MCP）
    # 谁在用：core/control_api.py 的回环 HTTP 服务 + 随包的 MCP 桥
    # （data_tools/mcp/aiterminal_mcp.py）。DSH 侧只读 ~/.aiterminal/control.json
    # 就能发现地址与令牌，不需要知道端口是哪个。
    def control_state(self) -> dict:
        """给 AI 设置对话框看的状态"""
        server = self.control_server
        return {
            "enabled": bool(server and server.running),
            "url": server.url if server else "",
            "token": server.token if server else "",
        }

    def set_control_enabled(self, enabled: bool, remember: bool = True) -> bool:
        """开/关本地控制通道（只绑回环 + 令牌）。返回是否已开启。"""
        if remember:
            self.settings.set("control_api_enabled", bool(enabled))
            self.settings.save()
        if enabled:
            if self.control_server is None:
                from PyQt6.QtWidgets import QApplication
                from core.control_api import ControlServer

                app = QApplication.instance()
                self.control_server = ControlServer(
                    self.session_manager,
                    app_version=(app.applicationVersion() if app else ""),
                    knowledge=getattr(self.ai_panel, "kb", None),
                )
            try:
                url = self.control_server.start()
            except Exception as e:
                self.control_server.stop()
                self.control_server = None
                QMessageBox.warning(self, "外部接管启动失败",
                                    f"{type(e).__name__}: {e}")
                return False
            self.status_bar.showMessage(
                f"外部接管已开启：{url}（令牌见「工具 → AI 设置」）", 8000)
            self.warm_knowledge_for_control()
            return True
        if self.control_server is not None:
            self.control_server.stop()
            self.status_bar.showMessage("外部接管已关闭", 4000)
        return False

    def reset_control_token(self) -> str:
        """重置令牌（旧令牌立刻失效），返回新令牌"""
        if self.control_server is None:
            return ""
        return self.control_server.reset_token()

    def warm_knowledge_for_control(self):
        """后台先把知识库索引建好，别让接管方第一次查资料就撞超时。

        实测（本机 27,297 篇 / 782 MB 索引）：`/knowledge/stats` 冷启动要 **72.7 秒**
        （现建索引），而 MCP 桥的普通请求超时是 30 秒 ⇒ 接管方第一次查资料必失败。
        控制通道一开就丢个守护线程去建，建好后 control_api 会复用同一个实例，
        之后所有 /knowledge/* 调用都是秒回。

        ⚠ 但这要**常驻约 0.9 GB 内存**（索引 676 MB 读成 Python 对象），而用户的
        首要诉求是"把内存降下来好跑模拟器" ⇒ 默认**不**预热（同一个 knowledge_autobuild
        开关控制）。不预热也不影响正确性：桥的超时已放宽到 300 秒，第一次查资料
        只是慢（几十秒），不会失败。要秒回就把 knowledge_autobuild 打开。
        """
        if not self.settings.get("knowledge_autobuild", False):
            return
        import threading

        def worker():
            time.sleep(8)                      # 先让主窗口画出来，别抢启动时的 CPU
            try:
                kb = self.session_manager._ensure_knowledge()
                if kb is not None:
                    # 检索侧还有一层懒初始化（倒排/向量），首次检索实测 60.9 秒 ——
                    # 顺手用一句废话把它也热起来，否则接管方第一次检索照样撞超时
                    kb.search("display version", top_k=1)
            except Exception:
                pass                           # 预热失败不影响任何功能，用到时再建

        threading.Thread(target=worker, name="control-kb-warm", daemon=True).start()

    # ------------------------------------------------------------ 界面
    def build_welcome_page(self) -> QWidget:
        """中央区的空状态：还没开会话时给一页引导（像 IDE 的欢迎页）

        以前没有会话时中间就是一大片空的，用户不知道该干什么。
        这里把最常用的几个入口和快捷键摆出来，照着做就行。
        """
        page = QWidget()
        page.setObjectName("WelcomePage")
        outer = QHBoxLayout(page)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)

        column = QWidget()
        column.setMaximumWidth(620)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addStretch(1)

        title = QLabel("NetOps Terminal")
        title.setObjectName("WelcomeTitle")
        layout.addWidget(title)

        sub = QLabel("网络设备智能终端 · SSH / Telnet / Console")
        sub.setObjectName("WelcomeSub")
        layout.addWidget(sub)
        layout.addSpacing(20)

        # 给一个明确的行动入口，比只写 "Ctrl+N" 更好上手
        action_row = QHBoxLayout()
        btn_new = QPushButton("新建连接")
        btn_new.setProperty("primary", "true")
        btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_new.setMinimumHeight(30)
        btn_new.clicked.connect(self.show_connect_dialog)
        action_row.addWidget(btn_new)
        action_row.addStretch()
        layout.addLayout(action_row)
        layout.addSpacing(22)

        for key, text in (
            ("Ctrl+N", "新建连接"),
            ("双击设备库里的设备", "直接连接（自动补全凭据）"),
            ("Ctrl+F", "在设备输出里查找（F3 下一个）"),
            ("Ctrl+= / Ctrl+-", "缩放终端字号"),
            ("左侧 ✦ 图标", "开关 AI 助手面板（Ctrl+I 是 AI 自动模式）"),
            ("右键设备 / 会话标签", "编辑、断开、删除"),
        ):
            row = QHBoxLayout()
            row.setSpacing(12)
            key_label = QLabel(key)
            key_label.setObjectName("WelcomeKey")
            key_label.setFixedWidth(150)
            row.addWidget(key_label)
            hint = QLabel(text)
            hint.setObjectName("WelcomeHint")
            # 终端区被面板挤窄时提示文字要能折行，否则会跟左边的快捷键叠在一起
            hint.setWordWrap(True)
            row.addWidget(hint, 1)
            layout.addLayout(row)

        layout.addStretch(2)
        outer.addWidget(column)
        outer.addStretch(3)
        return page

    def _refresh_central_page(self):
        """有会话就显示页签，没有就回到引导页"""
        self.tab_stack.setCurrentIndex(1 if self.tab_widget.count() > 0 else 0)

    def init_ui(self):
        """初始化界面"""
        self.setWindowTitle("NetOps Terminal · 网络运维终端 v0.7")
        self.setGeometry(100, 100, 1400, 900)

        # 中央区：没开会话时给一页引导（像 IDE 的欢迎页），开会话后换成页签
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.setMovable(True)
        self.tab_widget.tabCloseRequested.connect(self.close_session)
        self.tab_widget.currentChanged.connect(self.on_tab_changed)

        self.welcome_page = self.build_welcome_page()
        self.tab_stack = QStackedWidget()
        self.tab_stack.addWidget(self.welcome_page)   # 0
        self.tab_stack.addWidget(self.tab_widget)      # 1
        # tab_stack 待会儿作为 QSplitter 的右栏，这里先不设成中央区。
        # 它的欢迎页里有几行不换行的长文本，minimumSizeHint 有 500 多，
        # 窗口一窄就会把两侧面板挤到最小宽度 —— 设成 Ignored 允许被压缩，
        # 保底下限交给 _show_split_panel 的 min_terminal 管
        self.tab_stack.setSizePolicy(QSizePolicy.Policy.Ignored,
                                     QSizePolicy.Policy.Expanding)
        # 但中间栏必须有个**真实下限**：设成 Ignored 之后它的有效最小宽是 0，
        # AI 面板一开大就把欢迎页连同"新建连接"按钮一起压出可视区（用户截图）。
        # explicit 的 minimumWidth 不受 Ignored 影响 —— 实测有效。
        self.tab_stack.setMinimumWidth(360)

        # 最左边：工具窗口条（只有图标的一列，像 IDE）
        self.rail = NavRail(self)
        self.rail.item_selected.connect(self.on_rail_item)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, self.rail)

        # 三栏用一个 QSplitter 摆：设备库 | AI 面板 | 终端
        #
        # 以前是两个 QDockWidget，但 Qt 给 dock 之间的分隔条热区只有 4px
        # （QSS 里再调宽，视觉上就是一条粗黑杠），鼠标几乎抓不住 ——
        # 用户反馈的"拖拽没把面板变大"就是这个原因。换成 QSplitter 后
        # 热区宽度由 setHandleWidth 控制，抓得稳、样式也好调。
        self.vault_panel = VaultPanel()
        self.vault_panel.setMinimumWidth(170)

        self.ai_panel = AIPanel()
        self.chat_view = ChatView()
        self.right_tabs = QTabWidget()
        self.right_tabs.addTab(self.chat_view, "Catty 对话")
        self.right_tabs.addTab(self.ai_panel, "AI 高级")
        # 只有 AI 面板走"宽度归零"收起，所以要 Ignored 才收得干净；
        # 设备库是整块 show/hide，用默认策略（用了 Ignored 会被压得比最小宽度还窄）
        self.right_tabs.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Expanding)
        # 面板没有标题栏，在页签栏右侧放一个 ✕ 作为关闭入口
        corner = QWidget()
        corner_lay = QHBoxLayout(corner)
        corner_lay.setContentsMargins(0, 0, 4, 0)
        corner_lay.setSpacing(0)
        self.btn_close_ai = QToolButton()
        self.btn_close_ai.setObjectName("AICloseButton")
        self.btn_close_ai.setText("✕")
        self.btn_close_ai.setToolTip("关闭 AI 助手面板")
        self.btn_close_ai.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close_ai.setFixedSize(20, 20)
        self.btn_close_ai.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_close_ai.setStyleSheet(
            "QToolButton#AICloseButton { color: #93A0B4; background: transparent;"
            " border: none; font-size: 12px; }"
            "QToolButton#AICloseButton:hover { color: #ffffff;"
            " background: rgba(224, 108, 117, 0.32); border-radius: 4px; }")
        # 面板上的 ✕ 与左侧图标条的勾选态保持同步
        self.btn_close_ai.clicked.connect(
            lambda: self.set_ai_panel_visible(False))
        corner_lay.addWidget(self.btn_close_ai)
        self.right_tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)

        self.side_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.side_splitter.setObjectName("SideSplitter")
        self.side_splitter.setHandleWidth(6)             # 拖拽热区，6px 好抓
        # 允许折叠到 0：侧栏收起就是"折叠"，用户也能直接把面板拖没
        self.side_splitter.setChildrenCollapsible(True)
        self.side_splitter.addWidget(self.vault_panel)    # 0 设备库
        self.side_splitter.addWidget(self.right_tabs)     # 1 AI 面板
        self.side_splitter.addWidget(self.tab_stack)      # 2 终端
        # 拉伸窗口时只让终端变宽，两侧面板保持用户拖出来的宽度
        self.side_splitter.setStretchFactor(0, 0)
        self.side_splitter.setStretchFactor(1, 0)
        self.side_splitter.setStretchFactor(2, 1)
        self.setCentralWidget(self.side_splitter)

        # 收起 / 展开时用的宽度记忆（0 设备库 / 1 AI 面板）与开关状态
        self._panel_last_width = {0: 280, 1: 0}
        self._vault_open = True
        self._ai_open = False

        # AI 面板**默认收起**：压成 0 宽（否则设备库那侧的分隔条会被 Qt 一起藏掉），
        # 页签栏同时隐掉 —— 不然 0 宽时它的页签溢出按钮会在窗口角落画出一个 "+"
        self.right_tabs.setMinimumWidth(0)
        self.right_tabs.tabBar().setVisible(False)
        # AI 面板收起时把分隔条禁掉：收起只是"宽度归零"，鼠标仍然能把它拖出来
        # （用户反馈："即便关闭通过拖拽还是可以打开"）。禁用后拖不动，实测有效。
        self.side_splitter.handle(2).setEnabled(False)
        self.side_splitter.setSizes([280, 0, 900])
        # 直接拖分隔条也能开合面板，图标勾选态 / 页签栏要跟着变
        self.side_splitter.splitterMoved.connect(self.on_splitter_moved)

        # 用已保存的设备库初始化左侧 Vault
        self.vault_panel.set_devices(self.settings.get_vault_devices())

        self.sync_rail_state()

        # 工具栏与菜单
        self.init_actions()
        self.init_menu()

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")
        self.status_session = QLabel("无活动会话")
        self.status_safety = QLabel("安全闸门: 开")
        self.status_api = QLabel("API: 未配置")
        self.status_ai = QLabel("AI: OFF")
        self.status_bar.addPermanentWidget(self.status_session)
        self.status_bar.addPermanentWidget(self.status_safety)
        self.status_bar.addPermanentWidget(self.status_api)
        self.status_bar.addPermanentWidget(self.status_ai)

        # 应用样式表
        self.apply_style()

        # 启动时按已保存的凭据初始化 API 状态指示（未验证态）
        self.update_api_status()
        # 安全闸门状态常驻显示
        self.update_safety_status()

    def apply_style(self):
        """载入 QSS 样式表，缺失时使用内置暗色样式

        QSS 里的 url() 相对路径是按"当前工作目录"解析的（用字符串加载样式表
        没有基准目录），换个目录启动程序图标就全丢了。这里把图标路径统一换成
        绝对路径，样式表本身仍写成相对形式，方便阅读。
        """
        qss_path = os.path.join(BASE_DIR, "resources", "styles", "dark.qss")
        if os.path.exists(qss_path):
            try:
                with open(qss_path, 'r', encoding='utf-8') as f:
                    qss = f.read()
                icons = os.path.join(BASE_DIR, "resources", "icons").replace("\\", "/")
                self.setStyleSheet(qss.replace("url(../icons/", f"url({icons}/"))
                return
            except Exception:
                pass
        self.setStyleSheet(THEME_QSS)

    def init_actions(self):
        """
        创建需要跨方法引用的 QAction（**不再建工具栏**）。

        原来的工具栏被取消了 —— 那 14 个按钮里有 12 个在菜单中已有同名项，
        属于纯重复；剩下 2 个独有的（AI分析一次 / 清屏）已补进对应菜单。
        这里只留两个需要被外部 setText / setEnabled 的 action。
        """
        # AI 模式开关：状态要在多处同步（setText / setChecked）
        self.action_ai = QAction("AI: OFF", self)
        self.action_ai.setCheckable(True)
        self.action_ai.setShortcut("Ctrl+I")
        self.action_ai.setToolTip("启用 / 关闭当前会话的 AI 辅助")
        self.action_ai.triggered.connect(self.toggle_ai_mode)

        # 串口控制：仅 Console 会话可用，切换会话时要 setEnabled
        self.action_serial = QAction("串口控制（Break / DTR / RTS）…", self)
        self.action_serial.setToolTip("Break / DTR / RTS / 切换波特率 / 重新连接")
        self.action_serial.setEnabled(False)   # 仅 Console 会话可用
        self.action_serial.triggered.connect(self.open_serial_control)

    def init_menu(self):
        """
        初始化菜单栏 —— 所有功能入口都收在这里（已取消工具栏）。

        结构：文件 / 会话 / 视图 / 工具 / AI / 资料 / 帮助
        其中「视图」和「资料」是新建的：原来这两类操作挤在工具栏和工具菜单里，
        没有合适的归属，单独成菜单更清楚。
        """
        menubar = self.menuBar()

        # ---------------------------------------------------------- 文件
        file_menu = menubar.addMenu("文件(&F)")
        act_new = QAction("新建连接…", self)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(self.show_connect_dialog)
        file_menu.addAction(act_new)
        act_log = QAction("保存当前日志…", self)
        act_log.triggered.connect(self.save_current_log)
        file_menu.addAction(act_log)
        file_menu.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        # ---------------------------------------------------------- 会话
        session_menu = menubar.addMenu("会话(&S)")
        act_close = QAction("关闭当前会话", self)
        act_close.triggered.connect(self.disconnect_current)
        session_menu.addAction(act_close)
        act_close_all = QAction("关闭全部会话", self)
        act_close_all.triggered.connect(self.close_all_sessions)
        session_menu.addAction(act_close_all)
        session_menu.addSeparator()
        act_restore = QAction("恢复上次的会话…", self)
        act_restore.triggered.connect(self.restore_last_sessions)
        session_menu.addAction(act_restore)

        # ---------------------------------------------------------- 视图（新建）
        view_menu = menubar.addMenu("视图(&V)")
        act_clear = QAction("清屏", self)
        act_clear.setShortcut("Ctrl+K")
        act_clear.setToolTip("清空当前终端显示内容")
        act_clear.triggered.connect(self.clear_current)
        view_menu.addAction(act_clear)
        view_menu.addSeparator()
        act_toggle_vault = QAction("显示/隐藏 设备库面板", self)
        act_toggle_vault.triggered.connect(
            lambda: self.set_vault_visible(not self.vault_panel.isVisible()))
        view_menu.addAction(act_toggle_vault)
        act_toggle_ai = QAction("显示/隐藏 AI 面板", self)
        # 与图标条走同一个入口，勾选态不会和面板可见性脱节。
        # 判据必须是 self._ai_open：AI 面板收起是"宽度归零"，right_tabs.isVisible()
        # 恒为 True —— 用它判断会让这一项**永远只会收起、再也打不开**
        # （2026-10-21 gui_drive_test 的 G11 就是这么挂的：既没切换也没提示）。
        act_toggle_ai.triggered.connect(
            lambda: self.set_ai_panel_visible(not self._ai_open))
        view_menu.addAction(act_toggle_ai)

        # ---------------------------------------------------------- 工具
        tool_menu = menubar.addMenu("工具(&T)")

        # 安全闸门放最前面 —— 这是最要紧的开关，不该藏在菜单底部
        self.act_confirm_config = QAction("配置命令下发前需确认", self)
        self.act_confirm_config.setCheckable(True)
        self.act_confirm_config.setChecked(self.settings.confirm_config_commands)
        self.act_confirm_config.setToolTip(
            "开启后，包含配置类命令的批次会先弹出命令清单等你确认；\n"
            "AI 自动模式下生成的配置命令只打到终端里给你过目，不会自动下发"
        )
        self.act_confirm_config.triggered.connect(self.on_confirm_config_toggled)
        tool_menu.addAction(self.act_confirm_config)

        tool_menu.addSeparator()

        act_tpl = QAction("命令模板与批量下发…", self)
        act_tpl.triggered.connect(self.show_template_dialog)
        tool_menu.addAction(act_tpl)

        act_patrol = QAction("多设备并发巡检…（结果导出 Excel）", self)
        act_patrol.triggered.connect(self.open_patrol_dialog)
        tool_menu.addAction(act_patrol)

        act_batch_export = QAction("导出上次批量下发结果…", self)
        act_batch_export.triggered.connect(self.export_batch_result)
        tool_menu.addAction(act_batch_export)

        # 要跑模拟器（EVE-NG / eNSP）等吃内存的程序时，一键把资料库索引腾出去
        act_free = QAction("释放内存（卸载资料库索引）", self)
        act_free.setToolTip("把已加载的资料库索引从内存里丢掉；索引仍在磁盘上，"
                            "下次检索会自动重新加载")
        act_free.triggered.connect(self.release_knowledge_memory)
        tool_menu.addAction(act_free)

        act_diff = QAction("配置差异比对…", self)
        act_diff.triggered.connect(self.show_diff_dialog)
        tool_menu.addAction(act_diff)

        tool_menu.addSeparator()

        act_snapshot = QAction("采集配置快照", self)
        act_snapshot.triggered.connect(self.take_snapshot)
        tool_menu.addAction(act_snapshot)

        act_snapshot_cmp = QAction("与上次快照对比", self)
        act_snapshot_cmp.triggered.connect(self.compare_snapshots)
        tool_menu.addAction(act_snapshot_cmp)

        tool_menu.addSeparator()

        # 串口控制（仅 Console 会话可用，用共享的 action 以便外部 setEnabled）
        tool_menu.addAction(self.action_serial)

        tool_menu.addSeparator()

        act_prompt = QAction("编辑系统提示词…", self)
        act_prompt.triggered.connect(self.edit_system_prompt)
        tool_menu.addAction(act_prompt)

        self.act_ai_settings = QAction("AI 设置…", self)
        self.act_ai_settings.setShortcut("Ctrl+,")
        self.act_ai_settings.setToolTip(
            "配置 AI 提供方 / API Key / 接口地址 / 模型，并可测试连接")
        self.act_ai_settings.triggered.connect(self.open_ai_settings)
        tool_menu.addAction(self.act_ai_settings)

        tool_menu.addSeparator()

        self.act_auto_log = QAction("自动记录会话日志", self)
        self.act_auto_log.setCheckable(True)
        self.act_auto_log.setChecked(self.settings.auto_log)
        self.act_auto_log.triggered.connect(self.on_auto_log_toggled)
        tool_menu.addAction(self.act_auto_log)

        act_open_logs = QAction("打开日志目录", self)
        act_open_logs.triggered.connect(self.open_log_dir)
        tool_menu.addAction(act_open_logs)

        # ---------------------------------------------------------- AI
        ai_menu = menubar.addMenu("AI(&A)")
        # 直接用共享 action（带勾选态，文字随会话在 AI: ON / OFF 间切换）
        ai_menu.addAction(self.action_ai)
        act_step = QAction("AI 分析一次", self)
        act_step.setToolTip("对当前屏幕输出跑一次分析（不开启自动模式）")
        act_step.triggered.connect(self.analyze_once)
        ai_menu.addAction(act_step)
        ai_menu.addSeparator()
        act_reset = QAction("清空 AI 上下文", self)
        act_reset.triggered.connect(self.reset_ai_context)
        ai_menu.addAction(act_reset)

        # ---------------------------------------------------------- 帮助
        help_menu = menubar.addMenu("帮助(&H)")
        act_about = QAction("关于 NetOps Terminal", self)
        act_about.triggered.connect(self.show_about)
        help_menu.addAction(act_about)

        # 右上角原本还有一个「AI 助手」开关按钮，现在左侧工具窗口条上有 ✦ 图标，
        # 两处入口重复，已去掉 —— 面板开关统一走图标条 / 视图菜单。

    def showEvent(self, event):
        """窗口显示后落实一次侧栏宽度，并同步图标条勾选态

        窗口显示之前拿不到真实尺寸（setSizes 的绝对值会被重新分配），
        isVisible() 也恒为 False，会把设备库误判成"已收起"。
        """
        super().showEvent(event)
        if not getattr(self, "_split_sized", False):
            self._split_sized = True
            self._apply_panel_widths()
        self.sync_rail_state()

    def _apply_panel_widths(self):
        """把记录的侧栏宽度落实到 splitter（首次显示时用）"""
        total = sum(self.side_splitter.sizes())
        vault = self._panel_last_width.get(0, 280) if self._vault_open else 0
        ai = self._panel_last_width.get(1, 0) if self._ai_open else 0
        self.side_splitter.setSizes([vault, ai, max(240, total - vault - ai)])

    def on_rail_item(self, key: str):
        """左侧工具窗口条：点图标开/关对应面板"""
        if key == "devices":
            self.set_vault_visible(not self._vault_open)
        elif key == "ai":
            self.set_ai_panel_visible(not self._ai_open)
        elif key == "knowledge":
            self.open_knowledge_dialog()
        # 可见性变化会把状态同步回图标条；"资料库"这种纯动作不影响图标态
        self.sync_rail_state()

    def sync_rail_state(self):
        """把面板的可见状态同步到左侧图标条（不会反过来触发开关）"""
        self.rail.set_checked("devices", self._vault_open)
        self.rail.set_checked("ai", self._ai_open)

    def on_splitter_moved(self, _pos: int, _index: int):
        """用户直接拖分隔条时，面板的开合状态要跟着走"""
        self._sync_ai_open_state()
        self.sync_rail_state()

    def _sync_ai_open_state(self):
        """按 AI 面板的实际宽度回写开关状态（页签栏和最小宽度一起跟着走）"""
        opened = self.side_splitter.sizes()[1] > 0
        if opened == self._ai_open:
            return
        self._ai_open = opened
        self.right_tabs.setMinimumWidth(280 if opened else 0)
        self.right_tabs.tabBar().setVisible(opened)

    # ------------------------------------------------------------ 面板显隐
    # 三栏都挂在 self.side_splitter 上：索引 0 设备库 / 1 AI 面板 / 2 终端。
    #
    # 两种面板的收起方式不同，是被 Qt 的两条行为逼出来的（都实测过）：
    #   · AI 面板用"宽度归零"：QSplitter 在某一栏不可见时，会把紧挨它的
    #     拖动条一起藏掉 —— 用 setVisible(False) 的话，AI 面板一收起，
    #     设备库就没法拖宽了。压成 0 宽后拖动条还在，位置也不动。
    #   · 设备库用整块 show/hide：它在最左边，收起来不妨碍谁拖动；而"宽度
    #     归零"要求 sizePolicy 设成 Ignored，那会让它被压得比最小宽度还窄
    #     （搜索框和"新建"按钮会被挤出面板）。
    def _show_split_panel(self, index: int, widget, width: int,
                          min_terminal: int = 360):
        """把 splitter 第 index 栏展开到 width 宽，空间从终端那栏借"""
        sizes = self.side_splitter.sizes()
        width = max(160, min(width, max(160, sizes[2] - min_terminal)))
        sizes[index] = width
        sizes[2] = max(min_terminal, sizes[2] - width)
        self.side_splitter.setSizes(sizes)

    def set_vault_visible(self, visible: bool):
        """开关设备库面板（图标条 / 视图菜单都走这里）"""
        visible = bool(visible)
        if visible == self._vault_open:
            self.sync_rail_state()
            return
        if visible:
            self.vault_panel.setVisible(True)
            self._vault_open = True
            self._show_split_panel(0, self.vault_panel,
                                   self._panel_last_width.get(0) or 280)
        else:
            self._panel_last_width[0] = self.side_splitter.sizes()[0] or 280
            self.vault_panel.setVisible(False)
            self._vault_open = False
        self.sync_rail_state()

    def set_ai_panel_visible(self, visible: bool):
        """开关 AI 助手面板（图标条、视图菜单、面板上的 ✕ 都走这里）"""
        visible = bool(visible)
        if visible == self._ai_open:
            self.sync_rail_state()
            return
        sizes = self.side_splitter.sizes()
        if visible:
            self.right_tabs.setMinimumWidth(280)
            self.right_tabs.tabBar().setVisible(True)
            self.side_splitter.handle(2).setEnabled(True)   # 能拖了
            self._ai_open = True
            # 首次打开按窗口宽度的三分之一给宽度，夹在 340~560；之后尊重用户拖的
            default = min(560, max(340, int(self.width() * 0.34)))
            self._show_split_panel(1, self.right_tabs,
                                   self._panel_last_width.get(1) or default)
        else:
            if sizes[1] > 0:
                self._panel_last_width[1] = sizes[1]
            sizes[2] += sizes[1]
            sizes[1] = 0
            self.side_splitter.setSizes(sizes)
            self.right_tabs.setMinimumWidth(0)
            # 0 宽时它的页签溢出按钮会在窗口角落画出一个小符号，一并隐掉
            self.right_tabs.tabBar().setVisible(False)
            self.side_splitter.handle(2).setEnabled(False)  # 收起后拖不动
            self._ai_open = False
        self.sync_rail_state()

    def init_signals(self):
        """连接会话管理器与AI面板的信号"""
        # 工作线程里对 chat_view 的操作一律通过这个信号排队回主线程执行
        self.chat_ui_call.connect(self.on_chat_ui_call)

        sm = self.session_manager
        sm.session_created.connect(self.on_session_created)
        sm.session_closed.connect(self.on_session_closed)
        sm.session_error.connect(self.on_session_error)
        sm.ai_output.connect(self.on_ai_output)
        sm.ai_command.connect(self.on_ai_command)
        sm.ai_research_ready.connect(self.on_research_ready)
        sm.connection_info.connect(self.on_connection_info)
        sm.connection_lost.connect(self.on_connection_lost)

        self.ai_panel.credentials_changed.connect(self.on_credentials_changed)
        self.ai_panel.connection_tested.connect(self.on_api_connection_tested)
        self.ai_panel.research_requested.connect(self.on_research_requested)
        self.ai_panel.search_config_changed.connect(self.on_search_config_changed)
        self.ai_panel.import_knowledge_requested.connect(self.import_knowledge_files)
        self.ai_panel.manage_knowledge_requested.connect(self.open_knowledge_dialog)
        self.ai_panel.save_note_requested.connect(self.on_save_note)

        # 左侧设备库 Vault
        self.vault_panel.connect_requested.connect(self.on_vault_connect)
        self.vault_panel.disconnect_requested.connect(self.on_vault_disconnect)
        self.vault_panel.add_requested.connect(self.show_connect_dialog)
        self.vault_panel.edit_requested.connect(self.on_vault_edit)
        self.vault_panel.delete_requested.connect(self.on_vault_delete)
        self.vault_panel.selection_changed.connect(self.on_vault_selection)

        # 右侧 Catty 对话面板
        self.chat_view.message_sent.connect(self.on_chat_message)
        self.chat_view.execute_requested.connect(self.on_chat_execute)
        self.chat_view.explain_requested.connect(self.on_chat_explain)
        self.chat_view.research_requested.connect(self.on_chat_research)
        self.chat_view.clear_requested.connect(lambda: self.chat_view.clear())

    def init_timer(self):
        """初始化定时器"""
        # 30ms 刷新终端显示（从连接器缓冲区取数据）
        # 100ms 时打字回显与长输出会明显一顿一顿的（每批最多等 100ms 才上屏）
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_output)
        self.timer.start(30)

        # 1s 驱动 AI 自动模式
        self.ai_timer = QTimer()
        self.ai_timer.timeout.connect(self.ai_tick)
        self.ai_timer.start(1000)

        # 1s 检查设备型号探测的回显是否吐完（据此解析型号/版本）
        self.version_timer = QTimer()
        self.version_timer.timeout.connect(self.check_device_version)
        self.version_timer.start(1000)

        # 界面线程卡顿看门狗：这个定时器本该每 500ms 跑一次，若两次之间
        # 隔了很久，说明界面线程被某处阻塞了（曾出现"连打 Tab 后卡死"）。
        # 打日志是为了下次能直接看到卡在哪一步，正常情况不会输出。
        self._watch_tick = time.monotonic()
        self.stall_timer = QTimer()
        self.stall_timer.timeout.connect(self.check_stall)
        self.stall_timer.start(500)

    def check_stall(self):
        """界面线程被长时间阻塞时留下证据（写日志文件，见 log_slow）"""
        now = time.monotonic()
        gap = now - self._watch_tick
        self._watch_tick = now
        if gap > 2.0:
            self.log_slow(f"界面线程被阻塞 {gap:.1f}s")

    def log_slow(self, message: str, tag: str = "卡顿"):
        """把卡顿/慢操作写进 `<程序目录>\\aiterminal-slow.log`

        为什么落盘而不只 print：打包版是 windowed（console=False），**没有
        stderr** —— 用户报"卡一下"时，只 print 的话现场什么都看不到。有秒数和
        时刻，才谈得上定位是哪一步慢（2026-10-17 加）。
        """
        print(f"[{tag}] {message}", file=sys.stderr, flush=True)
        try:
            from core import app_paths                            # noqa: WPS433
            with open(os.path.join(app_paths.base_dir(), "aiterminal-slow.log"),
                      "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [{tag}] {message}\n")
        except Exception:                                         # noqa: BLE001
            pass

    # ------------------------------------------------------------ 会话
    def show_connect_dialog(self):
        """显示连接对话框"""
        dialog = ConnectDialog(self)
        if dialog.exec():
            config = dialog.get_config()
            self.create_session(config)

    def create_session(self, config: dict):
        """创建新会话（连接在工作线程里跑，界面不冻结）

        ⚠ 这里**不能**直接调 `session_manager.create_session`：它是同步
        `connect()`，会把 GUI 线程按住直到握手结束（SSH 默认 10s 超时、
        Telnet 等登录提示、串口等驱动），用户看到的就是"点了连接就卡死"。
        线程跑完后由 `session_created` / `session_error` 信号回到主线程建标签页
        或报错，界面全程可用（状态栏 + 忙碌光标给出反馈）。
        """
        if self._connect_task is not None and self._connect_task.isRunning():
            self.status_bar.showMessage("上一个连接还在进行中，请稍候…", 3000)
            return
        target = config.get('host') or config.get('serial_port') or ''
        self.status_bar.showMessage(f"正在连接 {target} …（界面可继续操作）")
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)

        task = ConnectWorker(self.session_manager, {
            'name': config.get('name', '新建连接'),
            'protocol': config.get('protocol', 'SSH'),
            'host': config.get('host', ''),
            'username': config.get('username', ''),
            'password': config.get('password', ''),
            'port': config.get('port', 22),
            # eNSP Pro：设备命令行走 HTTP+STOMP（没有 TCP 端口），
            # 靠沙箱/设备编号定位 console，必须一起带进会话
            'sandbox_id': config.get('sandbox_id', 0),
            'node_id': config.get('node_id', 0),
            'serial_port': config.get('serial_port', ''),
            'baudrate': config.get('baudrate', 9600),
            'key_filename': config.get('key_filename'),
            # 串口 / Console 参数
            'bytesize': config.get('bytesize', 8),
            'parity': config.get('parity', 'N'),
            'stopbits': config.get('stopbits', 1),
            'flow': config.get('flow', 'none'),
            'dtr': config.get('dtr'),
            'rts': config.get('rts'),
            'line_ending': config.get('line_ending'),
            'char_delay_ms': config.get('char_delay_ms', 0),
            'auto_reconnect': config.get('auto_reconnect', True),
            'reconnect_interval': config.get('reconnect_interval', 2.0),
            'local_echo': config.get('local_echo', False),
        }, self)
        task.finished.connect(lambda t=task: self.on_connect_task_done(t))
        self._connect_task = task
        task.start()

    def on_connect_task_done(self, task):
        """连接线程结束：恢复光标与状态栏，耗时记进慢日志"""
        QApplication.restoreOverrideCursor()
        if self._connect_task is task:
            self._connect_task = None
        ok = task.session_id > 0
        self.log_slow(f"连接耗时 {task.cost:.1f}s（成功={ok}）", tag="连接")
        if ok:
            self.status_bar.showMessage(f"已连接（{task.cost:.1f}s）")
        else:
            reason = (task.error
                      or getattr(self.session_manager, 'last_connect_error', '')
                      or "连接失败")
            self.status_bar.showMessage(f"连接失败：{reason}", 8000)
        task.deleteLater()

    def on_session_created(self, session_id: int, name: str):
        """会话创建成功：建立标签页并接线"""
        session = self.session_manager.get_session(session_id)
        info = session.connector.info() if session and session.connector else ""
        tab = SessionTab(session_id, name, info)
        self.tabs[session_id] = tab

        tab.command_entered.connect(
            lambda cmd, sid=session_id: self.on_command_entered(sid, cmd)
        )
        tab.interrupt_requested.connect(
            lambda sid=session_id: self.on_interrupt(sid)
        )
        tab.raw_input.connect(
            lambda text, sid=session_id: self.on_raw_input(sid, text)
        )
        tab.echo_auto_off.connect(
            lambda sid=session_id: self.on_local_echo_auto_off(sid)
        )

        index = self.tab_widget.addTab(tab, name)
        self.tab_widget.setCurrentIndex(index)
        self._install_tab_close_button(tab, index)
        self._refresh_central_page()
        # setCurrentIndex 会把焦点吸到 QTabBar 上，导致用户连上后敲键盘
        # 全进标签栏、终端一片空白（本地回显也看不见）。必须显式把焦点
        # 交还给终端输入区
        tab.terminal.setFocus()
        self.session_manager.set_active_session(session_id)
        self.update_status()

        # Console 会话：按配置开启本地回显，并提示串口控制入口
        session = self.session_manager.get_session(session_id)
        if session and session.connector:
            # 逐字符实时下发时，回车下发连接器自己的行结束符（串口 CR / SSH LF）
            tab.terminal.line_ending = session.connector.line_ending or "\r"
        if session and session.protocol == "Serial":
            local_echo = bool(session.config.get('local_echo'))
            tab.terminal.set_local_echo(local_echo)
            tab.set_status(True, f"{session.connector.info()}"
                                 + ("（本地回显）" if local_echo else ""))
            # 串口设备不会主动重发提示符，不唤醒的话终端会一直空白，
            # 用户会以为"必须按回车才显示内容"。连接成功后自动补一个回车，
            # 让设备把当前提示符吐出来（注意：若设备行上有残留的半截命令，
            # 这个回车会把它执行掉，所以先确认用户没在敲）
            QTimer.singleShot(
                300, lambda sid=session_id: self.wake_up_console(sid))
            # 唤醒回车后设备仍一个字都不吐 ⇒ 它不回显，自动开本地回显，
            # 否则用户敲键盘屏幕上没有任何变化（见 check_console_echo）
            QTimer.singleShot(
                1700, lambda sid=session_id: self.check_console_echo(sid))
            self.on_ai_output(
                session_id,
                "[提示] Console 会话已建立。若设备无回显请开启“本地回显”，"
                "需进 BootROM 可用工具栏“串口控制”发送 Break。"
            )

        # 把新连上的设备登记进左侧 Vault（去重，不存密码），并同步聊天面板设备条
        if session:
            self.register_vault_device(session)
            self.sync_chat_device(session)
            # 资料库里（含手册整理出的）命令先装进建议表，插上就有提示可用
            self.load_kb_commands_into_sessions()
            # 认一下设备型号/版本：据此把命令建议换成本机准确的命令
            QTimer.singleShot(
                800, lambda sid=session_id: self.probe_device_version(sid))

        self.sync_session_context()

    # ------------------------------------------------------------ 设备型号探测
    def on_local_echo_auto_off(self, session_id: int):
        """设备回显证明了自己会回显 → 本地回显被终端自动撤掉，说一声

        这里不弹确认、不改用户保存的连接配置（那只是配置里的偏好），只是把"为什么
        屏幕上的字不再由本地画了"讲清楚，免得用户以为程序又不回显了。
        """
        session = self.session_manager.get_session(session_id)
        tab = self.tabs.get(session_id)
        if not tab:
            return
        info = session.connector.info() if session and session.connector else ""
        tab.set_status(True, f"{info}（设备会回显，本地回显已自动关闭）")
        self.on_ai_output(
            session_id,
            "[提示] 设备确实会回显输入，已自动关闭本地回显 —— 之前若出现过"
            "“输入一个字符显示两个”，就是本地回显和设备回显叠在一起造成的。"
        )

    def check_console_echo(self, session_id: int):
        """Console 连上后设备一直没有回显 → 自动打开本地回显

        判据：从连上到现在，设备**一个字符都没吐**。真设备收到唤醒回车后会回
        提示符；完全没动静说明它不回显（或线/流控不对）。此时若本地回显也关着，
        用户敲键盘、按回车屏幕上都没有任何变化，只会认为"程序坏了/回车没反应"
        （用户 2026-10-17 的反馈）。自动打开只影响"看得见自己敲的字"，不改设备
        行为，也不改用户在高级选项里的配置（仅本会话生效，可在界面关掉）。

        这是**猜**，会猜错：锐捷 S2928G-E 对唤醒回车不重打提示符，于是被判成
        "零回显"，本地回显被打开，此后每个字符都画两遍（用户 2026-10-19 的
        "输入 s 显示 ss"）。所以猜错必须能自愈：终端一旦看到设备把我们本地画过
        的字符原样回显回来，就自动关掉本地回显（见
        ``TerminalWidget._drop_duplicate_echo`` / ``on_local_echo_auto_off``）。
        """
        session = self.session_manager.get_session(session_id)
        tab = self.tabs.get(session_id)
        if not session or not tab or tab.terminal.local_echo:
            return
        if session.peek_output():               # 有回显（含 banner）→ 保持关闭，避免重影
            return
        tab.terminal.set_local_echo(True)
        info = session.connector.info() if session.connector else ""
        tab.set_status(True, f"{info}（已自动开启本地回显）")
        self.on_ai_output(
            session_id,
            "[提示] 设备没有回显（唤醒回车后屏幕上没出现任何字符），已自动开启"
            "本地回显 —— 这样你敲的字能看见。若设备本来就回显，可在「高级选项」里关掉。"
        )

    def wake_up_console(self, session_id: int):
        """串口连上后补一个回车，让设备把提示符吐出来

        用户已经在敲键盘时不能发 —— 这个回车会把他的半截命令直接执行掉。
        """
        if not self.device_line_idle(session_id):
            return
        self.on_raw_input(session_id, "\r")

    def device_line_idle(self, session_id: int) -> bool:
        """用户当前没在敲命令（探测前用它，免得把探测命令插进用户输入里）

        不只看本地输入缓冲：屏幕那一行（＝设备真实持有的一行）上有内容也算
        "正在输入"。只判缓冲的话，缓冲与屏幕不同步时会误判成空闲，探测命令
        就被拼进用户正在敲的那一行里了。
        """
        tab = self.tabs.get(session_id)
        if tab is None:
            return True
        try:
            return not tab.terminal.input_pending()
        except Exception:
            return False

    def probe_device_version(self, session_id: int):
        """发一条 show/display version，等回显解析出型号与系统版本"""
        session = self.session_manager.get_session(session_id)
        connector = self.session_manager.get_connector(session_id)
        if not (session and connector and connector.is_connected()):
            return
        if not self.device_line_idle(session_id):
            return          # 用户已经在敲命令了，别打断
        vendor = (session.vendor or "").lower()
        command = "display version" if vendor in ("h3c", "huawei") else "show version"
        self.version_probes[session_id] = command
        self.version_tried[session_id] = {command}   # 记下已试过的语法
        connector.send(command)      # send() 会带上该连接自己的行结束符

    def check_device_version(self):
        """设备输出安静下来后，解析型号/版本并按需取命令清单"""
        for session_id in list(self.version_probes):
            session = self.session_manager.get_session(session_id)
            connector = self.session_manager.get_connector(session_id)
            if not (session and connector and connector.is_connected()):
                self.version_probes.pop(session_id, None)
                continue
            # 等回显吐完（1.2s 内没有新输出）再解析，避免截到半截
            if time.time() - session.last_activity < 1.2:
                continue
            self.version_probes.pop(session_id, None)
            self.on_version_output(session_id, session.recent_output)

    def on_version_output(self, session_id: int, output: str):
        """从 version 回显里提取型号/版本；取不到就换另一种语法再试一次"""
        info = extract_device_info(output)
        session = self.session_manager.get_session(session_id)
        if not session:
            return

        if info["model"]:
            session.model = info["model"]
        if info["os_version"]:
            session.os_version = info["os_version"]
        if info["vendor"] != "unknown" and session.vendor == "unknown":
            session.vendor = info["vendor"]
            if session.ai_agent:
                session.ai_agent.set_vendor(info["vendor"])
        tab = self.tabs.get(session_id)
        if tab:
            tab.terminal.set_vendor(session.vendor)

        if not (info["model"] or info["os_version"]):
            # 语法不对（比如在 H3C 上 show version 会报错）：换另一种再试一次。
            # 每种语法只试一次 —— 否则解析一直失败就会不停地往设备发命令
            alternative = ("show version" if info["vendor"] in ("h3c", "huawei")
                           else "display version")
            tried = self.version_tried.setdefault(session_id, set())
            connector = self.session_manager.get_connector(session_id)
            if (alternative not in tried and connector and connector.is_connected()):
                tried.add(alternative)
                self.version_probes[session_id] = alternative
                connector.send(alternative)
            return

        # 型号/版本认出来了就记到会话上（厂商用于换内置命令表；型号/版本
        # 用于和资料库里整理出的手册命令对上）。这里**不再**按型号让 AI
        # 猜一批常用命令 —— 猜出来的（show fan / show power 之类）在真机上
        # 未必存在，反而把"提示的命令是错的"带进来了。
        self.version_tried.pop(session_id, None)

    # ------------------------------------------------------------ 导入文档整理
    def note_paths(self, title: str):
        """按文件名找出标题匹配的笔记文件

        文件名就是标题，直接比对最可靠 —— 同类型笔记多了以后语义检索
        很可能排不进前几名，于是每次都当"没有"重复生成。
        """
        kb = self.session_manager.knowledge_base
        if kb is None or not self.settings.knowledge_enabled or not title.strip():
            return []
        want = title.strip().lower()
        try:
            documents = kb.list_documents()
        except Exception:
            return []
        paths = []
        for doc in documents:
            name = os.path.basename(str(doc.get("name", ""))).rsplit(".", 1)[0].lower()
            if name == want or name.startswith(want + "_"):
                paths.append(str(doc.get("path", "")))
        return paths

    def start_document_digest(self, paths):
        """导入资料后，后台把文档整理成「技术解释 + 命令清单」

        命令清单由程序自己从正文里抽（不依赖外部服务，导完就能用）；
        技术解释是锦上添花：配了 AI 就顺带写一段，没配就留空 —— 本地整理
        本身照样完成，抽出的命令照样进自动提示。
        """
        kb = self.session_manager.knowledge_base
        if kb is None or not self.settings.knowledge_enabled or not paths:
            return

        pending = []
        for path in paths:
            path = str(path)
            name = os.path.splitext(os.path.basename(path))[0]
            if self.note_paths(f"{name} 摘要"):
                continue                     # 这份已经整理过了
            pending.append(path)
        if not pending:
            return

        # 只有"技术解释"要调大模型；没配 Key 就纯本地整理，不打扰用户
        credentials = self.current_ai_credentials()
        with_ai = bool(credentials.get("api_key"))

        self.status_bar.showMessage(
            f"正在整理 {len(pending)} 份资料（命令清单"
            + (" + 技术解释" if with_ai else "") + "）…", 8000)

        def worker():
            agent = None
            if with_ai:
                agent = AIAgent(api_key=credentials["api_key"],
                                model=credentials["model"],
                                provider=credentials["provider"],
                                base_url=credentials["base_url"])
            for path in pending:
                name = os.path.basename(path)
                try:
                    text = extract_text(path)
                except Exception:
                    continue
                if len((text or "").strip()) < 200:
                    continue
                # 厚手册的命令跨章节均匀采样，别只取前面几章的
                commands = [c for c in even_sample(
                    find_command_candidates(text, limit=5000), 300) if c]

                explanation = ""
                if agent is not None:
                    explanation, ai_commands = agent.digest_document(
                        name, text, commands)
                    ai_commands = [c for c in (ai_commands or []) if c]
                    known = set(ai_commands)
                    # AI 认出来的更准，排前面；程序抽到的补充在后面
                    commands = ai_commands + [c for c in commands
                                              if c not in known]
                if not explanation and not commands:
                    continue
                self.digest_ready.emit({"name": name, "explanation": explanation,
                                        "commands": commands})
            self.digest_ready.emit({"finished": True})

        threading.Thread(target=worker, daemon=True).start()

    def on_digest_ready(self, payload: dict):
        """整理结果落库：技术解释 + 命令清单存成一份笔记"""
        if payload.get("finished"):
            self.status_bar.showMessage("资料整理完成，整理出的命令已可用于自动提示", 6000)
            return
        kb = self.session_manager.knowledge_base
        if kb is None:
            return

        commands = [c for c in (payload.get("commands") or []) if c]
        explanation = (payload.get("explanation") or "").strip()
        content = explanation
        if commands:
            content += ("\n\n" if content else "") + "```\n" + "\n".join(commands) + "\n```"
        if not content.strip():
            return

        name = str(payload.get("name", "文档"))
        kb.save_note(os.path.splitext(name)[0] + " 摘要", content,
                     category="命令速查", tags="文档整理",
                     meta={"来源": name})
        self.build_knowledge_index_bg(force=False)
        self.load_kb_commands_into_sessions()
        self.status_bar.showMessage(
            f"已整理《{name}》：{len(commands)} 条命令进入自动提示", 6000)

    def load_kb_commands_into_sessions(self):
        """把资料库「命令速查」里的命令装进各会话的命令提示

        包括按型号生成的常用命令、以及从手册整理出来的命令清单。
        """
        kb = self.session_manager.knowledge_base
        if kb is None or not self.settings.knowledge_enabled:
            return
        try:
            documents = kb.list_documents()
        except Exception:
            return
        commands = []
        for doc in documents:
            name = str(doc.get("name", "")).replace("\\", "/")
            if not name.startswith("命令速查/") or doc.get("error"):
                continue
            for command in extract_commands(read_text_file(str(doc.get("path", "")))):
                if command not in commands:
                    commands.append(command)
                if len(commands) >= 500:
                    break
            if len(commands) >= 500:
                break
        if not commands:
            return
        for tab in self.tabs.values():
            tab.terminal.load_commands(commands)

    def _install_tab_close_button(self, tab, index: int):
        """给会话标签装一个可见的 ✕ 关闭按钮

        QSS 里的 `QTabBar::close-button { image: none; }` 把默认关闭图标
        隐藏了，这里自己装一个，否则标签上找不到关闭入口。
        """
        btn = QToolButton(self.tab_widget.tabBar())
        btn.setObjectName("TabCloseButton")
        btn.setText("✕")
        btn.setToolTip("关闭该会话")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(18, 18)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setStyleSheet(
            "QToolButton#TabCloseButton { color: #93A0B4; background: transparent;"
            " border: none; font-size: 11px; }"
            "QToolButton#TabCloseButton:hover { color: #ffffff;"
            " background: rgba(224, 108, 117, 0.32); border-radius: 9px; }")
        btn.clicked.connect(lambda _=False, w=tab: self._close_tab_widget(w))
        self.tab_widget.tabBar().setTabButton(
            index, QTabBar.ButtonPosition.RightSide, btn)

    def _close_tab_widget(self, widget):
        """按标签控件本身定位并关闭（避免索引失效）"""
        index = self.tab_widget.indexOf(widget)
        if index >= 0:
            self.close_session(index)

    def close_session(self, index: int):
        """关闭会话"""
        widget = self.tab_widget.widget(index)
        if widget is None:
            return
        session_id = widget.property('session_id')
        if session_id:
            # session_manager.close_session 会同步触发 session_closed
            # → on_session_closed，那里已按 session_id 移除过标签页；
            # 这里必须用 indexOf(widget) 重新定位，否则旧 index 会误删相邻页
            self.session_manager.close_session(session_id)
            pos = self.tab_widget.indexOf(widget)
            if pos >= 0:
                self.tab_widget.removeTab(pos)
            self.tabs.pop(session_id, None)
            self.ai_auto_sessions.discard(session_id)
        else:
            self.tab_widget.removeTab(index)
        self._refresh_central_page()
        self.update_status()
        self.sync_vault_connections()
        self.sync_session_context()          # 关掉最后一个会话也要把 AI 设备条清掉

    def close_all_sessions(self):
        """关闭全部会话"""
        for index in range(self.tab_widget.count() - 1, -1, -1):
            self.close_session(index)

    def on_session_closed(self, session_id: int):
        """会话被外部关闭时同步标签页"""
        tab = self.tabs.get(session_id)
        if tab is not None:
            index = self.tab_widget.indexOf(tab)
            if index >= 0:
                self.tab_widget.removeTab(index)
            self.tabs.pop(session_id, None)
        self.ai_auto_sessions.discard(session_id)
        self.version_probes.pop(session_id, None)
        self.version_tried.pop(session_id, None)
        self._refresh_central_page()
        self.update_status()
        self.sync_vault_connections()
        self.sync_session_context()

    def on_tab_changed(self, index: int):
        """标签切换"""
        widget = self.tab_widget.widget(index)
        if widget:
            session_id = widget.property('session_id')
            if session_id:
                self.session_manager.set_active_session(session_id)
                self.sync_ai_action()
                self.sync_session_context()
                # 切标签后焦点跟到终端，避免敲键盘没有响应
                if hasattr(widget, "terminal"):
                    widget.terminal.setFocus()
        self.update_status()

    def sync_session_context(self):
        """按当前会话同步界面：聊天面板设备条、串口按钮可用性"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None

        # 聊天面板始终绑定当前设备
        if session:
            self.sync_chat_device(session)
            self.vault_panel.set_connected(session.config, connected=True)
            # 命令建议按厂商换一批常用命令（厂商是连接后异步识别出来的）
            tab = self.tabs.get(session_id)
            if tab and session.vendor and session.vendor != "unknown":
                tab.terminal.set_vendor(session.vendor)

        else:
            # 一个会话都没有：把设备条清干净。以前这里只有 `if session:` 分支，
            # 关掉最后一个会话后设备条还写着"当前设备：xxx ● 已连接"
            # （用户反馈："已经没有连接，AI 这里没有变化"）
            self.chat_view.set_device("", connected=False)
            self.chat_view.set_model("")

        # 串口控制只在 Console 会话下可用
        is_serial = bool(session and session.protocol == "Serial"
                         and session.connector
                         and session.connector.supports_serial_control())
        self.action_serial.setEnabled(is_serial)

    def disconnect_current(self):
        """断开当前会话"""
        index = self.tab_widget.currentIndex()
        if index >= 0:
            self.close_session(index)

    def clear_current(self):
        """清空当前终端"""
        tab = self.current_tab()
        if tab:
            tab.clear()

    def save_current_log(self):
        """保存当前会话日志"""
        tab = self.current_tab()
        if tab:
            tab.save_log()

    def current_tab(self):
        """返回当前标签页控件"""
        widget = self.tab_widget.currentWidget()
        return widget if isinstance(widget, SessionTab) else None

    def current_session_id(self):
        """返回当前会话ID"""
        tab = self.current_tab()
        return tab.property('session_id') if tab else None

    # ------------------------------------------------------------ 设备库 Vault
    def register_vault_device(self, session):
        """把已连接的设备登记进左侧 Vault（去重，不存密码）"""
        config = dict(session.config)
        config['name'] = session.name
        if session.vendor and session.vendor != "unknown":
            config['vendor'] = session.vendor
        added = self.settings.add_vault_device(config)
        self.vault_panel.set_devices(self.settings.get_vault_devices())
        self.vault_panel.set_connected(config, connected=True)

    def sync_vault_connections(self):
        """按当前仍存活的会话整体重建设备库绿点（关闭标签/断开后调用）"""
        keys = {
            self._session_match_key(session.config)
            for session in self.session_manager.get_all_sessions().values()
        }
        self.vault_panel.sync_connected_keys(keys)

    def on_vault_connect(self, config: dict):
        """双击/点击“连接”某台设备：记录里存了密码就直接连，没有才弹框补全

        用户反馈"即便左边保存了，打开时还是要重新输入内容" —— 根因就是这里
        无论有没有密码都先弹连接对话框。存了密码（b64 混淆）时别再打搅用户。
        """
        if not config:
            return
        if config.get('password'):
            cfg = dict(config)
            pwd = cfg.get('password') or ''
            if isinstance(pwd, str) and pwd.startswith('b64:'):
                cfg['password'] = self.settings.decode_password(pwd)
            self.create_session(cfg)
            return
        # 没存密码：弹对话框补全凭据（主机/用户名/端口已按记录填好）
        dialog = ConnectDialog(self)
        dialog.set_config(config)
        if dialog.exec():
            self.create_session(dialog.get_config())

    def on_vault_disconnect(self, config: dict):
        """右键“断开连接”：关闭该设备对应的会话标签（同时断开连接）"""
        if not config:
            return
        target_key = self._session_match_key(config)
        # 收集所有匹配的 session_id（同一设备可能开了多个标签）
        matched = [
            sid for sid, session in self.session_manager.get_all_sessions().items()
            if self._session_match_key(session.config) == target_key
        ]
        if not matched:
            self.status_bar.showMessage("该设备当前没有活动连接", 3000)
            self.sync_vault_connections()
            return
        # 逐个关闭（close_session 内部会 removeTab，用 session_id 直接关更稳）
        for session_id in matched:
            tab = self.tabs.get(session_id)
            if tab is not None:
                index = self.tab_widget.indexOf(tab)
                if index >= 0:
                    self.close_session(index)
                    continue
            self.session_manager.close_session(session_id)
        self.sync_vault_connections()
        self.status_bar.showMessage(
            f"已断开 {config.get('name') or config.get('host') or config.get('serial_port', '')}",
            3000)

    @staticmethod
    def _session_match_key(config: dict) -> tuple:
        """会话/设备匹配键：协议 + 主机(或串口) + 端口，与 VaultPanel._device_key 一致"""
        host = (config.get('host') or config.get('serial_port') or '')
        return (config.get('protocol'), host, config.get('port'))

    def on_vault_edit(self, config: dict):
        """编辑一台已保存的设备信息"""
        if not config:
            return
        dialog = ConnectDialog(self)
        dialog.set_config(config)
        if dialog.exec():
            new_config = dialog.get_config()
            # 按**原记录**定位（改了主机/端口也能改到同一条，不再新增重复项）
            self.settings.update_vault_device(new_config, old_config=config)
            self.vault_panel.set_devices(self.settings.get_vault_devices())

    def on_vault_delete(self, config: dict):
        """从设备库删除一台设备（若该设备还有活动会话，一并关闭标签页）"""
        if not config:
            return
        # 先关掉该设备对应的会话，避免"左边删了、右边标签页还在"
        target_key = self._session_match_key(config)
        matched = [
            sid for sid, session in self.session_manager.get_all_sessions().items()
            if self._session_match_key(session.config) == target_key
        ]
        for session_id in matched:
            tab = self.tabs.get(session_id)
            index = self.tab_widget.indexOf(tab) if tab is not None else -1
            if index >= 0:
                self.close_session(index)
            else:
                self.session_manager.close_session(session_id)
                self.tabs.pop(session_id, None)
        self.settings.remove_vault_device(config)
        self.vault_panel.set_devices(self.settings.get_vault_devices())
        self.sync_vault_connections()

    def on_vault_selection(self, config: dict):
        """选中设备变化时同步聊天面板的设备条（不自动连接）"""
        if config:
            self.chat_view.set_device(
                config.get('name') or config.get('host') or '设备',
                config.get('protocol', ''),
                config.get('host') or config.get('serial_port', ''),
                connected=False,
                vendor=config.get('vendor', 'unknown'),
            )
        else:
            self.chat_view.set_device("", connected=False)

    def sync_chat_device(self, session):
        """把当前会话设备信息同步到聊天面板顶部设备条"""
        self.chat_view.set_device(
            session.name,
            session.protocol,
            session.config.get('host') or session.config.get('serial_port', ''),
            connected=bool(session.connector and session.connector.is_connected()),
            vendor=session.vendor,
        )
        if session.ai_enabled and session.ai_agent:
            self.chat_view.set_model(session.ai_agent.describe_model())

    # ------------------------------------------------------------ Catty 对话
    def ensure_session_ai_enabled(self, session):
        """确保当前会话已启用 AI；密钥缺失时返回 None"""
        if session.ai_enabled and session.ai_agent:
            return session.ai_agent
        creds = self.current_ai_credentials()
        if not creds.get("api_key"):
            return None
        self.session_manager.enable_ai(session.session_id, **creds, task="")
        tab = self.tabs.get(session.session_id)
        if tab:
            tab.set_ai_badge(True)
        self.action_ai.setChecked(True)
        self.action_ai.setText("AI: ON")
        self.status_ai.setText("AI: ON")
        return session.ai_agent

    def _active_session(self):
        sid = self.current_session_id()
        return self.session_manager.get_session(sid) if sid else None

    def on_chat_message(self, text: str):
        """用户在 Catty 里发消息：调用 AI 对话并渲染回复"""
        # 先把用户的话渲染出来，再做环境校验 —— 否则没连设备/没配 Key 时
        # 输入框已被清空、用户的话凭空消失，看起来像"说了没反应"
        self.chat_view.add_user(text)
        session = self._active_session()
        if not session:
            self.chat_view.add_error("还没有连接设备。请在左侧设备库双击一台设备先连接。")
            return
        agent = self.ensure_session_ai_enabled(session)
        if not agent:
            self.chat_view.add_error(
                "未配置 AI：请打开右侧『AI 高级 → AI 助手』页，点『AI 设置…』填写 API Key。")
            return
        self.chat_view.set_busy(True)
        threading.Thread(
            target=self._chat_worker, args=(session.session_id, text),
            daemon=True
        ).start()

    def _chat_worker(self, session_id: int, text: str):
        try:
            session = self.session_manager.get_session(session_id)
            if not session or not session.ai_agent:
                self.chat_ui_call.emit(
                    "add_error", ("AI 未就绪，请确认已配置 API Key。",))
                return
            device_output = session.peek_output() or session.recent_output or ""
            agent = session.ai_agent
            # 先在本地经验里找同类记录（纯本地检索，不花额度），命中就一并交给 AI
            experience = agent.experience_context(text)
            hits = list(agent.last_experience_hits)
            reply_text, commands = agent.converse(
                text, device_output, task=session.ai_task, experience=experience)
            if hits:
                names = "、".join(os.path.basename(hit["source"]) for hit in hits[:3])
                self.chat_ui_call.emit("add_system", (
                    f"[经验] 已参考 {len(hits)} 条历史经验：{names}",))
            if not reply_text and not commands:
                self.chat_ui_call.emit(
                    "add_error", ("AI 没有返回内容，可能是网络不畅或密钥无效。",))
                return
            self.chat_ui_call.emit("add_ai", (reply_text, commands))
            # 后台把这一轮整理成一条经验（成功点 / 踩坑点），不阻塞对话
            self.summarize_experience_bg(
                session_id, text, reply_text, commands, device_output)
        except Exception as e:
            self.chat_ui_call.emit("add_error", (f"AI 调用失败：{e}",))
        finally:
            self.chat_ui_call.emit("set_busy", (False,))

    def summarize_experience_bg(self, session_id: int, user_message: str,
                                reply_text: str, commands, device_output: str):
        """后台让 AI 把这一轮整理成经验总结，写完发信号回主线程落库

        失败或没什么可归档时静默跳过 —— 这是后台整理，不该打扰用户。
        """
        if not self.settings.knowledge_enabled:
            return
        session = self.session_manager.get_session(session_id)
        if not session or not session.ai_agent:
            return
        agent = session.ai_agent
        device = f"{session.name}（{session.protocol}）"
        vendor = session.vendor

        def worker():
            try:
                note = agent.summarize_experience(
                    user_message, reply_text, commands, device_output,
                    device=device, vendor=vendor)
            except Exception:
                note = ""
            if note:
                self.experience_ready.emit({
                    "session_id": session_id,
                    "need": user_message,
                    "note": note,
                })

        threading.Thread(target=worker, daemon=True).start()

    def on_experience_ready(self, payload: dict):
        """经验总结写好：存进资料库的「经验总结」分类，供以后快速匹配"""
        note = (payload.get("note") or "").strip()
        if not note:
            return
        kb = self.session_manager.knowledge_base
        if kb is None:
            return

        need = (payload.get("need") or "").strip().replace("\n", " ")
        title = f"经验 · {need[:40]}" if need else "经验总结"
        session = self.session_manager.get_session(payload.get("session_id"))
        path = kb.save_note(
            title, note, category=EXPERIENCE_CATEGORY,
            tags=self.suggest_tags(session, need, note),
            meta=self.build_note_meta(session))
        if not path:
            return
        # 增量重建索引，让这条经验立刻能被匹配到
        self.build_knowledge_index_bg(force=False)
        self.status_bar.showMessage(
            f"已记入经验总结（{len(note)} 字），下次提同类需求会直接参考", 5000)

    def on_chat_execute(self, commands):
        """Catty 命令计划卡片上的“执行”：复用配置命令确认闸门"""
        session = self._active_session()
        if not session:
            self.chat_view.add_error("没有活动会话，无法下发命令。")
            return
        commands = [str(c).strip() for c in (commands or []) if str(c).strip()]
        if not commands:
            return
        self.chat_view.add_system(
            f"已下发 {len(commands)} 条命令"
            f"（含配置类命令会先弹确认框）")
        self.run_batch(session.session_id, commands,
                       self.settings.batch_delay_ms, True)

    def on_chat_explain(self):
        """Catty 里的“解释输出”：用 AI 解读当前终端内容"""
        session = self._active_session()
        if not session:
            self.chat_view.add_error("请先连接一台设备。")
            return
        agent = self.ensure_session_ai_enabled(session)
        if not agent:
            self.chat_view.add_error("未配置 AI：请在『AI 高级 → AI 助手』页点『AI 设置…』填写 API Key。")
            return
        self.chat_view.add_user("（解释当前输出）")
        self.chat_view.set_busy(True)
        threading.Thread(
            target=self._chat_explain_worker, args=(session.session_id,),
            daemon=True
        ).start()

    def _chat_explain_worker(self, session_id: int):
        try:
            session = self.session_manager.get_session(session_id)
            device_output = session.peek_output() or session.recent_output or ""
            answer = session.ai_agent.explain(device_output, "")
            self.chat_ui_call.emit("add_ai", (answer, []))
        except Exception as e:
            self.chat_ui_call.emit("add_error", (f"AI 解释失败：{e}",))
        finally:
            self.chat_ui_call.emit("set_busy", (False,))

    def on_chat_research(self, text: str):
        """Catty 里的“查资料”：检索本地资料库 / 联网后作答"""
        session = self._active_session()
        if not text or not text.strip():
            self.chat_view.add_error("请输入要查的问题。")
            return
        if not session:
            self.chat_view.add_error("请先连接一台设备。")
            return
        agent = self.ensure_session_ai_enabled(session)
        if not agent:
            self.chat_view.add_error("未配置 AI：请在『AI 高级 → AI 助手』页点『AI 设置…』填写 API Key。")
            return
        if not agent.lookup_enabled:
            self.chat_view.add_error(
                "未启用资料检索：请在『AI 高级』页开启本地资料库或联网检索。")
            return
        self.chat_view.add_user(f"（查资料）{text}")
        self.chat_view.set_busy(True)
        threading.Thread(
            target=self._chat_research_worker,
            args=(session.session_id, text.strip()), daemon=True
        ).start()

    def _chat_research_worker(self, session_id: int, text: str):
        try:
            session = self.session_manager.get_session(session_id)
            device_output = session.peek_output() or session.recent_output or ""
            answer = session.ai_agent.research(text, device_output)
            sources = session.ai_agent.last_lookup_sources or []
            body = answer or "（未查到相关内容）"
            if sources:
                body += "\n\n参考来源：\n" + "\n".join(
                    f"- 【{'本地' if s.get('source_type') == 'local' else '网页'}】"
                    f"{s.get('title', '')}" for s in sources)
            self.chat_ui_call.emit("add_ai", (body, []))
        except Exception as e:
            self.chat_ui_call.emit("add_error", (f"资料检索失败：{e}",))
        finally:
            self.chat_ui_call.emit("set_busy", (False,))

    # ------------------------------------------------------------ 命令
    def on_command_entered(self, session_id: int, command: str):
        """用户输入命令"""
        session = self.session_manager.get_session(session_id)
        if not session or not session.connector:
            return
        if not session.connector.is_connected():
            self.status_bar.showMessage("连接已断开，无法发送命令", 3000)
            return
        # 顺带识别设备厂商，供 AI 使用正确语法
        if session.ai_agent and session.ai_agent.vendor == "unknown":
            vendor = detect_vendor(session.peek_output())
            if vendor != "unknown":
                session.ai_agent.set_vendor(vendor)
        self.session_manager.send_command(session_id, command)

    def on_interrupt(self, session_id: int):
        """发送 Ctrl+C 中断"""
        connector = self.session_manager.get_connector(session_id)
        if connector and connector.is_connected():
            connector.send_raw('\x03')

    def on_raw_input(self, session_id: int, text: str):
        """发送原始输入（逐字符输入 / 回车 / Tab / 退格等）"""
        connector = self.session_manager.get_connector(session_id)
        if not (connector and connector.is_connected()):
            # 掉线后按键不但没发出去、屏幕上也没有任何反馈，用户只会觉得"键盘坏了"。
            # 每个会话只提一次，免得每个字符刷一行。
            if session_id not in self._input_dropped_notified:
                self._input_dropped_notified.add(session_id)
                tab = self.tabs.get(session_id)
                if tab:
                    tab.append_output(
                        "\r\n[未发送] 连接已断开，刚才的输入没有发到设备\r\n")
            return
        # 回车时顺带识别设备厂商（改为逐字符下发后，这里替代原 on_command_entered 的时机）
        # 识别出来后同步给命令建议表，这样提示的就是该厂商的语法
        if text in ("\r", "\n", "\r\n"):
            session = self.session_manager.get_session(session_id)
            if session:
                vendor = session.vendor
                if vendor == "unknown":
                    vendor = detect_vendor(session.peek_output())
                    if vendor != "unknown":
                        session.vendor = vendor
                        if session.ai_agent:
                            session.ai_agent.set_vendor(vendor)
                tab = self.tabs.get(session_id)
                if tab and vendor != "unknown":
                    tab.terminal.set_vendor(vendor)
        started = time.monotonic()
        connector.send_raw(text)
        cost = time.monotonic() - started
        if cost > 0.3:
            # 串口写满/驱动阻塞会把界面线程一起拖住（表现为"卡死"），留个证据
            self.log_slow(f"下发输入耗时 {cost:.2f}s：{text!r}", tag="慢")

    # ------------------------------------------------------------ AI
    def toggle_ai_mode(self, checked: bool):
        """切换AI模式"""
        session_id = self.current_session_id()
        if not session_id:
            self.action_ai.setChecked(False)
            self.status_bar.showMessage("请先建立一个会话", 3000)
            return

        if checked:
            api_key = self.ai_panel.get_api_key()
            if not api_key:
                self.action_ai.setChecked(False)
                QMessageBox.warning(
                    self, "缺少 API Key",
                    "请先在『AI 高级 → AI 助手』页点『AI 设置…』填写 API Key。\n\n"
                    "支持 Anthropic Claude、DeepSeek 以及任意 OpenAI 兼容接口。"
                )
                return
            task = ""
            credentials = self.current_ai_credentials()
            # 界面上已没有"自动模式"开关，一律走建议模式：AI 只给建议，不自动下发
            auto = False
            self.session_manager.set_ai_credentials(
                credentials["api_key"], credentials["model"],
                credentials["provider"], credentials["base_url"])
            self.session_manager.enable_ai(session_id, **credentials, task=task, auto=auto)
            if auto:
                self.ai_auto_sessions.add(session_id)
            tab = self.tabs.get(session_id)
            if tab:
                tab.set_ai_badge(True)
            self.action_ai.setText("AI: ON")
            self.status_ai.setText("AI: ON")
            self.status_bar.showMessage("AI模式已启用" + ("（自动执行）" if auto else "（建议模式）"))
            self.on_ai_output(session_id, f"[系统] AI 已启用，任务: {task or '（未设置，按输出自行判断）'}")
        else:
            self.session_manager.disable_ai(session_id)
            self.ai_auto_sessions.discard(session_id)
            tab = self.tabs.get(session_id)
            if tab:
                tab.set_ai_badge(False)
            self.action_ai.setText("AI: OFF")
            self.status_ai.setText("AI: OFF")
            self.status_bar.showMessage("AI模式已关闭")

    def sync_ai_action(self):
        """切换标签时同步 AI 开关状态"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        enabled = bool(session and session.ai_enabled)
        self.action_ai.setChecked(enabled)
        self.action_ai.setText("AI: ON" if enabled else "AI: OFF")
        self.status_ai.setText("AI: ON" if enabled else "AI: OFF")

    def on_credentials_changed(self, credentials):
        """AI 凭据变化（提供方 / 密钥 / 模型 / 接口地址）"""
        if isinstance(credentials, dict):
            self.session_manager.set_ai_credentials(
                credentials.get("api_key", ""),
                credentials.get("model", ""),
                credentials.get("provider", ""),
                credentials.get("base_url", ""),
            )
            from core.llm import provider_label
            self.status_bar.showMessage(
                f"AI 配置已保存：{provider_label(credentials.get('provider', ''))}"
                f" / {credentials.get('model', '')}", 4000)
            # 凭据变了，之前"已连通"的结论作废 —— 回到"未验证"
            self.update_api_status()
        else:  # 兼容旧的两参数调用
            self.session_manager.set_ai_credentials(*credentials)

    def on_api_connection_tested(self, ok: bool, message: str):
        """AI 面板测完连接，同步状态栏指示"""
        self.update_api_status(ok=ok, message=message)
        self.status_bar.showMessage(
            ("API 连接正常：" if ok else "API 连接失败：") + message, 5000)

    def update_api_status(self, ok=None, message: str = ""):
        """
        状态栏的 API 凭据指示（三态）

        ok=True  测试通过   → 绿色 ✓
        ok=False 测试失败   → 红色 ✗
        ok=None  未测试     → 看有没有填 Key：填了=未验证(灰)，没填=未配置(暗)
        """
        if ok is True:
            text, color = "API: ✓ 已连通", "#3FB950"
        elif ok is False:
            text, color = "API: ✗ 连接失败", "#F85149"
        else:
            creds = self.current_ai_credentials()
            if creds.get("api_key"):
                text, color = "API: 未验证", "#93A0B4"
            else:
                text, color = "API: 未配置", "#6B7684"
        self.status_api.setText(text)
        self.status_api.setStyleSheet(f"color: {color};")
        self.status_api.setToolTip(message or "可在『AI 助手』页的『AI 设置…』里测试 Key 是否可用")

    def update_safety_status(self):
        """
        状态栏的配置命令安全闸门指示。

        这是项目最要紧的一条约定（配置类命令下发前必须人工确认），
        所以让它常驻状态栏，不用翻菜单就知道当前是开是关。
        关闭时用醒目的红色 + ⚠，避免"以为开着其实关了"。
        """
        on = bool(self.settings.confirm_config_commands)
        if on:
            self.status_safety.setText("安全闸门: 开")
            self.status_safety.setStyleSheet("color: #3FB950;")
            self.status_safety.setToolTip(
                "配置类命令下发前会先弹出命令清单等你确认（工具菜单可关闭）")
        else:
            self.status_safety.setText("安全闸门: 关 ⚠")
            self.status_safety.setStyleSheet("color: #F85149; font-weight: 650;")
            self.status_safety.setToolTip(
                "⚠ 已关闭配置命令确认 —— 配置类命令可能被直接下发，请谨慎")

    def current_ai_credentials(self) -> dict:
        """当前 AI 面板上的凭据"""
        return {
            "api_key": self.ai_panel.get_api_key(),
            "model": self.ai_panel.get_model(),
            "provider": self.ai_panel.get_provider(),
            "base_url": self.ai_panel.get_base_url(),
        }

    def on_analyze_requested(self, task: str):
        """对当前屏幕输出跑一次 AI 分析；若输入形如命令则直接下发"""
        session_id = self.current_session_id()
        if not session_id:
            self.status_bar.showMessage("请先建立一个会话", 3000)
            return
        session = self.session_manager.get_session(session_id)
        if not session:
            return

        task = (task or "").strip()

        # 形如命令的输入：直接下发，不做 AI 分析
        if task and self._looks_like_command(task):
            self.session_manager.send_command(session_id, task)
            self.status_bar.showMessage(f"已发送: {task}", 2000)
            return

        if not session.ai_enabled:
            api_key = self.ai_panel.get_api_key()
            if not api_key:
                QMessageBox.warning(self, "缺少 API Key",
                                    "请先在『AI 高级 → AI 助手』页点『AI 设置…』填写 API Key。")
                return
            self.session_manager.enable_ai(
                session_id, **self.current_ai_credentials(), task=task
            )
            tab = self.tabs.get(session_id)
            if tab:
                tab.set_ai_badge(True)
            self.action_ai.setChecked(True)
            self.action_ai.setText("AI: ON")
            self.status_ai.setText("AI: ON")

        self.chat_view.add_system("[系统] 正在分析当前输出…")
        threading.Thread(
            target=self._manual_analyze_worker,
            args=(session_id, task),
            daemon=True
        ).start()

    @staticmethod
    def _looks_like_command(text: str) -> bool:
        """粗略判断输入是一行命令而非自然语言任务描述"""
        text = (text or "").strip()
        if not text or "\n" in text or len(text) > 80:
            return False
        # 含中文的多半是任务描述
        if any('\u4e00' <= ch <= '\u9fff' for ch in text):
            return False
        return len(text.split()) <= 5

    def analyze_once(self):
        """工具栏：单步分析一次（不带任务描述，按当前屏幕输出自行判断）"""
        self.on_analyze_requested("")

    def reset_ai_context(self):
        """清空当前会话的 AI 上下文"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        if session and session.ai_agent:
            session.ai_agent.reset_context()
            self.status_bar.showMessage("AI 上下文已清空", 2000)

    # ------------------------------------------------------------ AI 线程
    def _manual_analyze_worker(self, session_id: int, task: str):
        try:
            reply = self.session_manager.ai_analyze_once(session_id, task)
            if not reply:
                self.session_manager.ai_output.emit(
                    session_id, "[AI] 未生成新命令（任务完成或需要人工确认）"
                )
                return
            session = self.session_manager.get_session(session_id)
            if not session or not session.ai_agent:
                return
            commands, blocked = session.ai_agent.filter_commands(reply)
            for cmd in blocked:
                self.session_manager.ai_output.emit(
                    session_id, f"[安全拦截] {cmd}（危险命令，需人工确认）"
                )
            # 建议命令写入终端，由用户在对话面板里确认后下发
            for cmd in commands:
                self.session_manager.ai_command.emit(session_id, cmd)
        except Exception as e:
            self.session_manager.ai_output.emit(session_id, f"[错误] {e}")

    def _ai_step_worker(self, session_id: int, auto: bool):
        try:
            _, _, status = self.session_manager.ai_step(session_id, auto)
            # done=任务完成/需人工确认；queued=生成了配置类命令，等用户确认
            if status in ("done", "queued"):
                session = self.session_manager.get_session(session_id)
                if session:
                    session.ai_auto = False
                self.ai_auto_sessions.discard(session_id)
        except Exception as e:
            self.session_manager.ai_output.emit(session_id, f"[错误] AI 步骤异常: {e}")

    def ai_tick(self):
        """AI 自动模式心跳：输出稳定后触发一轮分析"""
        for session_id, session in self.session_manager.get_all_sessions().items():
            if not session.ai_enabled or session.ai_busy:
                continue
            if not session.connector or not session.connector.is_connected():
                continue
            if not session.peek_output().strip():
                continue
            # 等待输出稳定（0.8s 内无新数据）
            if time.time() - session.last_activity < 0.8:
                continue
            auto = bool(session.ai_auto and session_id in self.ai_auto_sessions)
            threading.Thread(
                target=self._ai_step_worker,
                args=(session_id, auto),
                daemon=True
            ).start()

    # ------------------------------------------------------------ 信号槽
    def on_chat_ui_call(self, method: str, args):
        """工作线程发来的 chat_view 方法调用，在主线程执行

        用 try/except 兜底：个别调用出错也不能让界面卡死或崩溃。
        """
        try:
            getattr(self.chat_view, method)(*(args or ()))
        except Exception:
            pass

    def on_ai_output(self, session_id: int, text: str):
        """AI 消息显示到对话面板"""
        session = self.session_manager.get_session(session_id)
        prefix = f"[{session.name}] " if session else ""
        self.chat_view.add_system(prefix + text)

    def on_ai_command(self, session_id: int, command: str):
        """AI 生成的命令写入终端，由用户确认后再下发"""
        tab = self.tabs.get(session_id)
        if tab:
            tab.append_output(f"\n[AI] {command}\n")

    def on_session_error(self, session_id: int, message: str):
        """连接/会话错误"""
        self.status_bar.showMessage(f"错误: {message}", 5000)
        if session_id > 0:
            self.chat_view.add_system(f"[错误] {message}")
            if "连接失败" in message or "认证失败" in message:
                QMessageBox.warning(self, "连接失败", message)

    # ------------------------------------------------------------ 刷新
    def update_output(self):
        """定时把连接器缓冲区内容刷新到终端（唯一的显示路径，避免重复输出）"""
        for session_id, tab in self.tabs.items():
            connector = self.session_manager.get_connector(session_id)
            if not connector:
                continue
            output = connector.get_output()
            if output:
                started = time.monotonic()
                tab.append_output(output)
                cost = time.monotonic() - started
                if cost > 0.3:
                    # 上屏太慢也会把界面顶住（长行 + 着色），留个证据
                    self.log_slow(f"上屏 {len(output)} 字符耗时 {cost:.2f}s", tag="慢")
                self._maybe_auto_page(session_id, tab, connector, output)
            tab.set_status(connector.is_connected())

    def _maybe_auto_page(self, session_id: int, tab, connector, output: str):
        """设备停在 `---- More ----` 时替用户敲一个空格

        为什么必须有：分页是设备端的流控，人不动它就不再吐数据。用户看到的就是
        "设备明明有数据，界面只有一行 ---- More ----"（2026-10-19 反馈）。
        批量下发早就会自动翻页，交互会话一直没有。
        安全边界：只在"用户没有未发送输入"时动手 —— 绝不把空格戳进正在敲的命令里。
        """
        from core.batch import CommandWaiter

        carry = self._page_tail.get(session_id, "")
        tail = (carry + (output or ""))[-240:]
        self._page_tail[session_id] = tail
        if not CommandWaiter.PAGING_RE.search(tail[-40:]):
            return
        if not self.settings.session_auto_page:
            self._page_tail[session_id] = ""
            return
        terminal = getattr(tab, "terminal", None)
        if terminal is not None and terminal.input_pending():
            # 用户正在敲命令：这处分页不替他按，免得空格进到命令里
            self._page_tail[session_id] = ""
            return
        self._page_tail[session_id] = ""
        try:
            connector.send_raw(" ")
        except Exception:                                     # noqa: BLE001
            return
        self.status_bar.showMessage("已自动翻页（---- More ----）", 2000)

    def update_status(self):
        """刷新状态栏"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        if session:
            state = "已连接" if (session.connector and session.connector.is_connected()) else "已断开"
            self.status_session.setText(f"{session.name} · {session.protocol} · {state}")
        else:
            self.status_session.setText("无活动会话")

    # ------------------------------------------------------------ 命令模板 / 批量下发
    def show_template_dialog(self):
        """打开命令模板对话框"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        if not session:
            self.status_bar.showMessage("请先建立一个会话", 3000)
            return

        dialog = TemplateDialog(
            self,
            default_delay=self.settings.batch_delay_ms,
            session_name=session.name,
            vendor=session.vendor,
            wait_mode=self.settings.batch_wait_mode,
            auto_page=self.settings.batch_auto_page,
        )
        dialog.commands_ready.connect(
            lambda options, sid=session_id: self.run_batch_opts(sid, options)
        )
        dialog.exec()

    def run_batch_opts(self, session_id: int, options: dict):
        """按对话框返回的选项执行批量下发"""
        options = options or {}
        self.settings.batch_wait_mode = options.get("wait_mode", "prompt")
        self.settings.set("batch_auto_page", bool(options.get("auto_page", True)))
        self.settings.set("batch_delay_ms", int(options.get("delay_ms", 500)))
        self.settings.save()
        self.run_batch(
            session_id,
            options.get("commands") or [],
            int(options.get("delay_ms", 500)),
            bool(options.get("block_dangerous", True)),
            wait_mode=options.get("wait_mode", "prompt"),
            auto_page=bool(options.get("auto_page", True)),
        )

    def run_batch(self, session_id: int, commands, delay_ms: int,
                  block_dangerous: bool = True, wait_mode: str = None,
                  auto_page: bool = None, skip_confirm: bool = False):
        """执行批量命令下发（配置类命令会先弹确认框）"""
        session = self.session_manager.get_session(session_id)
        if not session or not session.connector:
            self.status_bar.showMessage("会话不存在，批量下发已取消", 3000)
            return
        if self.batch_runner and self.batch_runner.running:
            QMessageBox.information(self, "批量下发进行中", "请等待当前批处理结束或先中止它。")
            return

        commands = [str(c).strip() for c in (commands or []) if str(c).strip()]
        if not commands:
            self.status_bar.showMessage("没有需要下发的命令", 3000)
            return

        # 安全闸门：配置类命令必须先让人看命令清单并确认
        if not skip_confirm and self.settings.confirm_config_commands \
                and has_config_command(commands):
            dialog = ConfigConfirmDialog(
                self, commands, target=session.name, action="批量下发")
            if not dialog.confirmed:
                self.status_bar.showMessage("已取消：配置命令未经确认", 5000)
                self.chat_view.add_system(
                    "[安全] 已取消下发：这批命令包含配置类命令，未经确认不会下发")
                return

        wait_mode = wait_mode or self.settings.batch_wait_mode
        if auto_page is None:
            auto_page = self.settings.batch_auto_page

        self.settings.set("batch_delay_ms", delay_ms)
        self.settings.save()

        runner = BatchRunner(
            session.connector, commands, delay_ms,
            block_dangerous=block_dangerous,
            logger=session.logger,
            parent=self,
            wait_mode=wait_mode,
            prompt_timeout=self.settings.batch_prompt_timeout,
            auto_page=auto_page,
        )
        runner.progress.connect(self.on_batch_progress)
        runner.blocked.connect(self.on_batch_blocked)
        runner.command_done.connect(self.on_batch_command_done)
        runner.finished.connect(self.on_batch_finished)
        self.batch_runner = runner

        mode_text = "等提示符返回" if wait_mode == "prompt" else f"固定间隔 {delay_ms}ms"
        self.chat_view.add_system(
            f"[批量] 开始下发 {len(runner.commands)} 条命令（{mode_text}）")

        self.progress_dialog = QProgressDialog(
            f"正在批量下发到 {session.name}…", "中止", 0, len(runner.commands), self
        )
        self.progress_dialog.setWindowTitle("批量下发")
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.canceled.connect(runner.stop)
        self.progress_dialog.show()

        if not runner.start():
            self.progress_dialog.close()
            self.progress_dialog = None

    def on_batch_progress(self, index: int, total: int, command: str):
        self.status_bar.showMessage(f"批量下发 {index}/{total}: {command}")
        if self.progress_dialog:
            self.progress_dialog.setLabelText(f"{index}/{total}\n{command}")
            self.progress_dialog.setValue(index - 1)

    def on_batch_command_done(self, index: int, status: str, elapsed: float):
        """单条命令执行完成（等提示符模式下会带上耗时）"""
        text = {
            "prompt": "完成", "silence": "完成（输出静默）", "delay": "已发送",
            "confirm": "需人工确认", "timeout": "超时", "blocked": "已拦截",
            "disconnected": "连接断开",
        }.get(status, status)
        if self.progress_dialog:
            self.progress_dialog.setValue(index)
        self.status_bar.showMessage(f"第 {index} 条: {text}（{elapsed:.1f}s）")

    def on_batch_blocked(self, index: int, command: str):
        self.chat_view.add_system(f"[安全拦截] 批量下发跳过第 {index} 条: {command}")

    def on_batch_finished(self, ok: bool, message: str):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        self.status_bar.showMessage(message, 8000)
        self.chat_view.add_system(f"[批量] {message}")

        # 有输出时提示可以导出结果
        if ok and self.batch_runner and self.batch_runner.results:
            self.status_bar.showMessage(message + "　可用「工具 → 导出批量下发结果」保存", 10000)

    def export_batch_result(self):
        """导出上一次批量下发的结果（Excel）"""
        runner = self.batch_runner
        if not runner or not runner.results:
            QMessageBox.information(self, "没有结果", "还没有可导出的批量下发结果。")
            return

        from core.xlsx_writer import write_xlsx

        path, _ = QFileDialog.getSaveFileName(
            self, "导出批量下发结果",
            f"批量下发结果_{time.strftime('%Y%m%d_%H%M')}.xlsx",
            "Excel 文件 (*.xlsx)"
        )
        if not path:
            return

        rows = [["序号", "命令", "状态", "耗时(秒)", "输出"]]
        rows.extend(runner.summary_rows())
        ok = write_xlsx(path, [("批量下发结果", rows)],
                        column_widths={"批量下发结果": [6, 30, 18, 10, 90]})
        if ok:
            QMessageBox.information(self, "导出成功", f"已导出:\n{path}")
        else:
            QMessageBox.warning(self, "导出失败", "写入 Excel 失败，请换个路径再试。")

    # ------------------------------------------------------------ 配置比对 / 快照
    def show_diff_dialog(self):
        """打开配置差异比对对话框"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        DiffDialog(self, session).exec()

    def take_snapshot(self):
        """采集一份配置快照（按厂商选择命令）"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        if not session:
            self.status_bar.showMessage("请先建立一个会话", 3000)
            return
        if not session.connector or not session.connector.is_connected():
            self.status_bar.showMessage("连接已断开，无法采集快照", 3000)
            return

        command = "show running-config" if session.vendor == "cisco" else "display current-configuration"
        self.status_bar.showMessage(f"正在采集配置快照（{command}）…")
        threading.Thread(
            target=self._snapshot_worker,
            args=(session_id, command),
            daemon=True
        ).start()

    def _snapshot_worker(self, session_id: int, command: str,
                         settle: float = 1.5, timeout: float = 25.0):
        """下发配置查看命令并等待输出稳定，然后存为快照"""
        session = self.session_manager.get_session(session_id)
        if not session:
            return
        try:
            session.start_capture()
            if not self.session_manager.send_command(session_id, command):
                session.stop_capture()
                # 静默返回过一次（2026-10-17 实测：点了「采集配置快照」什么都不发生，
                # 用户无从判断）—— 会话忙或刚断开时必须说出来
                reason = getattr(self.session_manager, "last_send_error", "") or "会话忙或已断开"
                self.session_manager.ai_output.emit(
                    session_id, f"[快照] 命令没能发出去（{reason}），本次未采集")
                self.status_bar.showMessage(f"快照未采集：{reason}", 5000)
                return

            start = time.time()
            last_len = 0
            last_change = time.time()
            while time.time() - start < timeout:
                current_len = session.capture_size()
                if current_len != last_len:
                    last_len = current_len
                    last_change = time.time()
                elif time.time() - last_change >= settle:
                    break
                time.sleep(0.2)

            text = session.stop_capture()
        except Exception as e:
            session.stop_capture()
            self.session_manager.ai_output.emit(session_id, f"[快照] 采集失败: {e}")
            return

        if not text.strip():
            self.session_manager.ai_output.emit(session_id, "[快照] 未采集到内容，请确认命令是否有输出")
            return

        label = f"{command} @ {time.strftime('%H:%M:%S')}"
        session.add_snapshot(label, text)
        self.session_manager.ai_output.emit(
            session_id,
            f"[快照] 已保存 {label}（{len(text)} 字符，当前共 {len(session.snapshots)} 份）"
        )

    def compare_snapshots(self):
        """对比最近两份快照；只有一份时与当前输出对比"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        if not session:
            self.status_bar.showMessage("请先建立一个会话", 3000)
            return

        if len(session.snapshots) >= 2:
            old_label, old_text, _ = session.previous_snapshot()
            new_label, new_text, _ = session.last_snapshot()
            dialog = DiffDialog(self, session, autofill=False)
            dialog.left_edit.setPlainText(old_text)
            dialog.right_edit.setPlainText(new_text)
            dialog.summary_label.setText(f"对比快照: {old_label}  →  {new_label}")
            dialog.run_diff()
            dialog.exec()
            return

        if len(session.snapshots) == 1:
            label, _text, _ = session.last_snapshot()
            dialog = DiffDialog(self, session, autofill=False)
            dialog.left_edit.setPlainText(_text)
            dialog.right_edit.setPlainText(session.peek_output())
            dialog.summary_label.setText(f"对比快照: {label}  →  当前输出")
            dialog.run_diff()
            dialog.exec()
            return

        QMessageBox.information(
            self, "没有快照",
            "当前会话还没有配置快照。\n请先执行「工具 → 采集配置快照」。"
        )

    # ------------------------------------------------------------ 提示词 / 日志
    def edit_system_prompt(self):
        """编辑系统提示词"""
        dialog = PromptDialog(self, self.settings.system_prompt)
        if not dialog.exec():
            return
        text = dialog.get_prompt()
        self.settings.set("system_prompt", text)
        self.settings.save()
        # 立即应用到所有已启用 AI 的会话
        for session in self.session_manager.get_all_sessions().values():
            if session.ai_agent:
                session.ai_agent.set_system_prompt(text)
        self.status_bar.showMessage(
            "系统提示词已保存" + ("（已恢复内置）" if not text else "（自定义）"), 4000
        )

    def open_ai_settings(self):
        """工具菜单：打开 AI 设置对话框"""
        dialog = AISettingsDialog(self, control=self)
        dialog.saved.connect(self.on_ai_settings_saved)
        dialog.exec()

    def on_ai_settings_saved(self):
        """AI 设置保存后：新凭据对已有会话生效，并刷新侧栏的当前配置提示"""
        self.on_credentials_changed(self.current_ai_credentials())
        self.ai_panel.update_ai_config_label()

    def on_auto_log_toggled(self, checked: bool):
        """开关自动日志"""
        self.settings.auto_log = checked
        self.settings.save()
        self.session_manager.set_auto_log(checked)
        self.status_bar.showMessage(
            f"自动记录会话日志已{'开启' if checked else '关闭'}", 3000
        )

    def on_confirm_config_toggled(self, checked: bool):
        """开关"配置命令下发前需确认"安全闸门"""
        self.settings.confirm_config_commands = checked
        self.settings.save()
        self.update_safety_status()      # 状态栏常驻指示同步
        self.status_bar.showMessage(
            "配置命令下发前需确认：已开启" if checked else
            "⚠ 已关闭配置命令确认，程序可能直接下发配置命令", 6000
        )

    def open_log_dir(self):
        """用系统文件管理器打开日志目录"""
        path = self.settings.log_dir
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        self.open_path(path)

    # ------------------------------------------------------------ 会话保存/恢复
    def save_session_state(self):
        """退出前保存会话列表"""
        include_password = self.settings.save_passwords
        sessions = self.session_manager.export_sessions(include_password=include_password)
        if include_password:
            for cfg in sessions:
                if cfg.get('password'):
                    cfg['password'] = Settings.encode_password(cfg['password'])
        self.settings.set_last_sessions(sessions)
        self.settings.save()

    def maybe_restore_sessions(self):
        """启动时询问是否恢复上次会话"""
        if not self.settings.restore_sessions:
            return
        if not self.settings.get_last_sessions():
            return
        self.restore_last_sessions()

    def restore_last_sessions(self):
        """恢复上次保存的会话"""
        sessions = self.settings.get_last_sessions()
        if not sessions:
            self.status_bar.showMessage("没有可恢复的会话记录", 3000)
            return

        answer = QMessageBox.question(
            self, "恢复上次会话",
            f"检测到上次的 {len(sessions)} 个会话，是否重新连接？\n\n"
            + "\n".join(f"· {s.get('name', '未命名')}  {s.get('protocol', '')}  "
                        f"{s.get('host', '')}" for s in sessions[:8])
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        for cfg in sessions:
            config = dict(cfg)
            if config.get('password'):
                config['password'] = Settings.decode_password(config['password'])
            config.pop('remember', None)
            self.create_session(config)

    # ------------------------------------------------------------ 本地资料库
    def open_knowledge_dialog(self):
        """打开本地资料库管理窗口"""
        dialog = KnowledgeDialog(self, self.session_manager.knowledge_base)
        dialog.knowledge_changed.connect(self.on_knowledge_changed)
        dialog.exec()

    def import_knowledge_files(self):
        """核心入口：把下载好的文件导入资料库"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要导入的命令手册 / 文档（可多选）", "",
            build_file_filter()
        )
        if not paths:
            return

        kb = self.session_manager.knowledge_base
        if kb is None:
            QMessageBox.warning(self, "无法导入", "本地资料库不可用。")
            return

        before = {doc["path"] for doc in kb.list_documents()}
        result = kb.import_paths(paths)
        added = len(result["added"])
        lines = [f"已导入 {added} 个文件到资料库：", f"{kb.root}", ""]
        if result["added"]:
            lines.extend(f"  · {name}" for name in result["added"][:15])
            if len(result["added"]) > 15:
                lines.append(f"  …… 其余 {len(result['added']) - 15} 个")
        if result["errors"]:
            lines.append("")
            lines.extend(f"  ! {os.path.basename(p)}: {msg}"
                         for p, msg in result["errors"][:8])
        if result["extracted"]:
            lines.append("")
            lines.append("以下文件暂未抽取到文本：")
            lines.extend(f"  ! {name}: {reason}" for name, reason in result["extracted"][:8])

        lines.append("")
        lines.append("正在建立索引，完成后 AI 查命令时会优先检索这些资料。")
        QMessageBox.information(self, "导入资料", "\n".join(lines))

        # 后台建索引，避免大 PDF 卡住界面
        self.status_bar.showMessage("正在为导入的资料建立索引…")
        self.build_knowledge_index_bg(force=True)

        # 再后台把这次新导入的文档整理成「技术解释 + 命令清单」：
        # 技术解释供 AI 检索作答，命令清单进命令自动提示
        new_paths = [doc["path"] for doc in kb.list_documents()
                     if doc["path"] not in before]
        if new_paths:
            QTimer.singleShot(
                500, lambda paths=list(new_paths): self.start_document_digest(paths))

    def release_knowledge_memory(self):
        """工具菜单：把资料库索引从内存里丢掉（腾地方给模拟器等程序）"""
        try:
            freed = self.session_manager.release_knowledge()
        except Exception as e:                       # noqa: BLE001
            self.status_bar.showMessage(f"释放内存失败: {type(e).__name__}: {e}", 6000)
            return
        self.on_knowledge_changed()
        self.status_bar.showMessage(
            f"已释放资料库索引：约 {freed:.0f} MB 内存；下次检索会自动重新加载", 8000)

    def start_local_model_bg(self):
        """后台拉起本地模型服务（起在显存上；界面状态由 AI 面板自行探测显示）"""
        from core import local_model

        def worker():
            try:
                local_model.start_server()
            except Exception:                        # noqa: BLE001
                pass                                 # 起不来就保持"未启动"，不打扰用户

        threading.Thread(target=worker, name="local-model-autostart", daemon=True).start()

    def build_knowledge_index_bg(self, force: bool = False):
        """后台建立/更新资料库索引（完成后用信号回到主线程刷新界面）"""
        kb = self.session_manager.knowledge_base
        if kb is None:
            return

        def worker():
            # 纯 Python 的索引构建会把 GIL 攥得很紧。把线程切换间隔调细，让 GUI
            # 线程有机会插进来重绘/响应 —— 否则这几秒里界面看起来就是"卡住"。
            old_switch = sys.getswitchinterval()
            sys.setswitchinterval(0.001)
            try:
                summary = kb.build_index(force=force)
                # 顺手把内存索引（词频统计）算好：否则第一次提问要现算，
                # "需求匹配到历史经验立刻给结果"就快不起来
                kb.warm_up()
            except Exception as e:
                summary = {"error": str(e)}
            finally:
                sys.setswitchinterval(old_switch)
            # 信号是线程安全的，会排队到主线程执行
            self.knowledge_index_done.emit(summary)

        threading.Thread(target=worker, daemon=True).start()

    def on_knowledge_indexed(self, summary: dict):
        """索引完成后刷新状态"""
        self.on_knowledge_changed()
        if summary.get("error"):
            self.status_bar.showMessage(f"资料库索引失败: {summary['error']}", 6000)
            return
        documents = summary.get("documents", 0)
        chunks = summary.get("chunks", 0)
        reused = summary.get("reused", 0)
        message = f"资料库索引完成：{documents} 篇文档 / {chunks} 个片段"
        if reused:
            message += f"（复用 {reused} 篇）"
        failed = summary.get("failed") or []
        if failed:
            message += f"，{len(failed)} 篇无法解析"
        self.status_bar.showMessage(message, 6000)

    def on_knowledge_changed(self):
        """资料库内容变化：同步到会话与界面"""
        self.session_manager.refresh_knowledge()
        self.update_knowledge_status()
        self.chat_view.add_system("[资料库] 本地资料已更新，AI 查命令时会优先检索本地文档")

    def update_knowledge_status(self):
        """刷新 AI 面板上的资料库状态

        ⚠ 索引没加载时走 light 模式：本方法**启动时就会调一次**，而索引
        （`.index.json` 676 MB）读进内存要 883 MB —— 用户要跑模拟器时不能因为
        刷新个状态就把它读进来。片段数等真正用资料库（检索）时再统计。
        """
        kb = self.session_manager.knowledge_base
        if kb is None:
            self.ai_panel.set_knowledge_status("本地资料库不可用")
            return
        light = not kb.index_loaded
        try:
            stats = kb.stats(light=light)
        except Exception as e:
            self.ai_panel.set_knowledge_status(f"资料库读取失败: {e}")
            return

        if not stats["enabled"]:
            self.ai_panel.set_knowledge_status("本地资料检索已关闭（可在“管理资料库”里打开）")
            return
        if stats["documents"] == 0:
            self.ai_panel.set_knowledge_status(
                "尚未导入资料。点“导入资料文件…”把下载好的命令手册放进来，"
                "AI 查命令时就会优先查它们。"
            )
            return
        if light:
            text = (f"已导入 {stats['documents']} 篇资料（{self.human_size(stats['size'])}）"
                    f"｜片段数在首次检索时统计（索引未加载，省内存）")
        else:
            text = (f"已导入 {stats['documents']} 篇资料、{stats['chunks']} 个检索片段"
                    f"（{self.human_size(stats['size'])}）")
        if stats["failed"]:
            text += f"，{stats['failed']} 篇无法解析"
        self.ai_panel.set_knowledge_status(text)

    # ------------------------------------------------------------ 知识沉淀
    def on_save_note(self, title: str, content: str, category: str = "AI笔记"):
        """把 AI 回答 / 命令清单沉淀到本地资料库（先弹窗让用户确认）"""
        kb = self.session_manager.knowledge_base
        if kb is None:
            QMessageBox.warning(self, "无法保存", "本地资料库不可用。")
            return

        session = self.session_manager.get_session(self.current_session_id())
        meta = self.build_note_meta(session)
        tags = self.suggest_tags(session, title, content)

        dialog = NoteDialog(self, title=title, content=content,
                            category=category, tags=tags, root_dir=kb.root)
        if not dialog.exec():
            return

        values = dialog.get_values()
        if not values["content"]:
            QMessageBox.information(self, "内容为空", "内容为空，未保存。")
            return
        self.save_note_to_kb(values["title"], values["content"],
                             values["category"], values["tags"], meta)

    def build_note_meta(self, session) -> dict:
        """整理这份笔记的来源信息"""
        meta = {"来源": "NetOps Terminal AI 助手"}
        if not session:
            return meta
        target = session.config.get('host', '') or session.config.get('serial_port', '')
        meta["设备"] = f"{session.name}（{session.protocol} {target}）".strip()
        if session.vendor and session.vendor != "unknown":
            meta["厂商"] = session.vendor
        if session.ai_task:
            meta["任务"] = session.ai_task
        return meta

    @staticmethod
    def suggest_tags(session, title: str, content: str) -> str:
        """按会话与内容猜几个标签，方便以后检索"""
        tags = []
        if session:
            if session.vendor and session.vendor != "unknown":
                tags.append(session.vendor)
            tags.append(session.protocol)
            if session.name:
                tags.append(session.name)
        # 从内容里挑几个像命令/型号的词
        for token in re.findall(r'[A-Za-z][A-Za-z0-9\-]{3,}', content or "")[:60]:
            if token.lower() in ("display", "show", "http", "https", "local", "both"):
                continue
            if token not in tags:
                tags.append(token)
            if len(tags) >= 8:
                break
        return ", ".join(tags[:8])

    def save_note_to_kb(self, title: str, content: str, category: str = "AI笔记",
                        tags="", meta: dict = None):
        """实际写入资料库并增量重建索引（供界面与测试调用）"""
        kb = self.session_manager.knowledge_base
        if kb is None:
            return None

        path = kb.save_note(title, content, category=category, tags=tags, meta=meta)
        if not path:
            QMessageBox.warning(self, "保存失败", kb.last_error or "未知错误")
            return None

        # 增量索引：只处理新增的这个文件，已索引的文档会复用
        self.build_knowledge_index_bg(force=False)
        self.update_knowledge_status()
        self.status_bar.showMessage(
            f"已存为资料: {os.path.basename(path)}（AI 以后查命令时能检索到）", 8000
        )
        try:
            self.chat_view.add_system(f"[资料库] 已沉淀: {os.path.relpath(path, kb.root)}")
        except Exception:
            pass
        return path

    def open_path(self, path: str):
        """用系统文件管理器打开目录"""
        try:
            if sys.platform.startswith('win'):
                os.startfile(path)  # noqa: S606
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception as e:
            QMessageBox.information(self, "打开目录", f"{path}\n\n打开失败: {e}")

    @staticmethod
    def human_size(size: int) -> str:
        """人类可读的大小"""
        value = float(size or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} GB"

    # ------------------------------------------------------------ 资料检索
    def on_search_config_changed(self):
        """检索配置变化：重建检索客户端并同步到已启用 AI 的会话"""
        self.session_manager.refresh_search_client()
        backend = self.ai_panel.backend_combo.currentData()
        if backend == "off":
            self.status_bar.showMessage("已关闭联网检索（本地资料库仍可用）", 3000)
        else:
            self.status_bar.showMessage("检索配置已更新", 2000)

    def on_research_requested(self, question: str):
        """资料检索请求（先本地资料库，再联网）"""
        session_id = self.current_session_id()
        if not session_id:
            self.status_bar.showMessage("请先建立一个会话", 3000)
            return
        session = self.session_manager.get_session(session_id)
        if not session:
            return

        if not session.ai_enabled:
            api_key = self.ai_panel.get_api_key()
            if not api_key:
                QMessageBox.warning(self, "缺少 API Key",
                                    "请先在『AI 高级 → AI 助手』页点『AI 设置…』填写 API Key。")
                return
            self.session_manager.enable_ai(
                session_id, **self.current_ai_credentials(), task=session.ai_task
            )
            tab = self.tabs.get(session_id)
            if tab:
                tab.set_ai_badge(True)
            self.action_ai.setChecked(True)
            self.action_ai.setText("AI: ON")
            self.status_ai.setText("AI: ON")

        threading.Thread(
            target=self._research_worker,
            args=(session_id, question),
            daemon=True
        ).start()

    def _research_worker(self, session_id: int, question: str):
        """后台执行联网查询"""
        try:
            answer = self.session_manager.ai_research(session_id, question)
            if not answer:
                self.session_manager.ai_research_ready.emit(
                    session_id, "[未获取到回答] 请检查 API Key、检索后端或网络连通性。", []
                )
        except Exception as e:
            self.session_manager.ai_research_ready.emit(
                session_id, f"[错误] 联网查询异常: {e}", []
            )

    def on_research_ready(self, session_id: int, answer: str, sources: list):
        """展示联网查询结果"""
        self.ai_panel.show_research_result(answer, sources)
        self.status_bar.showMessage(f"联网查询完成，参考来源 {len(sources or [])} 条", 5000)
        self.chat_view.add_system(
            f"[联网查询] 已完成，参考来源 {len(sources or [])} 条（详见“资料检索”页签）"
        )

    def on_connection_lost(self, session_id: int, reason: str):
        """设备/网络掉线：必须在屏幕上留下痕迹

        之前连接器发出了 connection_lost，但没有任何界面代码接它 —— 掉线后终端就
        静止在那里，用户看到的是一个"不动了但看起来还连着"的窗口，分不清是设备
        没反应、命令跑太久，还是已经断了（2026-10-19 用户反馈"掉线要显示出来"）。
        """
        tab = self.tabs.get(session_id)
        stamp = time.strftime("%H:%M:%S")
        message = f"[连接已断开 {stamp}] {reason}"
        if tab:
            # 紧跟设备输出之后再空一行，避免和最后一行输出粘在一起
            tab.append_output(f"\r\n{message}\r\n")
            tab.set_status(False, f"已断开 {stamp}：{reason}")
        self.chat_view.add_system(f"[掉线] {reason}")
        self.status_bar.showMessage(f"会话 {session_id} 已断开：{reason}", 8000)
        self.update_status()

    def on_connection_info(self, session_id: int, text: str):
        """连接过程提示（如串口自动重连）"""
        self.status_bar.showMessage(text, 5000)
        self.chat_view.add_system(f"[连接] {text}")
        tab = self.tabs.get(session_id)
        if tab:
            # "重连成功"是好事，不能把状态灯按成断开的红色（旧代码一律按 False）
            ok = "成功" in text
            tab.set_status(ok, text)
            if ok:
                stamp = time.strftime("%H:%M:%S")
                tab.append_output(f"\r\n[已重新连上 {stamp}] {text}\r\n")
                self._input_dropped_notified.discard(session_id)
        if "成功" in text:
            self.update_status()

    # ------------------------------------------------------------ 串口控制
    def open_serial_control(self):
        """打开串口控制面板"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        if not session:
            self.status_bar.showMessage("请先建立一个会话", 3000)
            return
        connector = session.connector
        if not connector or not connector.supports_serial_control():
            QMessageBox.information(
                self, "不适用",
                "当前会话不是串口（Console）连接，串口控制仅对 Serial 协议有效。"
            )
            return
        SerialControlDialog(self, connector, session.name).exec()

    # ------------------------------------------------------------ 多设备并发巡检
    def open_patrol_dialog(self):
        """打开多设备并发巡检窗口"""
        sessions = []
        for session in self.session_manager.get_all_sessions().values():
            config = dict(session.config)
            config['name'] = session.name
            if not config.get('host'):
                config['host'] = config.get('serial_port', '')
            # 巡检要"按厂家自动套命令"，厂家必须跟着设备走：连接后识别出来的
            # session.vendor 最准，记录里自带的 vendor 次之
            if session.vendor and session.vendor != 'unknown':
                config['vendor'] = session.vendor
            sessions.append(config)

        candidates = []
        for item in self.settings.get_recent_connections():
            candidate = dict(item)
            candidate['source'] = '最近连接'
            candidates.append(candidate)

        dialog = PatrolDialog(self, candidates=candidates, sessions=sessions)
        dialog.exec()

    # ------------------------------------------------------------ 其他
    def restore_geometry(self):
        """恢复窗口尺寸：限制在屏幕可用区域内，并居中显示

        不做限制的话，保存的高度一旦超过屏幕可用高度，窗口底部（对话输入框、
        状态栏）就会被挤出屏幕外，用户会觉得"面板上没有输入框"。
        没有历史尺寸时按屏幕比例取默认值（参考 NetCatty 那种占比）。
        """
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        win = self.settings.get("window") or {}

        try:
            width = int(win.get("width") or 0)
            height = int(win.get("height") or 0)
        except (TypeError, ValueError):
            width = height = 0

        if area is not None:
            max_w = max(640, int(area.width() * 0.92))
            max_h = max(480, int(area.height() * 0.92))
            if width <= 0:
                width = int(area.width() * 0.86)     # 首次启动：屏宽 86%
            if height <= 0:
                height = int(area.height() * 0.88)   # 首次启动：屏高 88%
            # 夹在 [合理下限, 屏幕可用区域] 之间，任何显示器上都不会溢出
            width = max(min(width, max_w), min(900, max_w))
            height = max(min(height, max_h), min(600, max_h))
            self.resize(width, height)
            self.move(area.x() + (area.width() - width) // 2,
                      area.y() + (area.height() - height) // 2)
        else:
            try:
                self.resize(width or 1400, height or 900)
            except Exception:
                pass

    def show_about(self):
        QMessageBox.about(
            self, "关于 NetOps Terminal",
            "<h3>NetOps Terminal v0.7</h3>"
            "<p>集成 AI 辅助功能的网络设备终端工具。</p>"
            "<p>支持 SSH / Telnet / Serial 连接，可分析设备输出并生成配置命令；"
            "提供命令模板批量下发、配置快照比对、会话日志落盘、会话恢复等运维能力。</p>"
            "<p>技术栈: PyQt6 · Paramiko · pyserial · Anthropic SDK</p>"
        )

    def closeEvent(self, event):
        """关闭窗口"""
        self.settings.set("window", {
            "width": self.width(),
            "height": self.height(),
        })
        self.save_session_state()
        self.session_manager.cleanup()
        if self.control_server is not None:      # 停服务并删掉发现文件
            self.control_server.stop()
        event.accept()


# 主题样式见 resources/styles/dark.qss（形制参考 PyCharm Darcula），
# 兜底样式与配色 token 在 ui/theme.py —— 这里不再重复一份内置样式。
