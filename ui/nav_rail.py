"""
左侧工具窗口条 —— 像 IDE 那样只有图标的一条窄边

PyCharm 最显眼的结构就是这个：最左边一条 40px 宽的竖条，放各工具窗口的图标，
点一下开/关对应面板。这里放三个最常用的：设备库 / AI 助手 / 资料库。

为什么用 QToolBar 而不是自定义控件：
    Qt 的工具条天生支持"竖排 + 图标 + 选中态 + 吸附在窗口边缘"，
    也不用自己处理布局与重绘。图标用字符（▤ ✦ ▦）而非图片，
    省资源、不挑字体，和项目里其它按钮的写法一致。

（这个文件之前是一份 150px 宽、带文字标签的导航条，但从未被主窗口接上，
属于死代码；现在改成 PyCharm 那种图标条并真正启用。）
"""
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QToolBar

#: (key, 提示, 图标字符, 是否是"开关型"面板)
RAIL_ITEMS = (
    ("devices", "设备库", "▤", True),
    ("ai", "AI 助手面板", "✦", True),
    ("knowledge", "资料库…", "▦", False),
)


class NavRail(QToolBar):
    """工具窗口条（图标按钮竖排）"""

    item_selected = pyqtSignal(str)   # 参数为 key

    def __init__(self, parent=None):
        super().__init__("工具窗口", parent)
        self.setObjectName("NavRail")
        self.setMovable(False)
        self.setFloatable(False)
        self.setOrientation(Qt.Orientation.Vertical)
        self.setIconSize(QSize(16, 16))
        self._actions = {}
        for key, tip, glyph, toggle in RAIL_ITEMS:
            action = QAction(glyph, self)
            action.setToolTip(tip)
            action.setCheckable(toggle)
            action.triggered.connect(lambda _=False, k=key: self.item_selected.emit(k))
            self.addAction(action)
            self._actions[key] = action

    def set_checked(self, key: str, checked: bool):
        """同步某个图标的选中态（不触发信号，避免来回递归）"""
        action = self._actions.get(key)
        if action is None or not action.isCheckable():
            return
        if action.isChecked() == bool(checked):
            return
        action.blockSignals(True)
        action.setChecked(bool(checked))
        action.blockSignals(False)

    def is_checked(self, key: str) -> bool:
        action = self._actions.get(key)
        return bool(action is not None and action.isChecked())
