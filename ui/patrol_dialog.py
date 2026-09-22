"""
多设备并发巡检 - 同时对多台设备跑同一组命令，结果导出 Excel

界面结构:
    上: 设备列表（勾选要巡检的设备）
    中: 巡检命令（多行，可从模板载入）
    下: 参数 + 进度 + 日志 + 导出
"""
import os
import time

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog,
    QMessageBox, QGroupBox, QPlainTextEdit, QSpinBox, QDoubleSpinBox,
    QCheckBox, QAbstractItemView, QProgressBar, QSplitter, QWidget,
    QComboBox
)
from PyQt6.QtCore import Qt

from core.patrol import PatrolRunner, export_patrol_excel, export_patrol_csv, STATUS_TEXT
from core.safety import has_config_command
from ui.command_confirm import ConfigConfirmDialog
from config.templates import COMMAND_TEMPLATES, template_commands
from config.settings import Settings

DEFAULT_COMMANDS = """display version
display interface brief
display cpu-usage
display memory
display logbuffer"""


class PatrolDialog(QDialog):
    """
    多设备并发巡检

    参数:
        candidates: 可选设备列表，每项是一个连接参数字典
                    （至少含 name / protocol / host，SSH/Telnet 需 username/password）
        sessions:   当前打开的会话（用于"从当前会话添加"）
    """

    def __init__(self, parent=None, candidates=None, sessions=None):
        super().__init__(parent)
        self.settings = Settings()
        self.candidates = list(candidates or [])
        self.sessions = list(sessions or [])
        self.runner = None
        self.last_summary = {}
        self.setWindowTitle("多设备并发巡检")
        self.resize(1020, 760)
        self.init_ui()
        self.load_settings()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # ---------------- 设备列表
        device_group = QGroupBox("巡检设备（勾选要巡检的设备）")
        device_layout = QVBoxLayout()

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["选择", "名称", "协议", "地址", "来源"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in (2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        device_layout.addWidget(self.table)

        row = QHBoxLayout()
        btn_from_sessions = QPushButton("从当前会话添加")
        btn_from_sessions.setToolTip("把界面上已打开的会话加入巡检列表")
        btn_from_sessions.clicked.connect(self.add_from_sessions)
        row.addWidget(btn_from_sessions)

        btn_from_candidates = QPushButton("添加最近连接")
        btn_from_candidates.setToolTip("把最近连接过的设备加入列表（不含密码，需手动补）")
        btn_from_candidates.clicked.connect(self.add_from_candidates)
        row.addWidget(btn_from_candidates)

        btn_all = QPushButton("全选")
        btn_all.clicked.connect(lambda: self.set_all_checked(True))
        row.addWidget(btn_all)

        btn_none = QPushButton("全不选")
        btn_none.clicked.connect(lambda: self.set_all_checked(False))
        row.addWidget(btn_none)

        btn_remove = QPushButton("移除选中")
        btn_remove.clicked.connect(self.remove_selected)
        row.addWidget(btn_remove)

        btn_clear = QPushButton("清空列表")
        btn_clear.clicked.connect(self.clear_devices)
        row.addWidget(btn_clear)
        row.addStretch()
        device_layout.addLayout(row)

        self.device_hint = QLabel("提示：SSH/Telnet 设备需要用户名密码，"
                                  "从「当前会话添加」会自动带上。")
        self.device_hint.setStyleSheet("color: #98a1b5; font-size: 11px;")
        device_layout.addWidget(self.device_hint)

        device_group.setLayout(device_layout)
        splitter.addWidget(device_group)

        # ---------------- 命令
        command_group = QGroupBox("巡检命令（每行一条，按顺序执行）")
        command_layout = QVBoxLayout()

        self.command_edit = QPlainTextEdit(DEFAULT_COMMANDS)
        self.command_edit.setMaximumHeight(140)
        self.command_edit.setStyleSheet("font-family: Consolas, monospace;")
        command_layout.addWidget(self.command_edit)

        cmd_row = QHBoxLayout()
        cmd_row.addWidget(QLabel("从模板载入:"))
        self.template_combo = QComboBox()
        self.template_combo.addItems(list(COMMAND_TEMPLATES.keys()))
        cmd_row.addWidget(self.template_combo, 1)

        btn_load_template = QPushButton("载入")
        btn_load_template.clicked.connect(self.load_template)
        cmd_row.addWidget(btn_load_template)

        btn_restore = QPushButton("恢复默认命令")
        btn_restore.clicked.connect(
            lambda: self.command_edit.setPlainText(DEFAULT_COMMANDS))
        cmd_row.addWidget(btn_restore)
        command_layout.addLayout(cmd_row)

        command_group.setLayout(command_layout)
        splitter.addWidget(command_group)

        # ---------------- 参数
        option_group = QGroupBox("执行参数")
        option_layout = QHBoxLayout()

        option_layout.addWidget(QLabel("并发数:"))
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 32)
        self.concurrency_spin.setToolTip("同时巡检几台设备，过大可能被设备侧限制")
        option_layout.addWidget(self.concurrency_spin)

        option_layout.addWidget(QLabel("连接超时:"))
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(1, 120)
        self.timeout_spin.setSuffix(" 秒")
        option_layout.addWidget(self.timeout_spin)

        option_layout.addWidget(QLabel("单命令超时:"))
        self.cmd_timeout_spin = QDoubleSpinBox()
        self.cmd_timeout_spin.setRange(3, 600)
        self.cmd_timeout_spin.setSuffix(" 秒")
        self.cmd_timeout_spin.setToolTip("等待提示符返回的最长时间，大配置建议调大")
        option_layout.addWidget(self.cmd_timeout_spin)

        self.chk_auto_page = QCheckBox("自动翻页")
        self.chk_auto_page.setToolTip("遇到 ---- More ---- 自动补空格继续")
        option_layout.addWidget(self.chk_auto_page)

        self.chk_block = QCheckBox("跳过危险命令")
        option_layout.addWidget(self.chk_block)
        option_layout.addStretch()

        option_group.setLayout(option_layout)
        splitter.addWidget(option_group)

        layout.addWidget(splitter, 1)

        # ---------------- 进度与日志
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        progress_row.addWidget(self.progress, 1)

        self.btn_start = QPushButton("开始巡检")
        self.btn_start.setDefault(True)
        self.btn_start.clicked.connect(self.start_patrol)
        progress_row.addWidget(self.btn_start)

        self.btn_stop = QPushButton("中止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_patrol)
        progress_row.addWidget(self.btn_stop)
        layout.addLayout(progress_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(150)
        self.log_view.setStyleSheet("background-color: #171a24; color: #e6e9f2; "
                                    "font-family: Consolas, monospace; font-size: 11px;")
        layout.addWidget(self.log_view)

        # ---------------- 导出
        export_row = QHBoxLayout()
        self.summary_label = QLabel("尚未执行巡检")
        self.summary_label.setStyleSheet("color: #98a1b5;")
        export_row.addWidget(self.summary_label, 1)

        self.btn_excel = QPushButton("导出 Excel")
        self.btn_excel.setEnabled(False)
        self.btn_excel.clicked.connect(self.export_excel)
        export_row.addWidget(self.btn_excel)

        self.btn_csv = QPushButton("导出 CSV")
        self.btn_csv.setEnabled(False)
        self.btn_csv.clicked.connect(self.export_csv)
        export_row.addWidget(self.btn_csv)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        export_row.addWidget(btn_close)
        layout.addLayout(export_row)

    # ------------------------------------------------------------ 设置
    def load_settings(self):
        """载入上次的参数与命令"""
        self.concurrency_spin.setValue(self.settings.patrol_concurrency)
        self.timeout_spin.setValue(self.settings.patrol_timeout)
        self.cmd_timeout_spin.setValue(self.settings.patrol_prompt_timeout)
        self.chk_auto_page.setChecked(self.settings.batch_auto_page)
        self.chk_block.setChecked(self.settings.block_dangerous)

        saved_commands = self.settings.get_patrol_commands()
        if saved_commands:
            self.command_edit.setPlainText("\n".join(saved_commands))

        # 默认把当前会话全部加入
        self.add_from_sessions(select_all=True)

    def save_settings(self):
        self.settings.set("patrol_concurrency", self.concurrency_spin.value())
        self.settings.set("patrol_timeout", self.timeout_spin.value())
        self.settings.set("patrol_prompt_timeout", self.cmd_timeout_spin.value())
        self.settings.set("batch_auto_page", self.chk_auto_page.isChecked())
        self.settings.set_patrol_commands(self.get_commands())
        self.settings.save()

    # ------------------------------------------------------------ 设备
    def add_device_row(self, device: dict, checked: bool = True, source: str = ""):
        """往表格里加一行设备"""
        row = self.table.rowCount()
        self.table.insertRow(row)

        check_item = QTableWidgetItem()
        check_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        check_item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.table.setItem(row, 0, check_item)

        name_item = QTableWidgetItem(device.get('name', '未命名'))
        name_item.setData(Qt.ItemDataRole.UserRole, device)
        self.table.setItem(row, 1, name_item)
        self.table.setItem(row, 2, QTableWidgetItem(device.get('protocol', '')))
        self.table.setItem(row, 3, QTableWidgetItem(
            device.get('host') or device.get('serial_port', '')))
        self.table.setItem(row, 4, QTableWidgetItem(source or device.get('source', '')))

    def existing_keys(self):
        """已加入列表的设备标识，用于去重"""
        keys = set()
        for row in range(self.table.rowCount()):
            device = self.table.item(row, 1).data(Qt.ItemDataRole.UserRole) or {}
            keys.add((device.get('protocol'), device.get('host'),
                      device.get('serial_port'), device.get('username')))
        return keys

    def add_from_sessions(self, select_all: bool = False):
        """把当前打开的会话加入列表"""
        if not self.sessions:
            if not select_all:
                QMessageBox.information(self, "没有会话", "界面上还没有已连接的会话。")
            return
        keys = self.existing_keys()
        added = 0
        for session in self.sessions:
            device = dict(session)
            key = (device.get('protocol'), device.get('host'),
                   device.get('serial_port'), device.get('username'))
            if key in keys:
                continue
            self.add_device_row(device, checked=True, source="当前会话")
            keys.add(key)
            added += 1
        if added:
            self.log_view.appendPlainText(f"已从当前会话添加 {added} 台设备")

    def add_from_candidates(self):
        """把候选设备（最近连接等）加入列表"""
        if not self.candidates:
            QMessageBox.information(self, "没有候选设备",
                                    "还没有最近连接记录，可以先连一次设备。")
            return
        keys = self.existing_keys()
        added = 0
        for device in self.candidates:
            item = dict(device)
            key = (item.get('protocol'), item.get('host'),
                   item.get('serial_port'), item.get('username'))
            if key in keys:
                continue
            self.add_device_row(item, checked=True,
                                source=item.get('source', '最近连接'))
            keys.add(key)
            added += 1
        self.log_view.appendPlainText(f"已添加 {added} 台候选设备（密码需自行确认）")

    def set_all_checked(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(state)

    def remove_selected(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def clear_devices(self):
        self.table.setRowCount(0)

    def checked_devices(self):
        """取勾选的设备配置"""
        devices = []
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked:
                device = self.table.item(row, 1).data(Qt.ItemDataRole.UserRole)
                if device:
                    devices.append(dict(device))
        return devices

    # ------------------------------------------------------------ 命令
    def load_template(self):
        """从模板载入巡检命令"""
        name = self.template_combo.currentText()
        commands = template_commands(name, {})
        if not commands:
            return
        if any('{' in command for command in commands):
            QMessageBox.information(
                self, "模板含变量",
                "该模板里有 {变量}，载入后请手动替换成实际值再开始巡检。"
            )
        self.command_edit.setPlainText("\n".join(commands))

    def get_commands(self):
        """取命令列表（去空行与注释）"""
        result = []
        for line in self.command_edit.toPlainText().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                result.append(line)
        return result

    # ------------------------------------------------------------ 巡检
    def start_patrol(self):
        devices = self.checked_devices()
        commands = self.get_commands()

        if not devices:
            QMessageBox.warning(self, "没有设备", "请先勾选要巡检的设备。")
            return
        if not commands:
            QMessageBox.warning(self, "没有命令", "请填写巡检命令。")
            return
        missing = [d.get('name', '') for d in devices
                   if d.get('protocol') in ('SSH', 'Telnet') and not d.get('username')]
        if missing:
            answer = QMessageBox.question(
                self, "缺少登录信息",
                "以下设备没有用户名，可能连接失败：\n  " + "\n  ".join(missing[:8])
                + "\n\n仍然继续？"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.save_settings()
        self.log_view.clear()
        self.progress.setRange(0, len(devices))
        self.progress.setValue(0)

        # 安全闸门：巡检命令里含配置类命令时，先展示命令清单等确认
        if self.settings.confirm_config_commands and has_config_command(commands):
            dialog = ConfigConfirmDialog(
                self, commands, target=f"{len(devices)} 台设备", action="并发巡检")
            if not dialog.confirmed:
                self.log_view.appendPlainText("已取消：配置命令未经确认")
                return

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_excel.setEnabled(False)
        self.btn_csv.setEnabled(False)
        self.summary_label.setText("巡检进行中…")

        self.runner = PatrolRunner(
            devices, commands,
            concurrency=self.concurrency_spin.value(),
            timeout=self.timeout_spin.value(),
            prompt_timeout=self.cmd_timeout_spin.value(),
            auto_page=self.chk_auto_page.isChecked(),
            block_dangerous=self.chk_block.isChecked(),
            task_name=f"巡检 {time.strftime('%Y-%m-%d %H:%M')}",
            parent=self,
        )
        self.runner.log.connect(self.on_log)
        self.runner.device_command.connect(self.on_device_command)
        self.runner.device_done.connect(self.on_device_done)
        self.runner.finished.connect(self.on_finished)
        self.runner.start()

    def stop_patrol(self):
        if self.runner and self.runner.running:
            self.runner.stop()
            self.btn_stop.setEnabled(False)
            self.log_view.appendPlainText("已请求中止，正在等待已发出的命令结束…")

    def on_log(self, text: str):
        self.log_view.appendPlainText(text)

    def on_device_command(self, device_index: int, command_index: int, command: str):
        name = ""
        if self.runner and device_index < len(self.runner.devices):
            name = self.runner.devices[device_index].get('name', '')
        self.summary_label.setText(f"{name} 正在执行: {command}")

    def on_device_done(self, _device_index: int, result: dict):
        self.progress.setValue(self.progress.value() + 1)
        commands = result.get("commands") or []
        abnormal = [c for c in commands if c["status"] not in ("prompt", "silence", "delay")]
        if abnormal:
            detail = "；".join(
                f"{c['command']} → {STATUS_TEXT.get(c['status'], c['status'])}"
                for c in abnormal[:3]
            )
            self.log_view.appendPlainText(f"    ⚠ {result.get('name')}: {detail}")

    def on_finished(self, ok: bool, summary: dict):
        self.last_summary = summary or {}
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

        if summary.get("error"):
            self.summary_label.setText(summary["error"])
            QMessageBox.warning(self, "巡检未执行", summary["error"])
            return

        self.summary_label.setText(
            f"巡检{'已中止' if summary.get('aborted') else '完成'}："
            f"成功 {summary.get('ok', 0)} 台 / 异常 {summary.get('failed', 0)} 台，"
            f"耗时 {summary.get('elapsed', 0):.1f} 秒"
        )
        self.btn_excel.setEnabled(bool(self.runner and self.runner.results))
        self.btn_csv.setEnabled(bool(self.runner and self.runner.results))
        if ok and self.runner and self.runner.results:
            self.log_view.appendPlainText("巡检结束，可以导出 Excel 了。")

    # ------------------------------------------------------------ 导出
    def default_name(self, ext: str) -> str:
        return f"巡检结果_{time.strftime('%Y%m%d_%H%M')}.{ext}"

    def export_excel(self):
        if not (self.runner and self.runner.results):
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出巡检结果", self.default_name("xlsx"),
            "Excel 文件 (*.xlsx)"
        )
        if not path:
            return
        if export_patrol_excel(path, self.runner, self.last_summary):
            QMessageBox.information(
                self, "导出成功",
                f"已导出:\n{path}\n\n包含三个工作表：巡检信息 / 巡检汇总 / 命令明细"
            )
        else:
            QMessageBox.warning(self, "导出失败", "写入 Excel 失败，请换个路径再试。")

    def export_csv(self):
        if not (self.runner and self.runner.results):
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出巡检结果", self.default_name("csv"),
            "CSV 文件 (*.csv)"
        )
        if not path:
            return
        if export_patrol_csv(path, self.runner):
            QMessageBox.information(
                self, "导出成功",
                f"已导出汇总:\n{path}\n明细另存为:\n"
                f"{os.path.splitext(path)[0]}_明细.csv"
            )
        else:
            QMessageBox.warning(self, "导出失败", "写入 CSV 失败。")

    def closeEvent(self, event):
        if self.runner and self.runner.running:
            answer = QMessageBox.question(self, "巡检进行中", "巡检还在进行，确定关闭？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.runner.stop()
        event.accept()
