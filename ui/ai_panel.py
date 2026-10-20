"""
AI 高级面板 —— AI 设置入口 + 资料检索

两个页签:
    1. AI 助手   —— 只放 AI 配置的状态提示与「AI 设置…」入口
    2. 资料检索  —— 本地资料库 / 联网检索配置、提问与结果

会话相关的 AI 交互（提问、生成命令、解释输出）都在「Catty 对话」页签里完成。
"""
import os
import threading

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
    QLineEdit, QPushButton, QLabel, QComboBox, QSpinBox,
    QGroupBox, QCheckBox, QListWidget, QListWidgetItem,
    QTabWidget, QMessageBox, QScrollArea, QFrame
)
from PyQt6.QtCore import Qt, pyqtSignal, QUrl
from PyQt6.QtGui import QDesktopServices

from core import local_model
from core.llm import provider_defaults, provider_label
from config.settings import Settings
from ui.ai_settings_dialog import AISettingsDialog
from ui.output_highlighter import OutputHighlighter

SEARCH_BACKENDS = [
    ("自动（推荐）", "auto"),
    ("百度", "baidu"),
    ("必应", "bing"),
    ("DuckDuckGo", "duckduckgo"),
    ("SearXNG（自建实例）", "searxng"),
    ("关闭联网检索", "off"),
]


class AIPanel(QWidget):
    """
    AI 高级面板

    信号:
        credentials_changed(dict):     AI 凭据变化 {provider, api_key, model, base_url}
        connection_tested(bool, str):  测试连接结果（由 AI 设置对话框转发）
        research_requested(str):       请求联网查询（参数为问题）
        search_config_changed():       检索配置变化
        import_knowledge_requested():  点"导入资料文件"
        manage_knowledge_requested():  点"管理资料库"
        save_note_requested(str, str, str): 存为资料: 标题, 内容, 分类
        local_status_ready(dict):      本地模型档位探测结果（后台线程 → 主线程）
    """

    credentials_changed = pyqtSignal(dict)   # {provider, api_key, model, base_url}
    connection_tested = pyqtSignal(bool, str)
    research_requested = pyqtSignal(str)
    search_config_changed = pyqtSignal()
    import_knowledge_requested = pyqtSignal()   # 点"导入资料文件"
    manage_knowledge_requested = pyqtSignal()   # 点"管理资料库"
    save_note_requested = pyqtSignal(str, str, str)  # 存为资料: 标题, 内容, 分类
    local_status_ready = pyqtSignal(dict)    # core.local_model.decide() 的返回值

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = Settings()
        # 先接信号再建界面：启动时 load_settings() 会立刻发起一次档位探测，
        # 结果必须能回到主线程（Qt 信号跨线程是队列投递，安全）。
        self.local_status_ready.connect(self._on_local_status_ready)
        self.init_ui()
        self.load_settings()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.tabs = QTabWidget()
        # 每个页签都套一层滚动容器：页签内容较多时若直接放，面板的最小高度
        # 会把主窗口顶得比屏幕还高（底部对话输入框、状态栏会被裁掉）。
        # 套上滚动后最小高度很小，窗口可自由缩小，内容靠滚动查看。
        self.tabs.addTab(self._wrap_scroll(self.build_assistant_tab()), "AI 助手")
        self.tabs.addTab(self._wrap_scroll(self.build_research_tab()), "资料检索")
        layout.addWidget(self.tabs)

    @staticmethod
    def _wrap_scroll(inner: QWidget) -> QScrollArea:
        """把页签内容包进滚动区，避免撑大整个窗口的最小高度"""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setStyleSheet("QScrollArea { background: transparent; }"
                           "QScrollArea > QWidget > QWidget { background: transparent; }")
        area.setWidget(inner)
        area.setMinimumHeight(140)
        return area

    # -------------------------------------------------- 页签 1: AI 助手
    def build_assistant_tab(self):
        """只留 AI 配置的状态提示与设置入口"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)

        settings_row = QHBoxLayout()
        self.ai_config_label = QLabel("")
        self.ai_config_label.setToolTip("AI 的提供方 / 模型，点右侧按钮修改")
        self.ai_config_label.setStyleSheet("color: #93A0B4; font-size: 11px;")
        settings_row.addWidget(self.ai_config_label, 1)

        self.btn_ai_settings = QPushButton("AI 设置…")
        self.btn_ai_settings.setToolTip(
            "配置 AI 提供方 / API Key / 接口地址 / 模型，并可测试连接")
        self.btn_ai_settings.clicked.connect(self.open_settings_dialog)
        settings_row.addWidget(self.btn_ai_settings)
        layout.addLayout(settings_row)

        # 本机档位 / 本地 LLM 可用性（P2）。**只标注，不自动启动模型**：
        # 启动时异步探测一次填进来，用户要真起服务就自己去「AI 设置…」点按钮。
        # 内存/显存不够时这里会如实显示"仅本地检索"——降级不等于降质。
        self.local_model_label = QLabel("")
        self.local_model_label.setToolTip("本机档位与本地 LLM 可用性（只读提示）")
        self.local_model_label.setStyleSheet("color: #93A0B4; font-size: 11px;")
        self.local_model_label.setWordWrap(True)
        layout.addWidget(self.local_model_label)
        layout.addStretch(1)

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
        self.kb_status.setStyleSheet("color: #93A0B4;")
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
            "background-color: #0E1117; color: #E6EAF2;"
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

    # ------------------------------------------------------------ 配置
    def load_settings(self):
        """从配置载入 AI 与检索设置"""
        self.update_ai_config_label()
        # 顺带把本机档位/本地 LLM 可用性标注一次（异步，不阻塞界面）。
        # 面板在启动时就被主窗口建出来（main_window.py:191），所以这里就是
        # "应用启动时判档"的落点；**只标注，不自动拉起模型**。
        self.refresh_local_status()

        # 检索设置
        self.set_combo_by_data(self.backend_combo, self.settings.search_backend)
        self.searxng_input.setText(self.settings.searxng_url)
        self.results_spin.setValue(self.settings.search_max_results)
        self.read_pages_spin.setValue(self.settings.search_read_pages)
        self.chk_ai_search.setChecked(self.settings.ai_auto_search)

    def update_ai_config_label(self):
        """刷新"当前：提供方 / 模型"提示"""
        self.ai_config_label.setText(
            f"当前：{provider_label(self.get_provider())} / {self.get_model()}")

    # ------------------------------------------------------ 本地模型标注（P2）
    def refresh_local_status(self):
        """探测本机档位并把结论写进 `local_model_label`（**不自动启动模型**）。

        ⚠ 不能在 UI 线程里直接调 `local_model.decide()`：它内部要起
        `llama-server --list-devices` 子进程（秒级），同步等就是卡启动。
        所以立刻显示一句便宜的"探测中…"（`server_status()` 只读 pid 文件），
        完整判定丢后台线程，结果经 `local_status_ready` 信号回主线程。
        """
        try:
            st = local_model.server_status()
        except Exception:                       # noqa: BLE001
            st = {}
        if st.get("running"):
            note = f"本地服务在跑：{st.get('model') or '未知模型'}"
        else:
            note = "本地服务未启动"
        self.local_model_label.setText(f"本地模型：探测中…（{note}）")
        self.local_model_label.setToolTip("正在判定本机档位（不影响本地资料检索）")
        threading.Thread(target=self._local_probe_worker, daemon=True).start()

    def _local_probe_worker(self):
        """后台线程：拿判定结果，回主线程。异常也要给结论，不能留"探测中…"。"""
        try:
            self.local_status_ready.emit(local_model.decide())
        except Exception as e:                  # noqa: BLE001
            self.local_status_ready.emit({
                "mode": local_model.MODE_RETRIEVAL,
                "label": f"本地模型探测失败（{type(e).__name__}）", "detail": str(e),
                "base_url": "", "tier": "", "model": "", "device": "",
                "reason": str(e)})

    def _on_local_status_ready(self, decision):
        """写标注。文案与「AI 设置…」对话框共用 `local_model.describe()`，两处不许各写一套。"""
        text = local_model.describe(decision)
        if decision.get("mode") == local_model.MODE_STARTABLE:
            # 只提示去哪起，不代替用户起 —— 自动拉起会凭空吃 2-5GB 内存/显存
            text += "｜可在「AI 设置…」里一键启动"
        self.local_model_label.setText("本地模型：" + text)
        self.local_model_label.setToolTip(local_model.describe(decision))

    def open_settings_dialog(self):
        """打开 AI 设置对话框"""
        dialog = AISettingsDialog(self)
        dialog.saved.connect(self.on_ai_settings_saved)
        dialog.connection_tested.connect(self.connection_tested.emit)
        dialog.exec()

    def on_ai_settings_saved(self):
        """对话框保存后刷新界面，并通知主窗口新凭据生效"""
        self.load_settings()
        self.on_credentials_changed()

    @staticmethod
    def set_combo_by_data(combo: QComboBox, value):
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    def get_provider(self) -> str:
        return self.settings.ai_provider

    def get_api_key(self) -> str:
        """获取API Key（当前提供方的）"""
        return (self.settings.api_key or "").strip()

    def get_base_url(self) -> str:
        return self.settings.get_provider_config(self.get_provider())["base_url"]

    def get_model(self) -> str:
        """获取模型名（留空则用提供方默认）"""
        return (self.settings.get_provider_config(self.get_provider())["model"]
                or provider_defaults(self.get_provider())["model"])

    def on_credentials_changed(self):
        """保存凭据并通知主窗口（AI 设置对话框保存后也走这里）"""
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
