"""
本地资料库管理 - 导入下载的文档、浏览与筛选笔记、批量导出成一份手册

用户操作路径:
    点"导入文件…"选好文档 → 自动复制进资料库目录 → 自动抽取正文建索引
    → AI 查命令时优先在本地资料库里找
"""
import os
import threading
import time

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog,
    QMessageBox, QGroupBox, QLineEdit, QTextEdit, QCheckBox,
    QAbstractItemView, QInputDialog, QProgressBar, QApplication,
    QComboBox, QTabWidget, QWidget
)
from PyQt6.QtCore import Qt, pyqtSignal, QUrl
from PyQt6.QtGui import QDesktopServices, QColor

from core.knowledge import KnowledgeBase, SUPPORTED_EXTENSIONS
from core.manual import (
    load_notes, filter_notes, collect_tags, collect_categories,
    export_manual, export_notes_csv
)
from ui.note_dialog import NoteDialog
from config.settings import Settings

ALL_CATEGORIES = "全部分类"
ALL_TAGS = "全部标签"
RAW_DOC_CATEGORY = "（原始文档）"


def build_file_filter() -> str:
    """构造文件选择过滤器（导入对话框与主窗口共用）"""
    patterns = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_EXTENSIONS))
    return (f"可解析的文档 ({patterns});;"
            "命令手册/文档 (*.pdf *.docx *.txt *.md);;"
            "表格 (*.xlsx *.csv *.tsv);;"
            "网页/压缩包 (*.html *.htm *.zip);;"
            "所有文件 (*)")


class KnowledgeDialog(QDialog):
    """
    本地资料库管理

    信号:
        knowledge_changed(): 资料库内容变化（导入/删除/换目录/重建索引/新建笔记）
    """

    knowledge_changed = pyqtSignal()
    index_finished = pyqtSignal(dict)

    def __init__(self, parent=None, knowledge_base: KnowledgeBase = None):
        super().__init__(parent)
        self.kb = knowledge_base or KnowledgeBase()
        self.settings = Settings()
        self._note_cache = {}   # (path, mtime) -> 解析出的笔记信息
        self.setWindowTitle("本地资料库 - 让 AI 查自己下载的命令文档")
        self.resize(1120, 780)
        self.index_finished.connect(self.on_index_finished)
        self.init_ui()
        self.refresh_filter_options()
        self.refresh_table()
        self.refresh_stats()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_document_tab(), "文档与笔记")
        self.tabs.addTab(self.build_search_tab(), "检索测试")
        layout.addWidget(self.tabs, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    # -------------------------------------------------- 页签 1: 文档与笔记
    def build_document_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)

        # 目录信息
        path_group = QGroupBox("资料库目录")
        path_layout = QHBoxLayout()
        self.path_label = QLabel(self.kb.root)
        self.path_label.setStyleSheet("color: #a5b1ff;")
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        path_layout.addWidget(self.path_label, 1)

        btn_open_dir = QPushButton("打开目录")
        btn_open_dir.setToolTip("在文件管理器里打开资料库目录")
        btn_open_dir.clicked.connect(self.open_directory)
        path_layout.addWidget(btn_open_dir)

        btn_change_dir = QPushButton("更换目录…")
        btn_change_dir.clicked.connect(self.change_directory)
        path_layout.addWidget(btn_change_dir)

        path_group.setLayout(path_layout)
        layout.addWidget(path_group)

        # 导入区
        import_group = QGroupBox("导入资料（下载好的手册、案例、对照表都可以）")
        import_layout = QVBoxLayout()

        row1 = QHBoxLayout()
        self.btn_import_files = QPushButton("📂  导入文件…")
        self.btn_import_files.setMinimumHeight(34)
        self.btn_import_files.setToolTip("选择文件后自动复制进资料库并建立索引")
        self.btn_import_files.clicked.connect(self.import_files)
        row1.addWidget(self.btn_import_files)

        self.btn_import_dir = QPushButton("导入文件夹…")
        self.btn_import_dir.setMinimumHeight(34)
        self.btn_import_dir.setToolTip("整个文件夹（含子目录）一起导入")
        self.btn_import_dir.clicked.connect(self.import_directory)
        row1.addWidget(self.btn_import_dir)

        self.btn_import_text = QPushButton("导入剪贴板文本…")
        self.btn_import_text.setMinimumHeight(34)
        self.btn_import_text.setToolTip("把网页上复制的命令说明存成文档")
        self.btn_import_text.clicked.connect(self.import_from_clipboard)
        row1.addWidget(self.btn_import_text)

        self.btn_new_note = QPushButton("新建笔记…")
        self.btn_new_note.setMinimumHeight(34)
        self.btn_new_note.setToolTip("手写一条命令笔记，直接进知识库")
        self.btn_new_note.clicked.connect(self.create_note)
        row1.addWidget(self.btn_new_note)
        import_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.chk_move = QCheckBox("导入后删除源文件（移动而非复制）")
        self.chk_move.setToolTip("默认是复制，源文件保留在原处")
        row2.addWidget(self.chk_move)

        row2.addWidget(QLabel("归入子目录:"))
        self.subdir_input = QLineEdit()
        self.subdir_input.setPlaceholderText("可留空，例如：H3C 或 华为S5700")
        self.subdir_input.setMaximumWidth(200)
        row2.addWidget(self.subdir_input)
        row2.addStretch()
        import_layout.addLayout(row2)

        tip = QLabel(
            "支持: PDF / Word(docx) / Excel(xlsx) / PPT(pptx) / txt / md / html / csv / 日志 / 配置 / zip。"
            "老版 .doc/.xls/.ppt 请先另存为新格式或导出 PDF。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #98a1b5; font-size: 11px;")
        import_layout.addWidget(tip)

        import_group.setLayout(import_layout)
        layout.addWidget(import_group)

        # 筛选
        filter_group = QGroupBox("筛选")
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("分类:"))
        self.category_combo = QComboBox()
        self.category_combo.setMinimumWidth(140)
        self.category_combo.currentIndexChanged.connect(self.refresh_table)
        filter_layout.addWidget(self.category_combo)

        filter_layout.addWidget(QLabel("标签:"))
        self.tag_combo = QComboBox()
        self.tag_combo.setMinimumWidth(140)
        self.tag_combo.currentIndexChanged.connect(self.refresh_table)
        filter_layout.addWidget(self.tag_combo)

        filter_layout.addWidget(QLabel("关键词:"))
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("按文件名 / 标题 / 标签筛选")
        self.filter_input.textChanged.connect(self.refresh_table)
        filter_layout.addWidget(self.filter_input, 1)

        btn_reset = QPushButton("重置筛选")
        btn_reset.clicked.connect(self.reset_filter)
        filter_layout.addWidget(btn_reset)
        filter_group.setLayout(filter_layout)
        layout.addWidget(filter_group)

        # 列表
        list_group = QGroupBox("资料列表（勾选后可批量导出成手册）")
        list_layout = QVBoxLayout()

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["选择", "名称", "分类", "标签", "类型", "大小", "修改时间", "状态"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 4, 5, 6, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(self.open_selected_file)
        list_layout.addWidget(self.table)

        row3 = QHBoxLayout()
        btn_check_all = QPushButton("全选")
        btn_check_all.clicked.connect(lambda: self.set_all_checked(True))
        row3.addWidget(btn_check_all)
        btn_check_none = QPushButton("全不选")
        btn_check_none.clicked.connect(lambda: self.set_all_checked(False))
        row3.addWidget(btn_check_none)

        self.btn_export_manual = QPushButton("📘 导出为手册…")
        self.btn_export_manual.setToolTip("把勾选的笔记合并成一份手册（Markdown 或 HTML）")
        self.btn_export_manual.clicked.connect(self.export_selected_manual)
        row3.addWidget(self.btn_export_manual)

        self.btn_export_csv = QPushButton("导出清单 CSV")
        self.btn_export_csv.setToolTip("导出笔记清单（分类/标题/标签/设备），便于用 Excel 查看")
        self.btn_export_csv.clicked.connect(self.export_note_list)
        row3.addWidget(self.btn_export_csv)
        row3.addStretch()
        list_layout.addLayout(row3)

        row4 = QHBoxLayout()
        self.btn_delete = QPushButton("删除勾选")
        self.btn_delete.clicked.connect(self.delete_selected)
        row4.addWidget(self.btn_delete)

        self.btn_open_file = QPushButton("打开选中文件")
        self.btn_open_file.clicked.connect(self.open_selected_file)
        row4.addWidget(self.btn_open_file)

        self.btn_reindex = QPushButton("重建索引")
        self.btn_reindex.setToolTip("文档内容变了或抽取失败时点这里")
        self.btn_reindex.clicked.connect(lambda: self.rebuild_index(force=True))
        row4.addWidget(self.btn_reindex)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh_all)
        row4.addWidget(self.btn_refresh)
        row4.addStretch()

        self.chk_enabled = QCheckBox("查命令时检索本地资料库")
        self.chk_enabled.setChecked(self.settings.knowledge_enabled)
        self.chk_enabled.stateChanged.connect(self.on_enabled_changed)
        row4.addWidget(self.chk_enabled)
        list_layout.addLayout(row4)

        self.stats_label = QLabel("-")
        self.stats_label.setStyleSheet("color: #98a1b5;")
        list_layout.addWidget(self.stats_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        list_layout.addWidget(self.progress)

        list_group.setLayout(list_layout)
        layout.addWidget(list_group, 1)
        return page

    # -------------------------------------------------- 页签 2: 检索测试
    def build_search_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)

        hint = QLabel("输入关键词，验证 AI 能不能从本地资料里查到。"
                      "查得到说明导入生效，AI 之后也会用上这些内容。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #98a1b5;")
        layout.addWidget(hint)

        search_row = QHBoxLayout()
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("例如：端口镜像 或 display cpu-usage")
        self.query_input.returnPressed.connect(self.test_search)
        search_row.addWidget(self.query_input, 1)

        btn_search = QPushButton("检索")
        btn_search.clicked.connect(self.test_search)
        search_row.addWidget(btn_search)
        layout.addLayout(search_row)

        self.result_view = QTextEdit()
        self.result_view.setReadOnly(True)
        self.result_view.setPlaceholderText("检索结果会显示命中的片段和来源文件")
        self.result_view.setStyleSheet("background-color: #171a24; color: #e6e9f2;")
        layout.addWidget(self.result_view, 1)
        return page

    # ------------------------------------------------------------ 导入
    def import_files(self):
        """导入文件（核心按钮）"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要导入的命令手册 / 文档", "", build_file_filter()
        )
        if not paths:
            return
        self.do_import(paths)

    def import_directory(self):
        """导入整个文件夹"""
        path = QFileDialog.getExistingDirectory(self, "选择要导入的文件夹")
        if not path:
            return
        self.do_import([path])

    def import_from_clipboard(self):
        """把剪贴板文本存成文档"""
        text = QApplication.clipboard().text()
        if not text.strip():
            QMessageBox.information(self, "剪贴板为空", "请先复制一段命令说明或配置片段。")
            return
        name, ok = QInputDialog.getText(
            self, "保存剪贴内容", "文件名（可留空）:",
            text=f"剪贴内容_{time.strftime('%Y%m%d_%H%M%S')}.md"
        )
        if not ok:
            return
        saved = self.kb.import_text(name, text)
        if not saved:
            QMessageBox.warning(self, "保存失败", self.kb.last_error or "未知错误")
            return
        QMessageBox.information(self, "已保存", f"已保存为:\n{saved}")
        self.after_change(rebuild=True)

    def create_note(self):
        """新建一条笔记"""
        dialog = NoteDialog(self, title="", content="", category="命令速查",
                            root_dir=self.kb.root)
        if not dialog.exec():
            return
        values = dialog.get_values()
        if not values["content"]:
            QMessageBox.information(self, "内容为空", "内容为空，未保存。")
            return
        path = self.kb.save_note(values["title"], values["content"],
                                 values["category"], values["tags"])
        if not path:
            QMessageBox.warning(self, "保存失败", self.kb.last_error or "未知错误")
            return
        self.after_change(rebuild=True)

    def do_import(self, paths):
        """执行导入并汇报结果"""
        move = self.chk_move.isChecked()
        subdir = self.subdir_input.text().strip()

        result = self.kb.import_paths(paths, move=move, subdir=subdir)

        lines = [f"新增 {len(result['added'])} 个文件"]
        if result["skipped"]:
            lines.append(f"跳过 {len(result['skipped'])} 个（不支持的类型）")
        if result["added"]:
            lines.append("\n已导入:")
            lines.extend(f"  · {name}" for name in result["added"][:20])
            if len(result["added"]) > 20:
                lines.append(f"  …… 其余 {len(result['added']) - 20} 个")
        if result["errors"]:
            lines.append("\n提示:")
            lines.extend(f"  · {os.path.basename(p)}: {msg}"
                         for p, msg in result["errors"][:10])
        if result["extracted"]:
            lines.append("\n抽取异常:")
            lines.extend(f"  · {name}: {reason}" for name, reason in result["extracted"][:10])

        QMessageBox.information(self, "导入完成", "\n".join(lines))
        self.after_change(rebuild=True)

    def after_change(self, rebuild: bool = True):
        """导入/删除/新建后的统一收尾"""
        self._note_cache.clear()
        self.refresh_filter_options()
        self.refresh_table()
        if rebuild:
            self.rebuild_index(force=True)
        else:
            self.refresh_stats()
        self.knowledge_changed.emit()

    def refresh_all(self):
        self._note_cache.clear()
        self.refresh_filter_options()
        self.refresh_table()
        self.refresh_stats()

    # ------------------------------------------------------------ 索引
    def rebuild_index(self, force: bool = False):
        """后台重建索引，避免大 PDF 卡住界面"""
        if self.progress.isVisible():
            return
        self.progress.setVisible(True)
        self.stats_label.setText("正在抽取正文并建立索引…")
        self.set_buttons_enabled(False)

        def worker():
            try:
                summary = self.kb.build_index(force=force)
            except Exception as e:
                summary = {"error": str(e), "documents": 0, "chunks": 0, "failed": []}
            self.index_finished.emit(summary)

        threading.Thread(target=worker, daemon=True).start()

    def on_index_finished(self, summary: dict):
        """索引完成"""
        self.progress.setVisible(False)
        self.set_buttons_enabled(True)
        self.refresh_all()

        if summary.get("error"):
            QMessageBox.warning(self, "索引失败", summary["error"])
        elif summary.get("failed"):
            detail = "\n".join(f"  · {name}: {reason}"
                               for name, reason in summary["failed"][:10])
            QMessageBox.information(
                self, "部分文档无法解析",
                f"已索引 {summary.get('documents', 0)} 篇文档、{summary.get('chunks', 0)} 个片段。\n"
                f"以下文档没能抽取到文本：\n{detail}\n\n"
                "扫描件 PDF 需要先做 OCR；老版 Office 格式请先另存为新格式。"
            )
        self.knowledge_changed.emit()

    def set_buttons_enabled(self, enabled: bool):
        for button in (self.btn_import_files, self.btn_import_dir,
                       self.btn_import_text, self.btn_new_note, self.btn_delete,
                       self.btn_reindex, self.btn_refresh,
                       self.btn_export_manual, self.btn_export_csv):
            button.setEnabled(enabled)

    # ------------------------------------------------------------ 筛选
    def refresh_filter_options(self):
        """重建分类 / 标签下拉（保持当前选择）"""
        notes = self.load_notes_cached()
        categories = collect_categories(notes)
        tags = collect_tags(notes)

        current_category = self.category_combo.currentData() or ""
        current_tag = self.tag_combo.currentData() or ""

        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem(ALL_CATEGORIES, "")
        for name, count in categories.items():
            self.category_combo.addItem(f"{name}（{count}）", name)
        index = self.category_combo.findData(current_category)
        self.category_combo.setCurrentIndex(index if index >= 0 else 0)
        self.category_combo.blockSignals(False)

        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem(ALL_TAGS, "")
        for name, count in tags.items():
            self.tag_combo.addItem(f"{name}（{count}）", name)
        index = self.tag_combo.findData(current_tag)
        self.tag_combo.setCurrentIndex(index if index >= 0 else 0)
        self.tag_combo.blockSignals(False)

    def reset_filter(self):
        self.category_combo.setCurrentIndex(0)
        self.tag_combo.setCurrentIndex(0)
        self.filter_input.clear()
        self.refresh_table()

    def current_filters(self):
        category = self.category_combo.currentData() or ""
        tag = self.tag_combo.currentData() or ""
        return category, tag, self.filter_input.text().strip()

    # ------------------------------------------------------------ 笔记缓存
    def load_notes_cached(self):
        """读取笔记信息（按路径+修改时间缓存，避免反复读盘）"""
        notes = []
        for document in self.kb.list_documents():
            if not str(document.get("name", "")).lower().endswith(('.md', '.markdown', '.txt')):
                continue
            key = (document["path"], document.get("mtime"))
            if key not in self._note_cache:
                parsed = load_notes(self.kb, [document])
                self._note_cache[key] = parsed[0] if parsed else None
            note = self._note_cache[key]
            if note:
                notes.append(note)
        return notes

    def find_note(self, name: str):
        """按资料名找笔记信息"""
        for note in self.load_notes_cached():
            if note["name"] == name:
                return note
        return None

    def document_category(self, document: dict) -> str:
        """判断一份资料属于哪个分类（笔记取元信息，原始文档取目录名）"""
        name = str(document.get("name", ""))
        if name.lower().endswith(('.md', '.markdown', '.txt')):
            note = self.find_note(name)
            if note:
                return note["category"] or "未分类"
        normalized = name.replace('\\', '/')
        if '/' in normalized:
            return normalized.split('/')[0]
        return RAW_DOC_CATEGORY

    def document_tags(self, document: dict) -> str:
        """取笔记标签（原始文档没有）"""
        name = str(document.get("name", ""))
        if not name.lower().endswith(('.md', '.markdown', '.txt')):
            return ""
        note = self.find_note(name)
        return ", ".join(note["tags"]) if note else ""

    # ------------------------------------------------------------ 列表
    def refresh_table(self):
        """按筛选条件刷新资料列表"""
        documents = self.kb.list_documents()
        category_filter, tag_filter, keyword = self.current_filters()
        keyword_lower = keyword.lower()

        self.table.setRowCount(0)
        shown = 0
        for document in documents:
            category = self.document_category(document)
            tags_text = self.document_tags(document)

            if category_filter and category != category_filter:
                continue
            if tag_filter:
                tag_list = [t.strip() for t in tags_text.split(',') if t.strip()]
                if tag_filter not in tag_list:
                    continue
            if keyword_lower:
                haystack = f"{document['name']} {category} {tags_text}".lower()
                if keyword_lower not in haystack:
                    continue

            row = self.table.rowCount()
            self.table.insertRow(row)

            check_item = QTableWidgetItem()
            check_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check_item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check_item)

            name_item = QTableWidgetItem(document["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, document["path"])
            if document["error"]:
                name_item.setToolTip(document["error"])
            self.table.setItem(row, 1, name_item)

            self.table.setItem(row, 2, QTableWidgetItem(category))
            tag_item = QTableWidgetItem(tags_text)
            tag_item.setToolTip(tags_text)
            self.table.setItem(row, 3, tag_item)
            self.table.setItem(row, 4, QTableWidgetItem(document["ext"]))
            self.table.setItem(row, 5, QTableWidgetItem(self.human_size(document["size"])))
            self.table.setItem(row, 6, QTableWidgetItem(document["mtime_text"]))

            if document["error"]:
                status, color = document["error"][:30], "#f85149"
            elif not document["supported"]:
                status, color = "不支持的类型", "#f85149"
            elif document["chunks"] == 0:
                status, color = "待索引", "#d29922"
            else:
                status, color = f"已索引({document['chunks']}段)", "#3fb950"
            status_item = QTableWidgetItem(status)
            status_item.setForeground(QColor(color))
            self.table.setItem(row, 7, status_item)
            shown += 1

        if shown != len(documents):
            self.stats_label.setText(
                f"筛选后显示 {shown} / {len(documents)} 项"
            )
        else:
            self.refresh_stats()

    def refresh_stats(self):
        """刷新统计信息"""
        stats = self.kb.stats()
        self.path_label.setText(stats["root"])
        text = (f"共 {stats['documents']} 篇资料，{stats['chunks']} 个检索片段，"
                f"占用 {self.human_size(stats['size'])}")
        if stats["failed"]:
            text += f"，其中 {stats['failed']} 篇无法解析"
        self.stats_label.setText(text)

    @staticmethod
    def human_size(size: int) -> str:
        size = float(size or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"

    def set_all_checked(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(state)

    def checked_paths(self):
        """勾选的资料路径"""
        paths = []
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked:
                item = self.table.item(row, 1)
                if item:
                    paths.append(item.data(Qt.ItemDataRole.UserRole))
        return paths

    def selected_path(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 1)
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def open_selected_file(self):
        path = self.selected_path()
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def delete_selected(self):
        paths = self.checked_paths() or ([self.selected_path()] if self.selected_path() else [])
        if not paths:
            QMessageBox.information(self, "未选中", "请先勾选或选中要删除的资料。")
            return

        preview = "\n".join(f"  · {os.path.relpath(p, self.kb.root)}" for p in paths[:10])
        if len(paths) > 10:
            preview += f"\n  …… 其余 {len(paths) - 10} 项"
        answer = QMessageBox.question(
            self, "确认删除",
            f"确定从资料库中删除以下 {len(paths)} 项？\n\n{preview}\n\n"
            "（只会删除资料库目录里的副本，不影响你原来的下载文件）"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        failed = []
        for path in paths:
            if not self.kb.remove_document(path):
                failed.append(os.path.basename(path))
        if failed:
            QMessageBox.warning(self, "部分删除失败", "\n".join(failed[:10]))
        self.after_change(rebuild=False)

    # ------------------------------------------------------------ 导出
    def select_notes_for_export(self):
        """挑出要导出手册的笔记：优先勾选，其次按当前筛选"""
        notes = self.load_notes_cached()
        if not notes:
            return []
        checked = set(self.checked_paths())
        if checked:
            return [n for n in notes if n["path"] in checked]
        category, tag, keyword = self.current_filters()
        return filter_notes(
            notes,
            categories=[category] if category else None,
            tags=[tag] if tag else None,
            keyword=keyword,
        )

    def export_selected_manual(self):
        """把笔记合并成一份手册（Markdown / HTML）"""
        notes = self.load_notes_cached()
        if not notes:
            QMessageBox.information(
                self, "没有笔记",
                "资料库里还没有可导出的笔记。\n\n"
                "笔记来源：AI 面板「💾 存为资料」、本窗口「新建笔记…」。\n"
                "PDF/Word 等原始文档不参与手册合并。"
            )
            return

        selected = self.select_notes_for_export()
        if not selected:
            QMessageBox.information(
                self, "没有可导出的笔记",
                "勾选的条目里没有笔记（PDF/Word 等原始文档不参与合并）。\n"
                "可以先勾选 .md 笔记，或清空勾选按当前筛选条件导出。"
            )
            return

        title, ok = QInputDialog.getText(
            self, "导出手册", "手册标题:",
            text=f"命令手册 {time.strftime('%Y-%m-%d')}"
        )
        if not ok:
            return

        path, chosen_filter = QFileDialog.getSaveFileName(
            self, "导出手册",
            f"{title or '命令手册'}.md",
            "Markdown (*.md);;HTML 手册 (*.html)"
        )
        if not path:
            return

        fmt = 'html' if ('html' in (chosen_filter or '').lower()
                         or path.lower().endswith('.html')) else 'md'
        subtitle = f"共 {len(selected)} 篇笔记　|　由 AITerminal 本地资料库导出"
        if export_manual(path, selected, title or "命令手册", subtitle, fmt):
            extra = ("HTML 版本可直接用浏览器打开并打印成 PDF。"
                     if fmt == 'html' else "Markdown 版本可直接编辑或再次导入资料库。")
            QMessageBox.information(
                self, "导出成功",
                f"已导出 {len(selected)} 篇笔记到:\n{path}\n\n{extra}"
            )
        else:
            QMessageBox.warning(self, "导出失败", "写入文件失败，请换个路径再试。")

    def export_note_list(self):
        """导出笔记清单 CSV"""
        notes = self.load_notes_cached()
        if not notes:
            QMessageBox.information(self, "没有笔记", "资料库里还没有笔记。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出笔记清单",
            f"笔记清单_{time.strftime('%Y%m%d_%H%M')}.csv",
            "CSV 文件 (*.csv)"
        )
        if not path:
            return
        if export_notes_csv(path, notes):
            QMessageBox.information(self, "导出成功", f"已导出 {len(notes)} 条记录到:\n{path}")
        else:
            QMessageBox.warning(self, "导出失败", "写入 CSV 失败。")

    # ------------------------------------------------------------ 目录
    def open_directory(self):
        """在文件管理器中打开资料库目录"""
        try:
            self.kb.ensure_dir()
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.kb.root))
        except Exception as e:
            QMessageBox.information(self, "资料库目录", f"{self.kb.root}\n\n打开失败: {e}")

    def change_directory(self):
        """更换资料库目录"""
        path = QFileDialog.getExistingDirectory(self, "选择资料库目录", self.kb.root)
        if not path:
            return
        self.settings.set("knowledge_dir", path)
        self.settings.save()
        self.kb.set_root(path)
        self._note_cache.clear()
        self.after_change(rebuild=True)

    def on_enabled_changed(self, _state):
        """开关本地资料检索"""
        enabled = self.chk_enabled.isChecked()
        self.settings.knowledge_enabled = enabled
        self.settings.save()
        self.kb.set_enabled(enabled)
        self.knowledge_changed.emit()

    # ------------------------------------------------------------ 检索测试
    def test_search(self):
        """在本地资料库里试检索，验证导入是否生效"""
        query = self.query_input.text().strip()
        if not query:
            return
        if not self.kb.enabled:
            self.result_view.setPlainText(
                "本地资料检索已关闭，请勾选「查命令时检索本地资料库」。")
            return

        hits = self.kb.search(query, top_k=5)
        if not hits:
            self.result_view.setPlainText(
                f"「{query}」在本地资料库中没有命中。\n\n"
                "可以试试：\n"
                "· 换更具体的关键词（带上型号或完整命令）\n"
                "· 确认文件状态是「已索引」（未索引的点「重建索引」）\n"
                "· 扫描件 PDF 需要先 OCR 才能检索"
            )
            return

        lines = [f"「{query}」命中 {len(hits)} 条：\n"]
        for index, hit in enumerate(hits, start=1):
            lines.append(f"[{index}] {hit['source']}（第 {hit['chunk_index'] + 1} 段，"
                         f"匹配度 {hit['score']}）")
            snippet = hit["text"].replace('\n', ' / ')
            lines.append(f"    {snippet[:220]}")
            lines.append("")
        self.result_view.setPlainText("\n".join(lines))
