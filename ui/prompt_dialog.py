"""
系统提示词编辑对话框 - 自定义 AI 的行为约束
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QPushButton,
    QLabel, QMessageBox, QGroupBox
)

from core.ai_agent import AIAgent


class PromptDialog(QDialog):
    """
    编辑系统提示词

    返回:
        get_prompt() 返回最终文本，空字符串表示恢复为内置提示词
    """

    def __init__(self, parent=None, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle("编辑系统提示词")
        self.resize(760, 620)
        self.init_ui(current)

    def init_ui(self, current: str):
        layout = QVBoxLayout(self)

        tip = QLabel(
            "系统提示词决定 AI 的输出规范。默认约束为：只输出命令本身、"
            "任务完成输出 DONE、无法判断输出 ASK。\n"
            "清空内容并保存即恢复内置提示词。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #8a8a8a;")
        layout.addWidget(tip)

        group = QGroupBox("提示词内容")
        group_layout = QVBoxLayout()
        self.editor = QPlainTextEdit()
        self.editor.setPlainText(current or AIAgent.default_system_prompt())
        self.editor.setStyleSheet("font-family: Consolas, monospace;")
        group_layout.addWidget(self.editor)
        group.setLayout(group_layout)
        layout.addWidget(group, 1)

        btn_row = QHBoxLayout()
        btn_default = QPushButton("恢复内置提示词")
        btn_default.clicked.connect(self.restore_default)
        btn_row.addWidget(btn_default)
        btn_row.addStretch()

        btn_save = QPushButton("保存")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self.on_save)
        btn_row.addWidget(btn_save)

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def restore_default(self):
        """填入内置提示词"""
        self.editor.setPlainText(AIAgent.default_system_prompt())

    def on_save(self):
        text = self.editor.toPlainText().strip()
        if not text:
            QMessageBox.information(
                self, "已清空",
                "内容为空，保存后将恢复使用内置提示词。"
            )
        self.accept()

    def get_prompt(self) -> str:
        """返回编辑后的提示词（空字符串表示用内置）"""
        text = self.editor.toPlainText().strip()
        return "" if text == AIAgent.default_system_prompt().strip() else text
