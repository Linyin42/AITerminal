"""
主窗口 - AITerminal主界面
"""
import os
import re
import sys
import time
import subprocess
import threading

from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QDockWidget,
    QStatusBar, QMessageBox, QApplication, QLabel, QProgressDialog,
    QFileDialog, QWidget
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction

from core.session import SessionManager
from core.terminal import detect_vendor
from core.batch import BatchRunner
from ui.session_tab import SessionTab
from ui.ai_panel import AIPanel
from ui.chat_panel import ChatView
from ui.vault_panel import VaultPanel
from ui.connect_dialog import ConnectDialog
from ui.template_dialog import TemplateDialog
from ui.diff_dialog import DiffDialog
from ui.prompt_dialog import PromptDialog
from ui.serial_dialog import SerialControlDialog
from ui.knowledge_dialog import KnowledgeDialog, build_file_filter
from ui.note_dialog import NoteDialog
from ui.patrol_dialog import PatrolDialog
from ui.command_confirm import ConfigConfirmDialog
from core.safety import has_config_command
from config.settings import Settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MainWindow(QMainWindow):
    """AITerminal主窗口"""

    #: 资料库后台索引完成（从工作线程发回主线程，避免跨线程操作界面）
    knowledge_index_done = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.session_manager = SessionManager()
        self.tabs = {}                 # session_id -> SessionTab
        self.ai_auto_sessions = set()  # 处于自动模式的会话
        self.batch_runner = None       # 当前批处理任务
        self.progress_dialog = None    # 批处理进度对话框
        self.knowledge_index_done.connect(self.on_knowledge_indexed)
        self.init_ui()
        self.init_signals()
        self.init_timer()
        self.restore_geometry()
        # 稍后询问是否恢复上次会话，确保窗口先显示出来
        QTimer.singleShot(400, self.maybe_restore_sessions)
        # 启动后后台预热本地资料库索引，避免首次检索等待
        self.update_knowledge_status()
        QTimer.singleShot(1500, lambda: self.build_knowledge_index_bg(force=False))

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        """初始化界面"""
        self.setWindowTitle("AITerminal - AI智能终端 v0.7")
        self.setGeometry(100, 100, 1400, 900)

        # 中央标签区
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.setMovable(True)
        self.tab_widget.tabCloseRequested.connect(self.close_session)
        self.tab_widget.currentChanged.connect(self.on_tab_changed)
        self.setCentralWidget(self.tab_widget)

        # 左侧：设备库 Vault
        self.vault_dock = QDockWidget("设备库", self)
        self.vault_dock.setObjectName("VaultDock")
        self.vault_panel = VaultPanel()
        self.vault_dock.setWidget(self.vault_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.vault_dock)
        self.vault_dock.setTitleBarWidget(QWidget())   # 隐藏标题栏（NetCatty 风格）

        # 中间：AI 助手（Catty 对话 + AI 高级），占比较窄
        self.ai_dock = QDockWidget("AI 助手", self)
        self.ai_dock.setObjectName("AIDock")
        self.ai_panel = AIPanel()
        self.chat_view = ChatView()
        self.right_tabs = QTabWidget()
        self.right_tabs.addTab(self.chat_view, "Catty 对话")
        self.right_tabs.addTab(self.ai_panel, "AI 高级")
        self.ai_dock.setWidget(self.right_tabs)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.ai_dock)
        self.ai_dock.setTitleBarWidget(QWidget())   # 隐藏标题栏
        # 把 AI 助手放到设备库右侧、终端左侧
        self.splitDockWidget(self.vault_dock, self.ai_dock, Qt.Orientation.Horizontal)

        # 初始宽度：设备库 280 / AI 助手 320 / 终端占最大
        self.resizeDocks(
            [self.vault_dock, self.ai_dock],
            [280, 320], Qt.Orientation.Horizontal
        )

        # 用已保存的设备库初始化左侧 Vault
        self.vault_panel.set_devices(self.settings.get_vault_devices())

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
        """载入 QSS 样式表，缺失时使用内置暗色样式"""
        qss_path = os.path.join(BASE_DIR, "resources", "styles", "dark.qss")
        if os.path.exists(qss_path):
            try:
                with open(qss_path, 'r', encoding='utf-8') as f:
                    self.setStyleSheet(f.read())
                return
            except Exception:
                pass
        self.setStyleSheet(DEFAULT_QSS)

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
            lambda: self.vault_dock.setVisible(not self.vault_dock.isVisible()))
        view_menu.addAction(act_toggle_vault)
        act_toggle_ai = QAction("显示/隐藏 AI 面板", self)
        act_toggle_ai.triggered.connect(
            lambda: self.ai_dock.setVisible(not self.ai_dock.isVisible()))
        view_menu.addAction(act_toggle_ai)

        # ---------------------------------------------------------- 工具
        tool_menu = menubar.addMenu("工具(&T)")

        # 安全闸门放最前面 —— 这是最要紧的开关，不该藏在菜单底部
        self.act_confirm_config = QAction("配置命令下发前需确认", self)
        self.act_confirm_config.setCheckable(True)
        self.act_confirm_config.setChecked(self.settings.confirm_config_commands)
        self.act_confirm_config.setToolTip(
            "开启后，包含配置类命令的批次会先弹出命令清单等你确认；\n"
            "AI 自动模式下生成的配置命令也会转入待执行队列而不会自动下发"
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

        # ---------------------------------------------------------- 资料（新建）
        doc_menu = menubar.addMenu("资料(&D)")
        act_search = QAction("资料检索（查命令与技术资料）…", self)
        act_search.setToolTip("先查本地资料库，再联网检索命令与技术资料")
        act_search.triggered.connect(self.focus_research_tab)
        doc_menu.addAction(act_search)
        act_import_docs = QAction("导入资料文件…（下载好的命令手册放这里）", self)
        act_import_docs.setToolTip(
            "把下载好的命令手册 / 配置案例导入本地资料库，供 AI 查命令时使用")
        act_import_docs.triggered.connect(self.import_knowledge_files)
        doc_menu.addAction(act_import_docs)
        doc_menu.addSeparator()
        act_kb = QAction("本地资料库管理…", self)
        act_kb.setToolTip("列表、检索测试、更换目录、重建索引")
        act_kb.triggered.connect(self.open_knowledge_dialog)
        doc_menu.addAction(act_kb)
        act_kb_dir = QAction("打开资料库目录", self)
        act_kb_dir.triggered.connect(self.open_knowledge_dir)
        doc_menu.addAction(act_kb_dir)
        doc_menu.addSeparator()
        act_save_note = QAction("把当前 AI 输出存为资料…", self)
        act_save_note.setToolTip("把 AI 生成的命令沉淀到本地资料库，以后可直接检索")
        act_save_note.triggered.connect(self.ai_panel.on_save_output)
        doc_menu.addAction(act_save_note)

        # ---------------------------------------------------------- 帮助
        help_menu = menubar.addMenu("帮助(&H)")
        act_about = QAction("关于 AITerminal", self)
        act_about.triggered.connect(self.show_about)
        help_menu.addAction(act_about)

    def init_signals(self):
        """连接会话管理器与AI面板的信号"""
        sm = self.session_manager
        sm.session_created.connect(self.on_session_created)
        sm.session_closed.connect(self.on_session_closed)
        sm.session_error.connect(self.on_session_error)
        sm.ai_output.connect(self.on_ai_output)
        sm.ai_command.connect(self.on_ai_command)
        sm.ai_research_ready.connect(self.on_research_ready)
        sm.connection_info.connect(self.on_connection_info)

        self.ai_panel.analyze_requested.connect(self.on_analyze_requested)
        self.ai_panel.execute_requested.connect(self.execute_last_command)
        self.ai_panel.explain_requested.connect(self.on_explain_requested)
        self.ai_panel.auto_mode_changed.connect(self.on_auto_mode_changed)
        self.ai_panel.credentials_changed.connect(self.on_credentials_changed)
        self.ai_panel.connection_tested.connect(self.on_api_connection_tested)
        self.ai_panel.research_requested.connect(self.on_research_requested)
        self.ai_panel.search_config_changed.connect(self.on_search_config_changed)
        self.ai_panel.queue_execute_requested.connect(self.on_queue_execute)
        self.ai_panel.import_knowledge_requested.connect(self.import_knowledge_files)
        self.ai_panel.manage_knowledge_requested.connect(self.open_knowledge_dialog)
        self.ai_panel.save_note_requested.connect(self.on_save_note)

        # 左侧设备库 Vault
        self.vault_panel.connect_requested.connect(self.on_vault_connect)
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
        # 100ms 刷新终端显示（从连接器缓冲区取数据）
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_output)
        self.timer.start(100)

        # 1s 驱动 AI 自动模式
        self.ai_timer = QTimer()
        self.ai_timer.timeout.connect(self.ai_tick)
        self.ai_timer.start(1000)

    # ------------------------------------------------------------ 会话
    def show_connect_dialog(self):
        """显示连接对话框"""
        dialog = ConnectDialog(self)
        if dialog.exec():
            config = dialog.get_config()
            self.create_session(config)

    def create_session(self, config: dict):
        """创建新会话"""
        self.status_bar.showMessage(f"正在连接 {config.get('host', '')} …")
        QApplication.processEvents()

        session_id = self.session_manager.create_session(
            name=config.get('name', '新建连接'),
            protocol=config.get('protocol', 'SSH'),
            host=config.get('host', ''),
            username=config.get('username', ''),
            password=config.get('password', ''),
            port=config.get('port', 22),
            serial_port=config.get('serial_port', ''),
            baudrate=config.get('baudrate', 9600),
            key_filename=config.get('key_filename'),
            # 串口 / Console 参数
            bytesize=config.get('bytesize', 8),
            parity=config.get('parity', 'N'),
            stopbits=config.get('stopbits', 1),
            flow=config.get('flow', 'none'),
            dtr=config.get('dtr'),
            rts=config.get('rts'),
            line_ending=config.get('line_ending'),
            char_delay_ms=config.get('char_delay_ms', 0),
            auto_reconnect=config.get('auto_reconnect', True),
            reconnect_interval=config.get('reconnect_interval', 2.0),
            local_echo=config.get('local_echo', False),
        )

        if session_id > 0:
            self.status_bar.showMessage(f"已连接到 {config.get('host', '')}")

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

        index = self.tab_widget.addTab(tab, name)
        self.tab_widget.setCurrentIndex(index)
        self.session_manager.set_active_session(session_id)
        self.update_status()

        # Console 会话：按配置开启本地回显，并提示串口控制入口
        session = self.session_manager.get_session(session_id)
        if session and session.protocol == "Serial":
            local_echo = bool(session.config.get('local_echo'))
            tab.terminal.set_local_echo(local_echo)
            tab.set_status(True, f"{session.connector.info()}"
                                 + ("（本地回显）" if local_echo else ""))
            self.on_ai_output(
                session_id,
                "[提示] Console 会话已建立。若设备无回显请开启“本地回显”，"
                "需进 BootROM 可用工具栏“串口控制”发送 Break。"
            )
        elif session and session.vendor != "unknown":
            self.ai_panel.set_quick_commands(session.vendor)

        # 把新连上的设备登记进左侧 Vault（去重，不存密码），并同步聊天面板设备条
        if session:
            self.register_vault_device(session)
            self.sync_chat_device(session)

        self.sync_session_context()

    def close_session(self, index: int):
        """关闭会话"""
        widget = self.tab_widget.widget(index)
        if widget is None:
            return
        session_id = widget.property('session_id')
        if session_id:
            self.session_manager.close_session(session_id)
            self.tab_widget.removeTab(index)
            self.tabs.pop(session_id, None)
            self.ai_auto_sessions.discard(session_id)
        self.update_status()

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
        self.update_status()
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
        self.update_status()

    def sync_session_context(self):
        """按当前会话同步界面：串口按钮可用性、厂商快捷命令、屏幕预览"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None

        # 聊天面板始终绑定当前设备
        if session:
            self.sync_chat_device(session)
            self.vault_panel.set_connected(session.config, connected=True)

        # 串口控制只在 Console 会话下可用
        is_serial = bool(session and session.protocol == "Serial"
                         and session.connector
                         and session.connector.supports_serial_control())
        self.action_serial.setEnabled(is_serial)

        if session:
            self.ai_panel.set_quick_commands(session.vendor)
        self.update_screen_preview()

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

    def on_vault_connect(self, config: dict):
        """双击/点击“连接”某台设备：弹连接对话框补全密码后连接"""
        if not config:
            return
        # 设备库存的是脱敏配置（无密码），连接前用对话框补全凭据
        dialog = ConnectDialog(self)
        dialog.set_config(config)
        if dialog.exec():
            self.create_session(dialog.get_config())

    def on_vault_edit(self, config: dict):
        """编辑一台已保存的设备信息"""
        if not config:
            return
        dialog = ConnectDialog(self)
        dialog.set_config(config)
        if dialog.exec():
            new_config = dialog.get_config()
            # 更新设备库（按 协议+主机 匹配），并刷新列表
            self.settings.update_vault_device(new_config)
            self.vault_panel.set_devices(self.settings.get_vault_devices())

    def on_vault_delete(self, config: dict):
        """从设备库删除一台设备"""
        if not config:
            return
        self.settings.remove_vault_device(config)
        self.vault_panel.set_devices(self.settings.get_vault_devices())

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
        session = self._active_session()
        if not session:
            self.chat_view.add_error("还没有连接设备。请在左侧设备库双击一台设备先连接。")
            return
        agent = self.ensure_session_ai_enabled(session)
        if not agent:
            self.chat_view.add_error(
                "未配置 AI：请打开右侧『AI 高级』页，选择提供方并填写 API Key。")
            return
        self.chat_view.add_user(text)
        self.chat_view.set_busy(True)
        threading.Thread(
            target=self._chat_worker, args=(session.session_id, text),
            daemon=True
        ).start()

    def _chat_worker(self, session_id: int, text: str):
        try:
            session = self.session_manager.get_session(session_id)
            if not session or not session.ai_agent:
                self.chat_view.add_error("AI 未就绪，请确认已配置 API Key。")
                return
            device_output = session.peek_output() or session.recent_output or ""
            reply_text, commands = session.ai_agent.converse(
                text, device_output, task=session.ai_task)
            if not reply_text and not commands:
                self.chat_view.add_error(
                    "AI 没有返回内容，可能是网络不畅或密钥无效。")
                return
            self.chat_view.add_ai(reply_text, commands)
            # 同步一份到 AI 高级面板，保持兼容
            self.ai_panel.append_output(f"[Catty] 用户: {text}")
            if commands:
                self.ai_panel.set_last_command(commands[-1])
                self.ai_panel.add_commands(commands)
        except Exception as e:
            self.chat_view.add_error(f"AI 调用失败：{e}")
        finally:
            self.chat_view.set_busy(False)

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
            self.chat_view.add_error("未配置 AI：请在『AI 高级』页填写 API Key。")
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
            self.chat_view.add_ai(answer, [])
            self.ai_panel.append_output(f"[Catty] 解释输出:\n{answer}")
        except Exception as e:
            self.chat_view.add_error(f"AI 解释失败：{e}")
        finally:
            self.chat_view.set_busy(False)

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
            self.chat_view.add_error("未配置 AI：请在『AI 高级』页填写 API Key。")
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
            self.chat_view.add_ai(body, [])
        except Exception as e:
            self.chat_view.add_error(f"资料检索失败：{e}")
        finally:
            self.chat_view.set_busy(False)

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
        """发送原始输入（回车/Tab等）"""
        connector = self.session_manager.get_connector(session_id)
        if connector and connector.is_connected():
            connector.send_raw(text)

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
                    "请先在右侧 AI 面板选择提供方并填写 API Key。\n\n"
                    "支持 Anthropic Claude、DeepSeek 以及任意 OpenAI 兼容接口。"
                )
                return
            task = self.ai_panel.get_task()
            credentials = self.current_ai_credentials()
            auto = self.ai_panel.get_auto_mode()
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
            text, color = "API: ✓ 已连通", "#3fb950"
        elif ok is False:
            text, color = "API: ✗ 连接失败", "#f85149"
        else:
            creds = self.current_ai_credentials()
            if creds.get("api_key"):
                text, color = "API: 未验证", "#98a1b5"
            else:
                text, color = "API: 未配置", "#666e82"
        self.status_api.setText(text)
        self.status_api.setStyleSheet(f"color: {color};")
        self.status_api.setToolTip(message or "点『AI 高级』页的『测试连接』验证 Key 是否可用")

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
            self.status_safety.setStyleSheet("color: #3fb950;")
            self.status_safety.setToolTip(
                "配置类命令下发前会先弹出命令清单等你确认（工具菜单可关闭）")
        else:
            self.status_safety.setText("安全闸门: 关 ⚠")
            self.status_safety.setStyleSheet("color: #f85149; font-weight: 650;")
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

    def on_auto_mode_changed(self, enabled: bool):
        """自动模式开关"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        if not session:
            return
        session.ai_auto = enabled
        if enabled:
            if not session.ai_enabled:
                self.status_bar.showMessage("请先启用 AI 开关，再开启自动模式", 3000)
                self.ai_panel.chk_auto.setChecked(False)
                return
            self.ai_auto_sessions.add(session_id)
            self.status_bar.showMessage("已开启自动模式：AI 生成的命令将自动下发", 3000)
        else:
            self.ai_auto_sessions.discard(session_id)
            self.status_bar.showMessage("已关闭自动模式", 2000)

    def on_analyze_requested(self, task: str):
        """AI 面板请求分析；若输入形如命令（如快捷命令）则直接下发"""
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
                                    "请先在 AI 面板选择提供方并填写 API Key。")
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

        self.ai_panel.append_output("[系统] 正在分析当前输出…")
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
        """工具栏：单步分析一次"""
        self.ai_panel.analyze_output()

    def execute_last_command(self):
        """执行 AI 最近生成的一条命令"""
        session_id = self.current_session_id()
        command = self.ai_panel.last_command
        if not session_id or not command:
            self.status_bar.showMessage("没有可执行的 AI 命令", 3000)
            return
        if not self.session_manager.send_command(session_id, command):
            self.status_bar.showMessage("发送失败：连接不可用", 3000)
            return
        self.on_ai_output(session_id, f"[执行] {command}")

    def on_explain_requested(self):
        """请求 AI 解释当前输出"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        if not session:
            self.status_bar.showMessage("请先建立一个会话", 3000)
            return
        if not session.ai_enabled:
            api_key = self.ai_panel.get_api_key()
            if not api_key:
                QMessageBox.warning(self, "缺少 API Key",
                                    "请先在 AI 面板选择提供方并填写 API Key。")
                return
            self.session_manager.enable_ai(
                session_id, **self.current_ai_credentials(), task="")
        threading.Thread(
            target=self._explain_worker, args=(session_id,), daemon=True
        ).start()

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
            # 建议命令写入终端，人工确认后可用"执行AI命令"下发
            for cmd in commands:
                self.session_manager.ai_command.emit(session_id, cmd)
        except Exception as e:
            self.session_manager.ai_output.emit(session_id, f"[错误] {e}")

    def _explain_worker(self, session_id: int):
        try:
            self.session_manager.ai_explain(session_id)
        except Exception as e:
            self.session_manager.ai_output.emit(session_id, f"[错误] {e}")

    def _ai_step_worker(self, session_id: int, auto: bool):
        try:
            commands, blocked, status = self.session_manager.ai_step(session_id, auto)
            # done=任务完成/需人工确认；queued=生成了配置类命令已转入待执行队列
            if status in ("done", "queued"):
                session = self.session_manager.get_session(session_id)
                if session:
                    session.ai_auto = False
                self.ai_auto_sessions.discard(session_id)
                self.ai_panel.chk_auto.setChecked(False)
        except Exception as e:
            self.session_manager.ai_output.emit(session_id, f"[错误] AI 步骤异常: {e}")

    def ai_tick(self):
        """AI 自动模式心跳：输出稳定后触发一轮分析"""
        # 顺带刷新"AI 可见的屏幕内容"预览
        self.update_screen_preview()

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
    def on_ai_output(self, session_id: int, text: str):
        """AI 消息显示到面板"""
        session = self.session_manager.get_session(session_id)
        prefix = f"[{session.name}] " if session else ""
        self.ai_panel.append_output(prefix + text)

    def on_ai_command(self, session_id: int, command: str):
        """AI 生成的命令：写入终端、记录并加入待执行队列"""
        tab = self.tabs.get(session_id)
        if tab:
            tab.append_output(f"\n[AI] {command}\n")
        self.ai_panel.set_last_command(command)
        # 进入"待执行"页签，由用户勾选后统一下发
        self.ai_panel.add_commands([command])

    def on_session_error(self, session_id: int, message: str):
        """连接/会话错误"""
        self.status_bar.showMessage(f"错误: {message}", 5000)
        if session_id > 0:
            self.ai_panel.append_output(f"[错误] {message}")
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
                tab.append_output(output)
            tab.set_status(connector.is_connected())

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
                self.ai_panel.append_output(
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
        self.ai_panel.append_output(
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
        self.ai_panel.append_output(f"[安全拦截] 批量下发跳过第 {index} 条: {command}")

    def on_batch_finished(self, ok: bool, message: str):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        self.status_bar.showMessage(message, 8000)
        self.ai_panel.append_output(f"[批量] {message}")

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

    def build_knowledge_index_bg(self, force: bool = False):
        """后台建立/更新资料库索引（完成后用信号回到主线程刷新界面）"""
        kb = self.session_manager.knowledge_base
        if kb is None:
            return

        def worker():
            try:
                summary = kb.build_index(force=force)
            except Exception as e:
                summary = {"error": str(e)}
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
        self.ai_panel.append_output("[资料库] 本地资料已更新，AI 查命令时会优先检索本地文档")

    def update_knowledge_status(self):
        """刷新 AI 面板上的资料库状态"""
        kb = self.session_manager.knowledge_base
        if kb is None:
            self.ai_panel.set_knowledge_status("本地资料库不可用")
            return
        try:
            stats = kb.stats()
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
        text = (f"已导入 {stats['documents']} 篇资料、{stats['chunks']} 个检索片段"
                f"（{self.human_size(stats['size'])}）")
        if stats["failed"]:
            text += f"，{stats['failed']} 篇无法解析"
        self.ai_panel.set_knowledge_status(text)

    def open_knowledge_dir(self):
        """打开资料库目录"""
        kb = self.session_manager.knowledge_base
        if kb is None:
            return
        try:
            kb.ensure_dir()
        except Exception:
            pass
        self.open_path(kb.root)

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
        meta = {"来源": "AITerminal AI 助手"}
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
            self.ai_panel.append_output(f"[资料库] 已沉淀: {os.path.relpath(path, kb.root)}")
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
    def focus_research_tab(self):
        """切到 AI 面板的资料检索页"""
        self.ai_dock.setVisible(True)
        self.ai_panel.tabs.setCurrentIndex(1)
        self.update_knowledge_status()
        self.ai_panel.question_input.setFocus()

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
                                    "请先在 AI 面板选择提供方并填写 API Key。")
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
        self.ai_panel.append_output(
            f"[联网查询] 已完成，参考来源 {len(sources or [])} 条（详见“联网查询”页签）"
        )

    def on_connection_info(self, session_id: int, text: str):
        """连接过程提示（如串口自动重连）"""
        self.status_bar.showMessage(text, 5000)
        self.ai_panel.append_output(f"[连接] {text}")
        tab = self.tabs.get(session_id)
        if tab:
            tab.set_status(False, text)

    def update_screen_preview(self):
        """把当前会话最近输出同步到 AI 面板的"屏幕内容"预览"""
        session_id = self.current_session_id()
        session = self.session_manager.get_session(session_id) if session_id else None
        if session:
            self.ai_panel.update_screen_preview(session.peek_output())
        else:
            self.ai_panel.update_screen_preview("")

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

    # ------------------------------------------------------------ 待执行队列
    def on_queue_execute(self, commands):
        """执行待执行队列中的命令（复用批量下发）"""
        session_id = self.current_session_id()
        if not session_id:
            self.status_bar.showMessage("请先建立一个会话", 3000)
            return
        self.run_batch(session_id, list(commands), self.settings.batch_delay_ms, True)

    # ------------------------------------------------------------ 多设备并发巡检
    def open_patrol_dialog(self):
        """打开多设备并发巡检窗口"""
        sessions = []
        for session in self.session_manager.get_all_sessions().values():
            config = dict(session.config)
            config['name'] = session.name
            if not config.get('host'):
                config['host'] = config.get('serial_port', '')
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
        """恢复窗口尺寸"""
        win = self.settings.get("window") or {}
        try:
            self.resize(int(win.get("width", 1400)), int(win.get("height", 900)))
        except Exception:
            pass

    def show_about(self):
        QMessageBox.about(
            self, "关于 AITerminal",
            "<h3>AITerminal v0.7</h3>"
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
        event.accept()


# 内置样式（resources/styles/dark.qss 缺失时的兜底）
# 与 resources/styles/dark.qss 的"靛蓝 Indigo / 仿 Netcatty"主题保持一致（精简版）
DEFAULT_QSS = """
QMainWindow, QDialog { background-color: #12141c; }
QWidget { color: #e6e9f2; font-family: "Mona Sans", "Segoe UI", "Microsoft YaHei UI",
    "Microsoft YaHei", system-ui, sans-serif; }
QToolBar { background-color: #171a24; border: none; spacing: 6px; padding: 7px 10px;
    border-bottom: 1px solid #2b3143; }
QToolBar QToolButton { color: #c5cddd; background-color: transparent;
    border: 1px solid transparent; padding: 5px 11px; border-radius: 8px; font-weight: 550; }
QToolBar QToolButton:hover { background-color: rgba(230, 233, 242, 0.08); }
QToolBar QToolButton:checked { background-color: rgba(124, 140, 248, 0.16);
    border-color: rgba(124, 140, 248, 0.55); color: #a5b1ff; }
QTabWidget::pane { border: none; }
QTabBar::tab { background-color: transparent; color: #98a1b5; padding: 8px 16px;
    margin-right: 2px; border-bottom: 2px solid transparent;
    border-radius: 6px 6px 0 0; font-weight: 550; }
QTabBar::tab:selected { color: #e6e9f2; border-bottom: 2px solid #7c8cf8; }
QDockWidget { color: #e6e9f2; }
QDockWidget::title { background-color: #171a24; padding: 6px;
    border-bottom: 1px solid #2b3143; }
QStatusBar { background-color: #171a24; color: #98a1b5;
    border-top: 1px solid #2b3143; }
QGroupBox { border: 1px solid rgba(43, 49, 67, 0.8); border-radius: 12px; margin-top: 12px;
    padding: 14px 10px 10px 10px; background-color: #1a1e29; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #a5b1ff;
    background-color: #12141c; padding: 0 6px; font-weight: 650; }
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {
    background-color: #171a24; color: #e6e9f2; border: 1px solid rgba(43, 49, 67, 0.75);
    padding: 6px 9px; border-radius: 8px; }
QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border-color: rgba(124, 140, 248, 0.65); }
QPushButton { background-color: rgba(38, 43, 58, 0.55); color: #e6e9f2;
    border: 1px solid rgba(43, 49, 67, 0.8); padding: 6px 14px; border-radius: 8px;
    font-weight: 550; }
QPushButton:hover { background-color: #262b3a; border-color: #39415a; }
QListWidget { background-color: #171a24; color: #e6e9f2;
    border: 1px solid rgba(43, 49, 67, 0.8); border-radius: 12px; padding: 4px; }
QListWidget::item { border-radius: 8px; }
QListWidget::item:hover { background-color: rgba(230, 233, 242, 0.06); }
QMenuBar { background-color: #171a24; color: #e6e9f2; }
QMenuBar::item:selected { background-color: rgba(230, 233, 242, 0.08); }
QMenu { background-color: #1e222f; border: 1px solid rgba(43, 49, 67, 0.7); border-radius: 12px; }
QMenu::item { border-radius: 8px; }
QMenu::item:selected { background-color: rgba(124, 140, 248, 0.18); color: #a5b1ff; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar:horizontal { background: transparent; height: 8px; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: rgba(152, 161, 181, 0.18);
    border-radius: 4px; min-height: 28px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QToolTip { background-color: #1e222f; color: #e6e9f2; border: 1px solid rgba(43, 49, 67, 0.7);
    border-radius: 8px; padding: 6px 9px; }
"""
