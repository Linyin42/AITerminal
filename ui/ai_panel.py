"""
AI面板 - AI 辅助 / 联网查询 / 待执行命令

分三个页签:
    1. AI 助手   —— API 配置、任务描述、快捷命令、屏幕内容预览、AI 输出、自动模式
    2. 联网查询  —— 让 AI 上网查厂商命令与报错含义，结果可回填给 AI
    3. 待执行    —— AI 生成的命令进入待执行队列，人工勾选后批量下发
"""
import os
import threading
import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
    QLineEdit, QPushButton, QLabel, QComboBox, QSpinBox,
    QGroupBox, QCheckBox, QListWidget, QListWidgetItem,
    QTabWidget, QSplitter, QMessageBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QUrl
from PyQt6.QtGui import QDesktopServices

from core.llm import PROVIDERS, LLMClient, LLMError, provider_defaults, provider_label
from config.settings import Settings
from config.presets import TASK_TEMPLATES, get_vendor_commands
from ui.output_highlighter import OutputHighlighter

SEARCH_BACKENDS = [
    ("自动（推荐）", "auto"),
    ("百度", "baidu"),
    ("必应", "bing"),
    ("DuckDuckGo", "duckduckgo"),
    ("SearXNG（自建实例）", "searxng"),
    ("关闭联网检索", "off"),
]

#: AI 提供方的展示顺序
PROVIDER_ORDER = ("anthropic", "deepseek", "openai_compat")


class AIPanel(QWidget):
    """
    AI辅助面板

    信号:
        analyze_requested(str):      请求分析当前输出（参数为任务描述）
        execute_requested():         请求执行最近一次 AI 生成的命令
        explain_requested():         请求 AI 解释当前输出
        auto_mode_changed(bool):     自动模式开关变化
        credentials_changed(str, str): API Key / 模型变化
        research_requested(str):     请求联网查询（参数为问题）
        search_config_changed():     检索配置变化
        queue_execute_requested(list): 请求批量执行待执行队列中的命令
    """

    analyze_requested = pyqtSignal(str)
    execute_requested = pyqtSignal()
    explain_requested = pyqtSignal()
    auto_mode_changed = pyqtSignal(bool)
    credentials_changed = pyqtSignal(dict)   # {provider, api_key, model, base_url}
    connection_tested = pyqtSignal(bool, str)  # 测试连接结果: 是否成功, 说明文字
    research_requested = pyqtSignal(str)
    search_config_changed = pyqtSignal()
    queue_execute_requested = pyqtSignal(list)
    import_knowledge_requested = pyqtSignal()   # 点"导入资料文件"
    manage_knowledge_requested = pyqtSignal()   # 点"管理资料库"
    save_note_requested = pyqtSignal(str, str, str)  # 存为资料: 标题, 内容, 分类

    MODELS = [
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        "claude-3-5-haiku-20241022",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = Settings()
        self.last_command = ""
        self.init_ui()
        self.load_settings()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_assistant_tab(), "AI 助手")
        self.tabs.addTab(self.build_research_tab(), "资料检索")
        self.tabs.addTab(self.build_queue_tab(), "待执行")
        layout.addWidget(self.tabs)

    # -------------------------------------------------- 页签 1: AI 助手
    def build_assistant_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)

        # API 配置
        api_group = QGroupBox("AI配置")
        api_layout = QVBoxLayout()

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("提供方:"))
        self.provider_combo = QComboBox()
        for name in PROVIDER_ORDER:
            self.provider_combo.addItem(provider_label(name), name)
        self.provider_combo.currentIndexChanged.connect(self.on_provider_changed)
        provider_row.addWidget(self.provider_combo, 1)
        api_layout.addLayout(provider_row)

        api_layout.addWidget(QLabel("API Key:"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.editingFinished.connect(self.on_credentials_changed)
        api_layout.addWidget(self.api_key_input)

        # 接口地址（仅 OpenAI 兼容协议需要）
        self.base_row = QWidget()
        base_layout = QHBoxLayout(self.base_row)
        base_layout.setContentsMargins(0, 0, 0, 0)
        base_layout.addWidget(QLabel("接口地址:"))
        self.base_url_input = QLineEdit()
        self.base_url_input.editingFinished.connect(self.on_credentials_changed)
        base_layout.addWidget(self.base_url_input, 1)
        api_layout.addWidget(self.base_row)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("模型:"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.currentTextChanged.connect(self.on_credentials_changed)
        model_row.addWidget(self.model_combo, 1)
        api_layout.addLayout(model_row)

        # 测试连接：用当前填写的内容（不必先保存）发一次最小请求
        test_row = QHBoxLayout()
        self.btn_test = QPushButton("测试连接")
        self.btn_test.setToolTip(
            "用当前填写的内容（不必先保存）发一次最小请求，\n"
            "验证 API Key / 接口地址 / 模型是否可用")
        self.btn_test.clicked.connect(self.on_test_connection)
        test_row.addWidget(self.btn_test)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        self.test_result.setStyleSheet("color: #98a1b5; font-size: 11px;")
        test_row.addWidget(self.test_result, 1)
        api_layout.addLayout(test_row)
        self.connection_tested.connect(self._on_test_finished)

        self.provider_hint = QLabel("")
        self.provider_hint.setWordWrap(True)
        self.provider_hint.setStyleSheet("color: #98a1b5; font-size: 11px;")
        api_layout.addWidget(self.provider_hint)

        api_group.setLayout(api_layout)
        layout.addWidget(api_group)

        # 任务设置
        task_group = QGroupBox("任务设置")
        task_layout = QVBoxLayout()

        task_layout.addWidget(QLabel("任务描述:"))
        self.task_input = QTextEdit()
        self.task_input.setPlaceholderText("例如：检查设备版本和接口状态")
        self.task_input.setMaximumHeight(60)
        task_layout.addWidget(self.task_input)

        self.template_combo = QComboBox()
        self.template_combo.addItem("— 选择任务模板 —")
        self.template_combo.addItems(TASK_TEMPLATES)
        self.template_combo.currentTextChanged.connect(self.on_template_selected)
        task_layout.addWidget(self.template_combo)

        task_group.setLayout(task_layout)
        layout.addWidget(task_group)

        # 快捷命令
        quick_group = QGroupBox("快捷命令")
        quick_layout = QVBoxLayout()
        self.quick_list = QListWidget()
        self.quick_list.setMaximumHeight(96)
        self.quick_list.setToolTip("双击发送该命令")
        self.quick_list.itemDoubleClicked.connect(self.on_quick_command)
        quick_layout.addWidget(self.quick_list)
        quick_group.setLayout(quick_layout)
        layout.addWidget(quick_group)

        # 屏幕内容预览（AI 看到的内容）
        screen_group = QGroupBox("屏幕内容（AI 可见）")
        screen_layout = QVBoxLayout()
        self.screen_preview = QTextEdit()
        self.screen_preview.setReadOnly(True)
        self.screen_preview.setMaximumHeight(110)
        self.screen_preview.setPlaceholderText("会话建立后这里显示最近捕获到的设备输出")
        self.screen_preview.setStyleSheet(
            "background-color: #14161f; color: #98a1b5;"
            " font-family: Consolas, monospace; font-size: 11px;"
        )
        # 设备输出着色（错误/UP/DOWN/IP/MAC/接口名）
        OutputHighlighter(self.screen_preview.document())
        screen_layout.addWidget(self.screen_preview)
        screen_group.setLayout(screen_layout)
        layout.addWidget(screen_group)

        # AI 输出
        output_group = QGroupBox("AI输出")
        output_layout = QVBoxLayout()
        self.ai_output = QTextEdit()
        self.ai_output.setReadOnly(True)
        self.ai_output.setStyleSheet("""
            QTextEdit {
                background-color: #14161f;
                color: #e6e9f2;
            }
        """)
        # 输出着色：AI 生成的命令/设备回显里的关键信息标出来
        OutputHighlighter(self.ai_output.document())
        output_layout.addWidget(self.ai_output)
        output_group.setLayout(output_layout)
        layout.addWidget(output_group, 1)

        # 按钮
        btn_layout = QHBoxLayout()
        self.btn_analyze = QPushButton("分析当前输出")
        self.btn_analyze.clicked.connect(self.analyze_output)
        btn_layout.addWidget(self.btn_analyze)

        self.btn_execute = QPushButton("执行AI命令")
        self.btn_execute.clicked.connect(self.execute_ai_command)
        btn_layout.addWidget(self.btn_execute)
        layout.addLayout(btn_layout)

        btn_layout2 = QHBoxLayout()
        self.btn_explain = QPushButton("解释输出")
        self.btn_explain.clicked.connect(self.explain_requested.emit)
        btn_layout2.addWidget(self.btn_explain)

        self.btn_save_output = QPushButton("💾 存为资料")
        self.btn_save_output.setToolTip(
            "把 AI 输出（生成的命令、检索到的资料）沉淀到本地资料库，\n"
            "以后查命令时能直接检索到"
        )
        self.btn_save_output.clicked.connect(self.on_save_output)
        btn_layout2.addWidget(self.btn_save_output)

        self.btn_clear = QPushButton("清空")
        self.btn_clear.clicked.connect(self.clear_output)
        btn_layout2.addWidget(self.btn_clear)
        layout.addLayout(btn_layout2)

        self.chk_auto = QCheckBox("自动模式（AI自动执行命令）")
        self.chk_auto.stateChanged.connect(
            lambda _: self.auto_mode_changed.emit(self.chk_auto.isChecked())
        )
        layout.addWidget(self.chk_auto)

        return page

    # -------------------------------------------------- 页签 2: 资料检索
    def build_research_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)

        # 本地资料库
        kb_group = QGroupBox("本地资料库（AI 优先查这里）")
        kb_layout = QVBoxLayout()

        self.kb_status = QLabel("尚未导入资料")
        self.kb_status.setWordWrap(True)
        self.kb_status.setStyleSheet("color: #98a1b5;")
        kb_layout.addWidget(self.kb_status)

        kb_row = QHBoxLayout()
        self.btn_import_docs = QPushButton("📂  导入资料文件…")
        self.btn_import_docs.setToolTip(
            "选择下载好的命令手册 / 配置案例 / 培训文档，\n"
            "自动复制进资料库目录并建立索引"
        )
        self.btn_import_docs.clicked.connect(self.import_knowledge_requested.emit)
        kb_row.addWidget(self.btn_import_docs)

        self.btn_manage_docs = QPushButton("管理资料库…")
        self.btn_manage_docs.setToolTip("查看列表、检索测试、更换目录、重建索引")
        self.btn_manage_docs.clicked.connect(self.manage_knowledge_requested.emit)
        kb_row.addWidget(self.btn_manage_docs)
        kb_layout.addLayout(kb_row)

        kb_group.setLayout(kb_layout)
        layout.addWidget(kb_group)

        # 联网检索配置
        cfg_group = QGroupBox("联网检索设置")
        cfg_layout = QVBoxLayout()

        backend_row = QHBoxLayout()
        backend_row.addWidget(QLabel("后端:"))
        self.backend_combo = QComboBox()
        for label, value in SEARCH_BACKENDS:
            self.backend_combo.addItem(label, value)
        self.backend_combo.currentIndexChanged.connect(self.on_search_config_changed)
        backend_row.addWidget(self.backend_combo, 1)
        cfg_layout.addLayout(backend_row)

        self.searxng_input = QLineEdit()
        self.searxng_input.setPlaceholderText("SearXNG 实例地址，如 https://searx.example.com")
        self.searxng_input.editingFinished.connect(self.on_search_config_changed)
        cfg_layout.addWidget(self.searxng_input)

        num_row = QHBoxLayout()
        num_row.addWidget(QLabel("结果数:"))
        self.results_spin = QSpinBox()
        self.results_spin.setRange(1, 10)
        self.results_spin.valueChanged.connect(self.on_search_config_changed)
        num_row.addWidget(self.results_spin)
        num_row.addWidget(QLabel("读正文:"))
        self.read_pages_spin = QSpinBox()
        self.read_pages_spin.setRange(0, 4)
        self.read_pages_spin.valueChanged.connect(self.on_search_config_changed)
        num_row.addWidget(self.read_pages_spin)
        num_row.addWidget(QLabel("篇"))
        num_row.addStretch()
        cfg_layout.addLayout(num_row)

        self.chk_ai_search = QCheckBox("允许 AI 自主检索资料（分析时按需查询）")
        self.chk_ai_search.stateChanged.connect(self.on_search_config_changed)
        cfg_layout.addWidget(self.chk_ai_search)

        cfg_group.setLayout(cfg_layout)
        layout.addWidget(cfg_group)

        # 提问
        ask_group = QGroupBox("资料检索")
        ask_layout = QVBoxLayout()

        self.question_input = QTextEdit()
        self.question_input.setPlaceholderText(
            "例如：H3C S5560 如何配置端口镜像？\n"
            "或：Cisco console 输出乱码怎么排查？"
        )
        self.question_input.setMaximumHeight(60)
        ask_layout.addWidget(self.question_input)

        row = QHBoxLayout()
        self.btn_research = QPushButton("查资料")
        self.btn_research.setToolTip("先查本地资料库，再联网检索")
        self.btn_research.setDefault(False)
        self.btn_research.clicked.connect(self.on_research)
        row.addWidget(self.btn_research)

        self.btn_research_with_screen = QPushButton("带上屏幕内容查询")
        self.btn_research_with_screen.setToolTip("把当前设备输出作为背景信息一起提交")
        self.btn_research_with_screen.clicked.connect(
            lambda: self.on_research(include_screen=True)
        )
        row.addWidget(self.btn_research_with_screen)

        self.btn_save_research = QPushButton("💾 存为资料")
        self.btn_save_research.setToolTip("把这次查到的命令沉淀到本地资料库")
        self.btn_save_research.clicked.connect(self.on_save_research)
        row.addWidget(self.btn_save_research)
        ask_layout.addLayout(row)

        ask_group.setLayout(ask_layout)
        layout.addWidget(ask_group)

        # 回答
        answer_group = QGroupBox("AI 回答")
        answer_layout = QVBoxLayout()
        self.research_output = QTextEdit()
        self.research_output.setReadOnly(True)
        self.research_output.setStyleSheet(
            "background-color: #14161f; color: #e6e9f2;"
        )
        OutputHighlighter(self.research_output.document())
        answer_layout.addWidget(self.research_output)
        answer_group.setLayout(answer_layout)
        layout.addWidget(answer_group, 1)

        # 来源
        src_group = QGroupBox("参考来源（本地资料 / 网页，双击打开）")
        src_layout = QVBoxLayout()
        self.source_list = QListWidget()
        self.source_list.setMaximumHeight(130)
        self.source_list.itemDoubleClicked.connect(self.open_source)
        src_layout.addWidget(self.source_list)
        src_group.setLayout(src_layout)
        layout.addWidget(src_group)

        return page

    # -------------------------------------------------- 页签 3: 待执行
    def build_queue_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)

        tip = QLabel("AI 生成的命令会进入这里。勾选后点“执行勾选项”批量下发，双击可编辑。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #98a1b5;")
        layout.addWidget(tip)

        self.queue_list = QListWidget()
        self.queue_list.setToolTip("双击编辑命令，勾选后执行")
        self.queue_list.itemChanged.connect(self.on_queue_item_changed)
        layout.addWidget(self.queue_list, 1)

        row1 = QHBoxLayout()
        btn_run_checked = QPushButton("执行勾选项")
        btn_run_checked.clicked.connect(self.run_checked_commands)
        row1.addWidget(btn_run_checked)

        btn_run_all = QPushButton("执行全部")
        btn_run_all.clicked.connect(self.run_all_commands)
        row1.addWidget(btn_run_all)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        btn_check_all = QPushButton("全选")
        btn_check_all.clicked.connect(lambda: self.set_all_checked(True))
        row2.addWidget(btn_check_all)

        btn_uncheck_all = QPushButton("全不选")
        btn_uncheck_all.clicked.connect(lambda: self.set_all_checked(False))
        row2.addWidget(btn_uncheck_all)

        btn_remove = QPushButton("移除勾选")
        btn_remove.clicked.connect(self.remove_checked)
        row2.addWidget(btn_remove)

        btn_save_queue = QPushButton("💾 存为资料")
        btn_save_queue.setToolTip("把队列里的命令沉淀成一份命令速查资料")
        btn_save_queue.clicked.connect(self.on_save_queue)
        row2.addWidget(btn_save_queue)

        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self.clear_queue)
        row2.addWidget(btn_clear)
        layout.addLayout(row2)

        self.queue_status = QLabel("队列为空")
        self.queue_status.setStyleSheet("color: #98a1b5;")
        layout.addWidget(self.queue_status)

        return page

    # ------------------------------------------------------------ 配置
    def load_settings(self):
        """从配置载入 AI 与检索设置"""
        provider = self.settings.ai_provider
        index = self.provider_combo.findData(provider)
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.provider_combo.blockSignals(False)
        self.apply_provider_fields(provider)

        self.chk_auto.setChecked(bool(self.settings.get("auto_mode_default", False)))

        # 检索设置
        self.set_combo_by_data(self.backend_combo, self.settings.search_backend)
        self.searxng_input.setText(self.settings.searxng_url)
        self.results_spin.setValue(self.settings.search_max_results)
        self.read_pages_spin.setValue(self.settings.search_read_pages)
        self.chk_ai_search.setChecked(self.settings.ai_auto_search)

    def apply_provider_fields(self, provider: str):
        """把某提供方的密钥 / 模型 / 接口地址填到界面上"""
        provider = (provider or "anthropic").lower()
        defaults = provider_defaults(provider)
        config = self.settings.get_provider_config(provider)

        self.api_key_input.setText(self.settings.get_api_key_for(provider))
        self.api_key_input.setPlaceholderText(defaults.get("key_hint", ""))

        self.base_url_input.setText(config["base_url"])
        self.base_row.setVisible(provider != "anthropic")

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        models = list(defaults.get("models") or [])
        if config["model"] and config["model"] not in models:
            models.insert(0, config["model"])
        self.model_combo.addItems(models)
        self.model_combo.setCurrentText(config["model"])
        self.model_combo.blockSignals(False)

        hints = {
            "anthropic": "Anthropic 官方接口，需要 sk-ant- 开头的密钥。",
            "deepseek": "DeepSeek 官方接口（OpenAI 兼容）。密钥在 platform.deepseek.com 申请；"
                        "deepseek-chat 通用，deepseek-reasoner 推理更强但更慢。",
            "openai_compat": "任何 OpenAI 兼容服务：vLLM / Ollama / 中转站等，"
                             "填服务商的接口地址（一般以 /v1 结尾）。",
        }
        self.provider_hint.setText(hints.get(provider, ""))

    def on_provider_changed(self):
        """切换提供方：先保存上一家的配置，再载入新一家的"""
        previous = self.settings.ai_provider
        self.settings.set_provider_config(
            previous, model=self.model_combo.currentText().strip(),
            base_url=self.base_url_input.text().strip())
        self.settings.set_api_key_for(previous, self.api_key_input.text().strip())

        provider = self.provider_combo.currentData()
        self.settings.ai_provider = provider
        self.apply_provider_fields(provider)
        self.settings.save()
        self.on_credentials_changed()

    @staticmethod
    def set_combo_by_data(combo: QComboBox, value):
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    def get_provider(self) -> str:
        return self.provider_combo.currentData() or "anthropic"

    def get_api_key(self) -> str:
        """获取API Key"""
        return self.api_key_input.text().strip()

    def get_base_url(self) -> str:
        return self.base_url_input.text().strip()

    def get_model(self) -> str:
        """获取模型名（留空则用提供方默认）"""
        return (self.model_combo.currentText().strip()
                or provider_defaults(self.get_provider())["model"])

    def get_task(self) -> str:
        """获取任务描述"""
        return self.task_input.toPlainText().strip()

    def get_auto_mode(self) -> bool:
        return self.chk_auto.isChecked()

    def set_quick_commands(self, vendor: str):
        """按设备厂商刷新快捷命令列表"""
        self.quick_list.clear()
        for label, cmd in get_vendor_commands(vendor).items():
            item = QListWidgetItem(f"{label}  |  {cmd}")
            item.setData(Qt.ItemDataRole.UserRole, cmd)
            item.setToolTip("双击发送该命令")
            self.quick_list.addItem(item)

    # ------------------------------------------------------------ AI 助手事件
    def on_credentials_changed(self):
        """保存凭据并通知主窗口"""
        provider = self.get_provider()
        key = self.get_api_key()
        model = self.get_model()
        base_url = self.get_base_url()

        self.settings.ai_provider = provider
        self.settings.set_api_key_for(provider, key)
        self.settings.set_provider_config(provider, model=model, base_url=base_url)
        self.settings.save()
        self.credentials_changed.emit({
            "provider": provider, "api_key": key,
            "model": model, "base_url": base_url,
        })

    # ------------------------------------------------------------ 测试连接
    def on_test_connection(self):
        """
        用**当前输入框里的值**（不必先保存）发一次最小请求验证连通性。

        注意：故意不用已保存的值 —— 用户改了 Key 还没失焦保存就点测试，
        测旧值等于白测。也不阻断保存：网络临时不通时照样能存 Key。
        """
        provider = self.get_provider()
        api_key = self.get_api_key()
        model = self.get_model()
        base_url = self.get_base_url()

        if not api_key:
            self._set_test_result("请先填写 API Key", ok=False)
            return
        if provider != "anthropic" and not base_url:
            self._set_test_result("请先填写接口地址", ok=False)
            return

        self.btn_test.setEnabled(False)
        self._set_test_result("测试中…", ok=None)
        threading.Thread(
            target=self._test_worker,
            args=(provider, api_key, model, base_url),
            daemon=True,
        ).start()

    def _test_worker(self, provider: str, api_key: str, model: str, base_url: str):
        """后台线程里跑网络请求，结果通过信号回主线程（不能直接碰控件）"""
        try:
            client = LLMClient(provider=provider, api_key=api_key,
                               model=model, base_url=base_url)
            client.chat(
                "You are a connectivity probe. Reply with exactly: ok",
                [{"role": "user", "content": "ping"}],
                max_tokens=8,      # 最小请求，成本可忽略
            )
            self.connection_tested.emit(True, f"已连通 · {model}")
        except LLMError as e:
            # LLMError 的文案已经是翻译过的人话（401/网络不可达等）
            self.connection_tested.emit(False, str(e))
        except Exception as e:
            self.connection_tested.emit(False, f"{type(e).__name__}: {e}")

    def _on_test_finished(self, ok: bool, message: str):
        self.btn_test.setEnabled(True)
        self._set_test_result(message, ok=ok)

    def _set_test_result(self, text: str, ok):
        """ok: True 成功 / False 失败 / None 进行中"""
        if ok is True:
            color, prefix = "#3fb950", "✓ "
        elif ok is False:
            color, prefix = "#f85149", "✗ "
        else:
            color, prefix = "#98a1b5", ""
        self.test_result.setText(prefix + text)
        self.test_result.setStyleSheet(f"color: {color}; font-size: 11px;")
        self.test_result.setToolTip(text)

    def on_template_selected(self, text: str):
        if text and not text.startswith("—"):
            self.task_input.setPlainText(text)

    def on_quick_command(self, item: QListWidgetItem):
        cmd = item.data(Qt.ItemDataRole.UserRole)
        if cmd:
            self.append_output(f"[快捷命令] {cmd}")
            self.analyze_requested.emit(cmd)

    def append_output(self, text: str):
        """添加AI输出"""
        self.ai_output.append(text)
        self.ai_output.ensureCursorVisible()

    def clear_output(self):
        """清空输出"""
        self.ai_output.clear()

    def analyze_output(self):
        """请求分析当前输出"""
        self.analyze_requested.emit(self.get_task())

    def execute_ai_command(self):
        """请求执行最近一条 AI 命令"""
        self.execute_requested.emit()

    def set_last_command(self, command: str):
        """记录最近一次 AI 生成的命令"""
        self.last_command = command
        self.btn_execute.setToolTip(f"执行: {command}")

    def update_screen_preview(self, text: str):
        """更新"AI 可见的屏幕内容"预览"""
        if not self.screen_preview.hasFocus():
            self.screen_preview.setPlainText((text or "")[-2000:])
            self.screen_preview.verticalScrollBar().setValue(
                self.screen_preview.verticalScrollBar().maximum()
            )

    # ------------------------------------------------------------ 联网查询
    def on_search_config_changed(self):
        """保存检索配置"""
        self.settings.set("search_backend", self.backend_combo.currentData())
        self.settings.set("searxng_url", self.searxng_input.text().strip())
        self.settings.set("search_max_results", self.results_spin.value())
        self.settings.set("search_read_pages", self.read_pages_spin.value())
        self.settings.set("ai_auto_search", self.chk_ai_search.isChecked())
        self.settings.save()
        self.search_config_changed.emit()

    def on_research(self, include_screen: bool = False):
        """发起联网查询"""
        question = self.question_input.toPlainText().strip()
        if not question:
            QMessageBox.information(self, "请输入问题", "请先填写要查询的内容。")
            return
        self.research_output.setPlainText("[系统] 正在联网检索…")
        self.source_list.clear()
        self.research_requested.emit(question if not include_screen
                                     else f"{question}\n（请结合设备当前输出作答）")

    def set_knowledge_status(self, text: str):
        """显示本地资料库状态（文档数等）"""
        self.kb_status.setText(text or "尚未导入资料")

    def show_research_result(self, answer: str, sources: list):
        """显示资料检索结果"""
        self.research_output.setPlainText(answer or "（无回答）")
        self.source_list.clear()
        for index, item in enumerate(sources or [], start=1):
            is_local = item.get("source_type") == "local"
            title = item.get("title") or item.get("url", "")
            prefix = "【本地】" if is_local else "【网页】"
            entry = QListWidgetItem(f"[{index}] {prefix}{title}")
            # 本地资料用文件路径，网页用 URL
            target = item.get("path") or item.get("url", "")
            entry.setData(Qt.ItemDataRole.UserRole, target)
            entry.setToolTip(target)
            self.source_list.addItem(entry)

    def show_research_error(self, message: str):
        self.research_output.setPlainText(f"[错误] {message}")

    def open_source(self, item: QListWidgetItem):
        """打开参考来源（本地文件或网页）"""
        target = item.data(Qt.ItemDataRole.UserRole)
        if not target:
            return
        if os.path.exists(target):
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))
        else:
            QDesktopServices.openUrl(QUrl(target))

    # ------------------------------------------------------------ 存为资料
    def source_entries(self):
        """把参考来源列表读成 [{"label", "target"}…]"""
        entries = []
        for index in range(self.source_list.count()):
            item = self.source_list.item(index)
            entries.append({
                "label": item.text(),
                "target": item.data(Qt.ItemDataRole.UserRole) or "",
            })
        return entries

    def on_save_research(self):
        """把资料检索的回答沉淀到本地资料库"""
        answer = self.research_output.toPlainText().strip()
        if not answer or answer.startswith("[系统]") or answer.startswith("[错误]"):
            QMessageBox.information(self, "没有内容", "请先执行一次资料检索，再保存。")
            return

        question_lines = self.question_input.toPlainText().strip().splitlines()
        title = (question_lines[0][:60] if question_lines else "") or "资料检索结果"
        content = answer

        entries = self.source_entries()
        if entries:
            content += "\n\n## 参考来源\n" + "\n".join(
                f"- {entry['label']}" + (f"（{entry['target']}）" if entry["target"] else "")
                for entry in entries
            )
        self.save_note_requested.emit(title, content, "命令速查")

    def on_save_output(self):
        """把 AI 输出（生成的命令 / 检索过程）沉淀到本地资料库"""
        text = self.ai_output.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "没有内容", "AI 输出为空，先让 AI 分析一次。")
            return
        title = self.get_task() or "AI 建议命令"
        self.save_note_requested.emit(title[:60], text[-4000:], "AI笔记")

    def on_save_queue(self):
        """把待执行队列里的命令沉淀成一份命令速查"""
        commands = self.all_commands()
        if not commands:
            QMessageBox.information(self, "队列为空", "待执行队列里没有命令。")
            return
        title = "命令清单 " + time.strftime("%m-%d %H:%M")
        content = "```\n" + "\n".join(commands) + "\n```"
        self.save_note_requested.emit(title, content, "命令速查")

    # ------------------------------------------------------------ 待执行队列
    def add_commands(self, commands, checked: bool = True):
        """把命令加入待执行队列（自动去重）"""
        existing = set()
        for index in range(self.queue_list.count()):
            existing.add(self.queue_list.item(index).text().strip())

        added = 0
        for command in commands or []:
            command = (command or "").strip()
            if not command or command in existing:
                continue
            item = QListWidgetItem(command)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable |
                          Qt.ItemFlag.ItemIsEditable)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.queue_list.addItem(item)
            existing.add(command)
            added += 1

        self.update_queue_status()
        return added

    def on_queue_item_changed(self, _item):
        self.update_queue_status()

    def update_queue_status(self):
        total = self.queue_list.count()
        checked = len(self.checked_commands())
        self.queue_status.setText(
            f"共 {total} 条，勾选 {checked} 条" if total else "队列为空"
        )

    def checked_commands(self):
        """取勾选的命令"""
        result = []
        for index in range(self.queue_list.count()):
            item = self.queue_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.text().strip())
        return result

    def all_commands(self):
        return [self.queue_list.item(i).text().strip()
                for i in range(self.queue_list.count())]

    def set_all_checked(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for index in range(self.queue_list.count()):
            self.queue_list.item(index).setCheckState(state)
        self.update_queue_status()

    def remove_checked(self):
        for index in range(self.queue_list.count() - 1, -1, -1):
            item = self.queue_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                self.queue_list.takeItem(index)
        self.update_queue_status()

    def clear_queue(self):
        self.queue_list.clear()
        self.update_queue_status()

    def run_checked_commands(self):
        commands = self.checked_commands()
        if not commands:
            QMessageBox.information(self, "没有勾选", "请先勾选要执行的命令。")
            return
        self.queue_execute_requested.emit(commands)

    def run_all_commands(self):
        commands = self.all_commands()
        if not commands:
            QMessageBox.information(self, "队列为空", "待执行队列里没有命令。")
            return
        self.queue_execute_requested.emit(commands)
