"""
命令模板对话框 - 选择模板、填写变量、预览并批量下发
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QLabel, QGroupBox, QSpinBox, QCheckBox, QSplitter, QWidget,
    QMessageBox, QComboBox
)
from PyQt6.QtCore import Qt, pyqtSignal

from config.templates import COMMAND_TEMPLATES, extract_variables, render_template


class TemplateDialog(QDialog):
    """
    命令模板与批量下发

    信号:
        commands_ready(dict): 用户确认下发，payload 含
            commands / delay_ms / block_dangerous / wait_mode / auto_page
    """

    commands_ready = pyqtSignal(dict)

    #: 等待方式选项
    WAIT_MODES = [
        ("等提示符返回（推荐，慢设备/长输出也稳）", "prompt"),
        ("固定间隔（简单，按设定毫秒数等待）", "delay"),
    ]

    def __init__(self, parent=None, default_delay: int = 500,
                 session_name: str = "", vendor: str = "unknown",
                 wait_mode: str = "prompt", auto_page: bool = True):
        super().__init__(parent)
        self.default_delay = default_delay
        self.session_name = session_name
        self.vendor = vendor
        self.default_wait_mode = wait_mode
        self.default_auto_page = auto_page
        self.var_edits = {}
        self.setWindowTitle(f"命令模板与批量下发 - {session_name}" if session_name else "命令模板与批量下发")
        self.resize(900, 640)
        self.init_ui()
        self.load_templates()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：模板列表
        left = QGroupBox("模板列表")
        left_layout = QVBoxLayout()
        self.tpl_list = QListWidget()
        self.tpl_list.currentRowChanged.connect(self.on_template_changed)
        left_layout.addWidget(self.tpl_list)

        self.tpl_desc = QLabel("")
        self.tpl_desc.setWordWrap(True)
        self.tpl_desc.setStyleSheet("color: #8a8a8a;")
        left_layout.addWidget(self.tpl_desc)
        left.setLayout(left_layout)
        splitter.addWidget(left)

        # 右：变量 + 预览
        right = QWidget()
        right_layout = QVBoxLayout(right)

        var_group = QGroupBox("变量")
        self.var_form = QFormLayout()
        var_group.setLayout(self.var_form)
        right_layout.addWidget(var_group)

        preview_group = QGroupBox("命令预览（可直接编辑）")
        preview_layout = QVBoxLayout()
        self.preview = QPlainTextEdit()
        self.preview.setPlaceholderText("每行一条命令")
        preview_layout.addWidget(self.preview)
        preview_group.setLayout(preview_layout)
        right_layout.addWidget(preview_group, 1)

        # 下发设置
        wait_row = QHBoxLayout()
        wait_row.addWidget(QLabel("等待方式:"))
        self.wait_combo = QComboBox()
        for label, value in self.WAIT_MODES:
            self.wait_combo.addItem(label, value)
        for index in range(self.wait_combo.count()):
            if self.wait_combo.itemData(index) == self.default_wait_mode:
                self.wait_combo.setCurrentIndex(index)
                break
        self.wait_combo.currentIndexChanged.connect(self.on_wait_mode_changed)
        wait_row.addWidget(self.wait_combo, 1)
        right_layout.addLayout(wait_row)

        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("间隔(ms):"))
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 30000)
        self.delay_spin.setSingleStep(100)
        self.delay_spin.setValue(self.default_delay)
        opt_row.addWidget(self.delay_spin)

        self.chk_block = QCheckBox("跳过危险命令")
        self.chk_block.setChecked(True)
        self.chk_block.setToolTip("reboot / delete / erase / undo all 等命令将被跳过")
        opt_row.addWidget(self.chk_block)

        self.chk_page = QCheckBox("自动翻页")
        self.chk_page.setChecked(self.default_auto_page)
        self.chk_page.setToolTip("遇到 ---- More ---- 自动补空格继续，避免卡住")
        opt_row.addWidget(self.chk_page)
        opt_row.addStretch()
        right_layout.addLayout(opt_row)

        self.wait_hint = QLabel("")
        self.wait_hint.setWordWrap(True)
        self.wait_hint.setStyleSheet("color: #8a8a8a; font-size: 11px;")
        right_layout.addWidget(self.wait_hint)
        self.on_wait_mode_changed()

        splitter.addWidget(right)
        splitter.setSizes([260, 620])
        layout.addWidget(splitter, 1)

        # 底部按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.btn_copy = QPushButton("复制命令")
        self.btn_copy.clicked.connect(self.copy_commands)
        btn_row.addWidget(self.btn_copy)

        self.btn_send = QPushButton("下发到当前会话")
        self.btn_send.setDefault(True)
        self.btn_send.clicked.connect(self.on_send)
        btn_row.addWidget(self.btn_send)

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        layout.addLayout(btn_row)

    def load_templates(self):
        """载入模板列表，优先展示与当前厂商匹配的模板"""
        names = list(COMMAND_TEMPLATES.keys())
        if self.vendor in ("h3c", "huawei"):
            names.sort(key=lambda n: (COMMAND_TEMPLATES[n].get("vendor") != "h3c",))
        elif self.vendor == "cisco":
            names.sort(key=lambda n: (COMMAND_TEMPLATES[n].get("vendor") != "cisco",))

        for name in names:
            tpl = COMMAND_TEMPLATES[name]
            item = QListWidgetItem(name)
            item.setToolTip(tpl.get("desc", ""))
            self.tpl_list.addItem(item)
        if names:
            self.tpl_list.setCurrentRow(0)

    # ------------------------------------------------------------ 交互
    def on_template_changed(self, row: int):
        """切换模板：重建变量表单并刷新预览"""
        if row < 0:
            return
        name = self.tpl_list.item(row).text()
        tpl = COMMAND_TEMPLATES.get(name, {})
        self.tpl_desc.setText(tpl.get("desc", ""))

        # 清空旧表单
        while self.var_form.count():
            item = self.var_form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.var_edits.clear()

        variables = tpl.get("variables") or {}
        names = extract_variables(tpl.get("commands", []))
        for var in names:
            edit = QLineEdit(str(variables.get(var, "")))
            edit.textChanged.connect(self.refresh_preview)
            self.var_edits[var] = edit
            self.var_form.addRow(f"{var}:", edit)

        if not names:
            hint = QLabel("该模板无需变量")
            hint.setStyleSheet("color: #8a8a8a;")
            self.var_form.addRow(hint)

        self.refresh_preview()

    def refresh_preview(self):
        """按当前变量渲染预览"""
        tpl = self.current_template()
        if not tpl:
            return
        values = {k: e.text() for k, e in self.var_edits.items()}
        commands = render_template(tpl.get("commands", []), values)
        self.preview.setPlainText("\n".join(commands))

    def current_template(self) -> dict:
        row = self.tpl_list.currentRow()
        if row < 0:
            return {}
        return COMMAND_TEMPLATES.get(self.tpl_list.item(row).text(), {})

    def get_commands(self):
        """取预览区里最终的命令列表（去掉空行与注释）"""
        result = []
        for line in self.preview.toPlainText().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                result.append(line)
        return result

    def copy_commands(self):
        commands = self.get_commands()
        if not commands:
            return
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText("\n".join(commands))

    def on_wait_mode_changed(self):
        """切换等待方式时更新提示与间隔可用性"""
        mode = self.wait_combo.currentData()
        is_delay = (mode == "delay")
        self.delay_spin.setEnabled(is_delay)
        self.chk_page.setEnabled(not is_delay)
        if is_delay:
            self.wait_hint.setText(
                "固定间隔：每条命令后按设定毫秒等待。"
                "设备慢或输出长时容易在命令还没执行完就发下一条，一般不建议。"
            )
        else:
            self.wait_hint.setText(
                "等提示符返回：每条命令发出后，等设备输出末尾回到提示符"
                "（> ] # $ % 结尾）且静默 0.3 秒，再发下一条。"
                "遇到 ---- More ---- 会自动翻页；遇到 [Y/N] 确认提示会停下来交给你处理。"
            )

    def on_send(self):
        commands = self.get_commands()
        if not commands:
            QMessageBox.warning(self, "没有命令", "命令预览为空，请先选择模板或输入命令。")
            return
        self.commands_ready.emit({
            "commands": commands,
            "delay_ms": self.delay_spin.value(),
            "block_dangerous": self.chk_block.isChecked(),
            "wait_mode": self.wait_combo.currentData(),
            "auto_page": self.chk_page.isChecked(),
        })
        self.accept()

    def skip_dangerous(self) -> bool:
        return self.chk_block.isChecked()
