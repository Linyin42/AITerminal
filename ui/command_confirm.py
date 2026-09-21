"""
配置命令下发确认框

用户要求（长期约定）:
    配置类命令在下发前必须先展示命令清单、等用户确认，程序不允许自动下发配置。

这个对话框就是那道闸门：把所有要下发的命令列出来，
把配置类命令标红并给出风险提示，用户点了"确认下发"才继续。
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QPlainTextEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QApplication
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from core.safety import classify, risk_notes


class ConfigConfirmDialog(QDialog):
    """
    配置命令确认

    参数:
        commands: 将要下发的命令列表
        target: 目标描述（设备名 / 地址）
        action: 动作描述（批量下发 / AI 自动模式 / 巡检…）
    """

    def __init__(self, parent=None, commands=None, target: str = "",
                 action: str = "下发命令"):
        super().__init__(parent)
        self.commands = [str(c).strip() for c in (commands or []) if str(c).strip()]
        self.target = target
        self.action = action
        self.result_info = classify(self.commands)
        self.setWindowTitle("确认下发配置命令")
        self.resize(720, 560)
        self.init_ui()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)

        config_count = len(self.result_info["config"])
        warning = QLabel(
            f"即将对 <b>{self.target or '当前设备'}</b> 执行 <b>{self.action}</b>："
            f"共 {self.result_info['total']} 条命令，其中 "
            f"<b style='color:#f14c4c;'>{config_count} 条会修改设备配置</b>。"
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)

        notes = risk_notes(self.commands)
        if notes:
            note_label = QLabel("风险提示：" + "；".join(notes))
            note_label.setWordWrap(True)
            note_label.setStyleSheet("color: #dcdcaa;")
            layout.addWidget(note_label)

        hint = QLabel("请逐条核对命令无误后再确认。本程序不会自动下发配置命令。")
        hint.setStyleSheet("color: #8a8a8a; font-size: 11px;")
        layout.addWidget(hint)

        # 命令清单
        self.table = QTableWidget(len(self.commands), 3)
        self.table.setHorizontalHeaderLabels(["#", "命令", "类型"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        for row, command in enumerate(self.commands):
            is_config = command in self.result_info["config"]
            self.table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            command_item = QTableWidgetItem(command)
            self.table.setItem(row, 1, command_item)
            type_item = QTableWidgetItem("配置" if is_config else "只读")
            type_item.setForeground(QColor("#f14c4c" if is_config else "#4ec9b0"))
            self.table.setItem(row, 2, type_item)
        self.table.setMaximumHeight(240)
        layout.addWidget(self.table)

        # 可复制文本（方便粘贴到工单/留档）
        self.text_view = QPlainTextEdit("\n".join(self.commands))
        self.text_view.setReadOnly(True)
        self.text_view.setMaximumHeight(90)
        self.text_view.setStyleSheet(
            "background-color: #1e1e1e; color: #d4d4d4; font-family: Consolas; font-size: 11px;")
        layout.addWidget(QLabel("命令原文（可复制留档）:"))
        layout.addWidget(self.text_view)

        # 按钮
        btn_row = QHBoxLayout()
        btn_copy = QPushButton("复制命令")
        btn_copy.clicked.connect(self.copy_commands)
        btn_row.addWidget(btn_copy)
        btn_row.addStretch()

        btn_ok = QPushButton("确认下发")
        btn_ok.setDefault(False)
        btn_ok.clicked.connect(self.on_confirm)
        btn_row.addWidget(btn_ok)

        btn_cancel = QPushButton("取消")
        btn_cancel.setDefault(True)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------ 交互
    def copy_commands(self):
        QApplication.clipboard().setText("\n".join(self.commands))
        QMessageBox.information(self, "已复制", "命令已复制到剪贴板。")

    def on_confirm(self):
        """二次确认：配置命令改设备，点错代价高"""
        config_count = len(self.result_info["config"])
        if config_count:
            answer = QMessageBox.question(
                self, "再次确认",
                f"确认向 {self.target or '当前设备'} 下发这 {config_count} 条配置命令？\n\n"
                "配置会立即在设备上生效（未执行 save 时重启会丢失）。"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    @property
    def confirmed(self) -> bool:
        return self.result() == QDialog.DialogCode.Accepted
