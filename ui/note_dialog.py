"""
沉淀笔记对话框 - 把 AI 回答 / 命令清单存回本地资料库

存进去的内容会参与本地检索，等于把"查到的命令"变成自己的知识库。
"""
import os

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QComboBox, QPlainTextEdit, QPushButton, QLabel, QGroupBox
)

from core.knowledge import KnowledgeBase


class NoteDialog(QDialog):
    """
    保存为资料

    参数:
        title: 预填标题
        content: 预填正文
        category: 预填分类
        tags: 预填标签（列表或逗号分隔字符串）
        root_dir: 资料库目录（用于显示保存位置）
    """

    def __init__(self, parent=None, title: str = "", content: str = "",
                 category: str = "AI笔记", tags=None, root_dir: str = ""):
        super().__init__(parent)
        self.setWindowTitle("存为资料（沉淀到本地知识库）")
        self.resize(680, 560)
        self.root_dir = root_dir
        self.init_ui(title, content, category, tags)

    # ------------------------------------------------------------ 界面
    def init_ui(self, title: str, content: str, category: str, tags):
        layout = QVBoxLayout(self)

        tip = QLabel(
            "保存后这份内容会进入本地资料库，AI 以后查命令时能直接检索到。\n"
            "建议标题写清「厂商 + 型号 + 主题」，方便以后搜。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #8a8a8a;")
        layout.addWidget(tip)

        form_group = QGroupBox("资料信息")
        form = QFormLayout()

        self.title_input = QLineEdit(title)
        self.title_input.setPlaceholderText("例如：H3C 端口镜像配置命令")
        form.addRow("标题:", self.title_input)

        self.category_combo = QComboBox()
        self.category_combo.setEditable(True)
        self.category_combo.addItems(list(KnowledgeBase.NOTE_CATEGORIES))
        self.category_combo.setCurrentText(category or "AI笔记")
        form.addRow("分类:", self.category_combo)

        if isinstance(tags, str):
            tag_text = tags
        else:
            tag_text = ", ".join(str(t) for t in (tags or []))
        self.tags_input = QLineEdit(tag_text)
        self.tags_input.setPlaceholderText("逗号分隔，例如：端口镜像, mirroring-group, S5560")
        form.addRow("标签:", self.tags_input)

        form_group.setLayout(form)
        layout.addWidget(form_group)

        content_group = QGroupBox("内容（可编辑后再保存）")
        content_layout = QVBoxLayout()
        self.content_edit = QPlainTextEdit(content or "")
        self.content_edit.setStyleSheet("font-family: Consolas, monospace;")
        content_layout.addWidget(self.content_edit)
        content_group.setLayout(content_layout)
        layout.addWidget(content_group, 1)

        self.path_label = QLabel("")
        self.path_label.setStyleSheet("color: #8a8a8a; font-size: 11px;")
        layout.addWidget(self.path_label)
        self.update_path_hint()
        self.title_input.textChanged.connect(self.update_path_hint)
        self.category_combo.currentTextChanged.connect(self.update_path_hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_save = QPushButton("保存到资料库")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self.accept)
        btn_row.addWidget(btn_save)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def update_path_hint(self):
        """提示将保存到哪里"""
        if not self.root_dir:
            self.path_label.setText("")
            return
        title = self.title_input.text().strip() or "笔记"
        title = os.path.basename(title.replace('\\', '/'))
        category = self.category_combo.currentText().strip() or "AI笔记"
        self.path_label.setText(
            f"保存位置: {os.path.join(self.root_dir, category, title + '.md')}"
        )

    # ------------------------------------------------------------ 取值
    def get_values(self) -> dict:
        """返回填写结果"""
        return {
            "title": self.title_input.text().strip(),
            "category": self.category_combo.currentText().strip() or "AI笔记",
            "tags": self.tags_input.text().strip(),
            "content": self.content_edit.toPlainText().strip(),
        }
