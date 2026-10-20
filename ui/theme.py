"""
统一主题（配色 token）—— One Dark Pro

样式表本体放在 resources/styles/dark.qss（可由用户直接改），
这个模块只负责两件事：
    1. 给 Python 侧自绘的控件（设备行、终端、对话气泡…）提供颜色 token，
       避免颜色散落在各个文件里各写各的
    2. 提供一份最小兜底样式：万一 qss 文件丢了，程序还不至于变成白底黑字

配色演进（都在这个字典里，换配色只改这里 + dark.qss）：
    一代：靛蓝 Indigo（仿 Netcatty，圆角大、偏紫）
    二代：经典 Darcula（#2b2b2b / #3c3f41，灰调但偏浅）
    三代：新版 PyCharm（底色更深、描边更弱，靠明度分层）
    四代：One Dark Pro —— 工具窗口比编辑区更暗（IDE 的层次），
          强调色由钢蓝换成天蓝 #61afef，语义色用 One Dark 原生的绿/黄/红
    五代：效果图那套（本文件）—— 深色三层（#0E1117 画布 / #12171F 工具窗口 /
          #151A23 卡片输入）+ 1px 白 8% 描边 + 单一强调色 #4C8DFF；
          语义色绿 #3FB950 / 琥珀 #D29922 / 红 #F85149，终端文字 #CFD8E6。
          四代之前的散落字面量已用 tools/retint_theme.py 按 LEGACY_MAP 全量扫过。
"""

# ---------------------------------------------------------------- 调色板
P = {
    # 背景层次（由暗到亮）：画布/终端 → 工具窗口 → 卡片/输入 → 悬停 → 选中
    "bg":           "#0E1117",   # 画布 / 终端（编辑区）
    "bg_panel":     "#12171F",   # 工具窗口（设备库、AI 面板、菜单栏、状态栏）
    "bg_sunken":    "#151A23",   # 页签条、卡片、表头、下沉区块
    "bg_input":     "#151A23",   # 输入框 / 下拉框
    "bg_hover":     "#182030",   # 悬停
    "bg_active":    "#1C2A42",   # 选中（accent 14% 叠在面板上）

    # 描边：效果图用 1px rgba(255,255,255,.08)（dark.qss 里就是 rgba），
    # 这里给等效实色，方便 QColor/自绘代码直接用
    "border":       "#242931",   # 白 8%
    "border_soft":  "#33373E",   # 白 14%
    "border_focus": "#4C8DFF",

    # 文字
    "fg":           "#E6EAF2",
    "fg_strong":    "#FFFFFF",
    "fg_muted":     "#93A0B4",
    "fg_disabled":  "#6B7684",

    # 强调与语义色
    "accent":       "#4C8DFF",
    "accent_hover": "#3F7DE8",
    "accent_fg":    "#9CC2FF",   # 淡蓝底上的强调文字
    "ok":           "#3FB950",
    "ok_fg":        "#5FD47A",
    "warn":         "#D29922",
    "warn_fg":      "#E3B341",
    "err":          "#F85149",

    # 终端
    "term_fg":      "#CFD8E6",
    "term_sel":     "#1C2A42",
    "term_prompt":  "#7FD18A",
    "term_cmd":     "#8AB4FF",
}

#: 历史配色 → 当前配色。一/二/三代用过的颜色都在里面，
#: 万一还有哪个控件写死了老颜色，用这张表一查就知道该换成什么。
LEGACY_MAP = {
    # ---- 四代：One Dark Pro（本代的上一代；散落字面量已按此表全量替换）----
    "#282c34": P["bg"], "#21252b": P["bg_panel"], "#2c313a": P["bg_sunken"],
    "#3a3f4b": P["bg_hover"], "#3e4451": P["bg_active"], "#262b33": P["bg_panel"],
    "#4b5263": P["border_soft"], "#61afef": P["accent"], "#7cc0f5": P["accent"],
    "#4d95d0": P["accent_hover"],
    "#abb2bf": P["fg"], "#d7dae0": P["fg_strong"], "#7f848e": P["fg_muted"],
    "#5c6370": P["fg_disabled"], "#c678dd": P["accent_fg"],
    "#98c379": P["ok"], "#e5c07b": P["warn"], "#e06c75": P["err"],
    "#56b6c2": P["accent"],
    # ---- 三代：新版 PyCharm ----
    "#1e1f22": P["bg"], "#2b2d30": P["bg_panel"], "#26282c": P["bg_sunken"],
    "#393b40": P["border"], "#43454a": P["border_soft"], "#333539": P["bg_hover"],
    "#2e436e": P["bg_active"],
    "#4a88c7": P["accent"], "#5b9bd5": P["accent_hover"], "#6ba3d6": P["accent_fg"],
    "#3d76ad": P["accent_hover"],
    "#dfe1e5": P["fg"], "#9da0a8": P["fg_muted"], "#6f737a": P["fg_disabled"],
    "#57965c": P["ok"], "#d5b778": P["warn"], "#db5c5c": P["err"],
    "#bcbec4": P["term_fg"], "#4e5157": P["border_soft"], "#5a5d63": P["fg_disabled"],
    # ---- 二代：经典 Darcula ----
    "#2b2b2b": P["bg"], "#3c3f41": P["bg_panel"], "#313335": P["bg_sunken"],
    "#323232": P["border"], "#3f4143": P["border_soft"],
    "#45494a": P["bg_input"], "#4c5052": P["bg_hover"], "#5a5e60": P["border_soft"],
    "#214283": P["bg_active"],
    "#bbbbbb": P["fg"], "#e8e8e8": P["fg_strong"], "#808080": P["fg_muted"],
    "#5f6164": P["fg_disabled"],
    "#a9b7c6": P["term_fg"], "#4b4b4b": P["border_soft"], "#5f5f5f": P["fg_disabled"],
    "#466d94": P["border_focus"],
    "#499c54": P["ok"], "#e05454": P["err"], "#d9a343": P["warn"],
    "#6e7175": P["fg_disabled"], "#c7d5e6": "#c8ccd4",
    "#ffa657": "#d19a66", "#79c0ff": P["accent"], "#f778ba": "#c678dd",
    "#ce9178": "#d19a66",
    # ---- 一代：靛蓝 Indigo（仿 Netcatty）----
    "#12141c": P["bg"], "#14161f": P["bg"], "#111318": P["bg"],
    "#171a24": P["bg_panel"], "#1a1e29": P["bg_sunken"], "#1e222f": P["bg_sunken"],
    "#242938": P["bg_hover"], "#262b3a": P["bg_hover"], "#1c202c": P["bg_sunken"],
    "#2b3143": P["border"], "#39415a": P["border_soft"], "#232a45": P["bg_active"],
    "#e6e9f2": P["fg"], "#c5cddd": P["fg"], "#98a1b5": P["fg_muted"],
    "#7c8cf8": P["accent"], "#98a5ff": P["accent_fg"], "#a5b1ff": P["accent_fg"],
    "#5f6fe8": P["accent_hover"], "#2b3450": P["bg_active"],
    "#3fb950": P["ok"], "#f85149": P["err"], "#d29922": P["warn"],
    "#22d3ee": P["accent"], "#666e82": P["fg_disabled"], "#565e70": P["fg_disabled"],
    "#8b93a7": P["fg_muted"], "#7d8598": P["fg_muted"],
}


#: 兜底样式：只保证"层次和可读性"，精细样式以 dark.qss 为准
QSS = f"""
QMainWindow, QDialog {{ background-color: {P['bg']}; }}
QWidget {{ color: {P['fg']}; }}
QMenuBar, QStatusBar, QToolBar {{ background: {P['bg_panel']}; color: {P['fg']}; }}
QMenu {{ background: {P['bg_sunken']}; border: 1px solid {P['border']}; }}
QMenu::item:selected {{ background: {P['bg_active']}; color: {P['accent']}; }}
QTabBar {{ background: {P['bg_panel']}; }}
QTabBar::tab {{ background: {P['bg_panel']}; color: {P['fg_muted']};
    padding: 5px 12px; border-right: 1px solid {P['border']};
    border-top: 2px solid transparent; }}
QTabBar::tab:selected {{ background: {P['bg']}; color: {P['fg_strong']};
    border-top: 2px solid {P['accent']}; }}
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit, QListWidget {{
    background: {P['bg_input']}; color: {P['fg']};
    border: 1px solid {P['border']}; padding: 4px 6px;
    selection-background-color: {P['bg_active']}; selection-color: {P['fg_strong']}; }}
QTextEdit, QPlainTextEdit {{ background: {P['bg']}; color: {P['term_fg']}; }}
QPushButton {{ background: {P['bg_sunken']}; color: {P['fg']};
    border: 1px solid {P['border']}; padding: 6px 14px; border-radius: 6px; }}
QPushButton:hover {{ background: {P['bg_hover']}; }}
QGroupBox {{ background: {P['bg_sunken']}; border: none; border-radius: 8px;
    margin-top: 26px; padding: 16px; }}
QGroupBox::title {{ color: {P['fg_muted']}; background: transparent;
    padding: 0 0 10px 0; font-size: 11px; }}
QToolTip {{ background: {P['bg_sunken']}; color: {P['fg']};
    border: 1px solid {P['border']}; padding: 4px 7px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle {{ background: {P['border_soft']}; min-height: 24px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""
