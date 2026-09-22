# -*- mode: python ; coding: utf-8 -*-
# AITerminal 打包配置（PyInstaller）
# 用法: pyinstaller build.spec
# 产物: dist/AITerminal.exe（单文件，双击即用）
import os

PROJECT = os.path.dirname(os.path.abspath(SPEC))  # 项目根目录

a = Analysis(
    [os.path.join(PROJECT, "main.py")],
    pathex=[PROJECT],
    binaries=[],
    datas=[
        (os.path.join(PROJECT, "resources"), "resources"),
    ],
    hiddenimports=[
        # GUI
        "PyQt6", "PyQt6.QtCore", "PyQt6.QtWidgets", "PyQt6.QtGui", "PyQt6.sip",
        # 连接与解析
        "paramiko", "pyserial", "serial", "pypdf",
        # paramiko 依赖
        "cryptography", "bcrypt", "nacl", "cffi",
    ],
    # 排除开发/测试类依赖，减小体积（保留 anthropic 以兼容 Anthropic 提供方）
    excludes=[
        "tests", "tools",       # 测试/调试脚本
        "PyInstaller", "setuptools", "pip", "pkg_resources",
        # 运行时用不到的重型依赖（Pillow 只在开发期生成图标时用过）
        "PIL", "Pillow",
        "tkinter", "numpy", "pandas", "matplotlib", "scipy",
        "IPython", "jupyter", "notebook", "pytest", "docutils",
        # 用不到的 Qt 子模块
        "PyQt6.QtPdf", "PyQt6.QtPdfWidgets",
        "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtQuickWidgets", "PyQt6.QtQuick3D",
        "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineQuick",
        "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
        "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtDesigner", "PyQt6.QtHelp",
        "PyQt6.QtBluetooth", "PyQt6.QtNfc", "PyQt6.QtPositioning", "PyQt6.QtLocation",
        "PyQt6.QtSensors", "PyQt6.QtSerialPort", "PyQt6.QtWebSockets",
        "PyQt6.QtWebChannel", "PyQt6.QtCharts", "PyQt6.QtDataVisualization",
        "PyQt6.Qt3DCore", "PyQt6.Qt3DRender", "PyQt6.Qt3DInput",
        "PyQt6.Qt3DLogic", "PyQt6.Qt3DAnimation", "PyQt6.Qt3DExtras",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

# ---------------------------------------------------------------- 剔除重型 Qt DLL
# PyQt6 的 hook 会把整个 Qt6/bin 都塞进来，其中这几个本项目用不到：
#   opengl32sw.dll  软件 OpenGL 回退（约 20MB）—— Qt Widgets 走光栅绘制，不请求 GL
#   Qt6Pdf.dll      约 4.5MB —— 我们用 Python 的 pypdf，不用 Qt 的 PDF 模块
# 其余按模块名匹配，避免误删 Qt6Core/Qt6Gui/Qt6Widgets/Qt6Svg（这些必需）。
_DROP_DLLS = (
    "opengl32sw",
    "qt6pdf", "qt6qml", "qt6quick", "qt6webengine", "qt6multimedia",
    "qt6sql", "qt6test", "qt6designer", "qt6help", "qt6bluetooth",
    "qt6nfc", "qt6positioning", "qt6location", "qt6sensors",
    "qt6serialport", "qt6websockets", "qt6webchannel", "qt6charts",
    "qt6datavisualization", "qt63d",
)
a.binaries = [
    b for b in a.binaries
    if not any(d in b[0].lower() for d in _DROP_DLLS)
]

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AITerminal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # 无控制台窗口（纯 GUI）
    windowed=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(PROJECT, "resources", "icons", "app.ico"),
)
