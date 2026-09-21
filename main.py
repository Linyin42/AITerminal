#!/usr/bin/env python3
"""
AITerminal - AI智能终端
主程序入口
"""
import os
import sys
import traceback

# 添加项目根目录到路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402

from ui.main_window import MainWindow  # noqa: E402


def _excepthook(exc_type, exc_value, exc_tb):
    """把未捕获异常显示为对话框，避免界面直接退出"""
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(text, file=sys.stderr)
    try:
        QMessageBox.critical(None, "程序异常", text[-1500:])
    except Exception:
        pass


def main():
    """主函数"""
    # 启用高DPI支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("AITerminal")
    app.setApplicationVersion("0.7.0")
    app.setOrganizationName("AITerminal Team")

    sys.excepthook = _excepthook

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
