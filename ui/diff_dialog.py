"""
配置比对对话框 - 对比两份配置文本（如两次 display current-configuration 快照）
"""
import html
import time

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QPushButton,
    QLabel, QSplitter, QTextEdit, QFileDialog, QMessageBox, QWidget
)
from PyQt6.QtCore import Qt

from core.diff import diff_lines, summarize, to_unified
from ui.theme import P


class DiffDialog(QDialog):
    """
    配置差异比对

    参数:
        session: 当前会话对象（SessionInfo），可为 None
                 传入后可以使用"从当前会话填入"和"用最近快照作基线"
        autofill: 打开时是否自动填充内容
    """

    def __init__(self, parent=None, session=None, autofill: bool = True):
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("配置差异比对")
        self.resize(1000, 700)
        self.init_ui()
        if autofill:
            self.prefill_from_session()

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)

        # 工具条
        bar = QHBoxLayout()

        btn_left_file = QPushButton("载入基线文件…")
        btn_left_file.clicked.connect(lambda: self.load_file(self.left_edit))
        bar.addWidget(btn_left_file)

        btn_right_file = QPushButton("载入对比文件…")
        btn_right_file.clicked.connect(lambda: self.load_file(self.right_edit))
        bar.addWidget(btn_right_file)

        self.btn_from_session = QPushButton("从当前会话填入右侧")
        self.btn_from_session.clicked.connect(self.fill_from_session)
        self.btn_from_session.setEnabled(session_available(self.session))
        bar.addWidget(self.btn_from_session)

        self.btn_from_snapshot = QPushButton("用最近快照作基线")
        self.btn_from_snapshot.clicked.connect(self.fill_from_snapshot)
        self.btn_from_snapshot.setEnabled(session_available(self.session))
        bar.addWidget(self.btn_from_snapshot)

        bar.addStretch()

        btn_swap = QPushButton("左右互换")
        btn_swap.clicked.connect(self.swap_sides)
        bar.addWidget(btn_swap)

        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self.clear_all)
        bar.addWidget(btn_clear)

        layout.addLayout(bar)

        # 输入区
        inputs = QSplitter(Qt.Orientation.Horizontal)

        left_box = QWidget()
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("基线配置（旧）"))
        self.left_edit = QPlainTextEdit()
        self.left_edit.setPlaceholderText("粘贴或载入旧配置")
        self.left_edit.textChanged.connect(self.mark_stale)
        left_layout.addWidget(self.left_edit)
        inputs.addWidget(left_box)

        right_box = QWidget()
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("对比配置（新）"))
        self.right_edit = QPlainTextEdit()
        self.right_edit.setPlaceholderText("粘贴或载入新配置")
        self.right_edit.textChanged.connect(self.mark_stale)
        right_layout.addWidget(self.right_edit)
        inputs.addWidget(right_box)

        layout.addWidget(inputs, 1)

        # 结果区
        result_header = QHBoxLayout()
        result_header.addWidget(QLabel("差异结果"))
        self.summary_label = QLabel("尚未对比")
        self.summary_label.setStyleSheet(f"color: {P['fg_muted']};")
        result_header.addWidget(self.summary_label)
        result_header.addStretch()

        btn_export = QPushButton("导出 unified diff…")
        btn_export.clicked.connect(self.export_diff)
        result_header.addWidget(btn_export)
        layout.addLayout(result_header)

        self.diff_view = QTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setStyleSheet(
            f"background-color: {P['bg']}; border: 1px solid {P['border']};")
        layout.addWidget(self.diff_view, 1)

        # 底部
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_run = QPushButton("开始对比")
        btn_run.setDefault(True)
        btn_run.clicked.connect(self.run_diff)
        btn_row.addWidget(btn_run)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------ 数据来源
    def prefill_from_session(self):
        """打开时自动把当前会话输出填入右侧，最近快照填入左侧"""
        if not session_available(self.session):
            return
        self.fill_from_snapshot()
        self.fill_from_session()
        if self.left_edit.toPlainText().strip() and self.right_edit.toPlainText().strip():
            self.run_diff()

    def fill_from_session(self):
        """把当前会话最近输出填入右侧"""
        if not session_available(self.session):
            return
        text = self.session.peek_output()
        if not text.strip():
            QMessageBox.information(self, "没有内容", "当前会话暂无输出，请先执行配置查看命令。")
            return
        self.right_edit.setPlainText(text)

    def fill_from_snapshot(self):
        """把最近一份快照填入左侧"""
        if not session_available(self.session):
            return
        snapshot = self.session.last_snapshot()
        if not snapshot:
            QMessageBox.information(self, "没有快照", "当前会话还没有配置快照。\n"
                                                      "可在「工具 → 采集配置快照」中先采一份。")
            return
        label, text, _ts = snapshot
        self.left_edit.setPlainText(text)
        self.summary_label.setText(f"基线来自快照: {label}")

    def load_file(self, target: QPlainTextEdit):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择文件", "", "文本文件 (*.txt *.log *.cfg *.conf);;所有文件 (*)"
        )
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                target.setPlainText(f.read())
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))

    def swap_sides(self):
        left = self.left_edit.toPlainText()
        self.left_edit.setPlainText(self.right_edit.toPlainText())
        self.right_edit.setPlainText(left)

    def clear_all(self):
        self.left_edit.clear()
        self.right_edit.clear()
        self.diff_view.clear()
        self.summary_label.setText("尚未对比")

    def mark_stale(self):
        self.summary_label.setText("内容已修改，请重新点击「开始对比」")

    # ------------------------------------------------------------ 对比
    def run_diff(self):
        old_text = self.left_edit.toPlainText()
        new_text = self.right_edit.toPlainText()
        if not old_text.strip() and not new_text.strip():
            self.summary_label.setText("两侧都为空，无法对比")
            return

        diff = diff_lines(old_text, new_text, context=3)
        added, removed = summarize(old_text, new_text)

        if added == 0 and removed == 0:
            self.summary_label.setText("两侧内容一致，无差异")
        else:
            self.summary_label.setText(f"新增 {added} 行 / 删除 {removed} 行")

        self.render_diff(diff)

    def render_diff(self, diff):
        """按行着色渲染差异（颜色取自主题 token，跟着主题走）"""
        colors = {
            '+': P["ok"],
            '-': P["err"],
            '@': P["accent"],
            ' ': P["fg_muted"],
        }
        spans = []
        for tag, line in diff:
            prefix = '' if tag == '@' else tag
            color = colors.get(tag, P["fg"])
            spans.append(
                f'<span style="color:{color};">{prefix}{html.escape(line)}</span>'
            )
        body = "\n".join(spans) if spans else "（无差异）"
        self.diff_view.setHtml(
            '<pre style="font-family:Consolas,monospace;font-size:10pt;margin:0;">'
            + body + '</pre>'
        )

    def export_diff(self):
        old_text = self.left_edit.toPlainText()
        new_text = self.right_edit.toPlainText()
        if not old_text.strip() and not new_text.strip():
            QMessageBox.information(self, "没有内容", "请先完成一次对比。")
            return

        default_name = "config_diff_%s.diff" % time.strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出差异", default_name, "Diff 文件 (*.diff);;文本文件 (*.txt)"
        )
        if not path:
            return

        session_name = getattr(self.session, 'name', '会话')
        content = to_unified(old_text, new_text,
                             fromfile=f"{session_name}-基线",
                             tofile=f"{session_name}-对比")
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content or "（无差异）\n")
            QMessageBox.information(self, "导出成功", f"已保存到:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))


def session_available(session) -> bool:
    """判断会话对象是否可用（存在且连接器还在）"""
    return session is not None and getattr(session, 'connector', None) is not None
