# -*- coding: utf-8 -*-
"""窗口小工具：把对话框摆到屏幕正中间。

为什么单独抽出来：以前各对话框只 `resize()` 不 `move()`，位置由窗口管理器决定，
多屏时经常半个窗口在屏幕外（用户反馈"打开资料库后的界面位置需要调整，居中即可"）。
"""
from PyQt6.QtGui import QGuiApplication


def center_on_screen(widget, parent=None):
    """把窗口挪到它所在屏幕的可用区正中间；比可用区还大时先缩到可用区以内。

    为什么不用 primaryScreen()：多屏时主屏未必是用户正在看的那块。有父窗口就
    跟着父窗口走，没有就按窗口当前所在的屏（再退到主屏）。
    """
    screen = None
    handle = parent.windowHandle() if parent is not None else None
    if handle is not None:
        screen = handle.screen()
    if screen is None:
        screen = QGuiApplication.screenAt(widget.frameGeometry().center())
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return
    avail = screen.availableGeometry()
    width = min(widget.width(), avail.width())
    height = min(widget.height(), avail.height())
    if (width, height) != (widget.width(), widget.height()):
        widget.resize(width, height)
    widget.move(avail.x() + (avail.width() - width) // 2,
                avail.y() + (avail.height() - height) // 2)
