# -*- mode: python ; coding: utf-8 -*-
# AITerminal 打包配置（PyInstaller · Linux）
# 用法: pyinstaller AITerminal-linux.spec
# 产物: dist/AITerminal（单文件 ELF，无控制台）
#
# 与 build.spec（Windows）的差异仅三处，其余保持逐行一致，避免两套配置漂移：
#   1. icon=None        —— Linux 下 PyInstaller 不接受 .ico 做可执行文件图标，
#                          图标由 packaging/aiterminal.desktop + app.png 提供；
#   2. upx=False        —— 大多数发行版没装 upx，开着只会刷告警；
#   3. console/windowed —— Linux 没有独立控制台概念，保留 windowed=True 让
#                          stdout 不抢终端（--script 模式靠退出码/落盘结果核对）。
import os

PROJECT = os.path.dirname(os.path.abspath(SPEC))  # 项目根目录

# ---------------------------------------------------------------- 随包脚本（datas）
# 为什么这些 .py 必须**以真实文件**进包（而不是只打进 PYZ）：
#   · `main.py --script <脚本>` 用 `runpy.run_path` 跑它们 —— runpy 要的是**文件**；
#   · `core/local_model.probe()` 会**进程内 import** probe_tier（打包态是 windowed，
#     子进程 stdout 拿不到，只能进程内调用），import 也要有真实文件在位；
#   · `core/local_rag.py` 把 local_llm 目录插进 sys.path 后 `import retrieve`（PyInstaller
#     静态分析看不到这种导入，必须 datas 兜住）。
# `knowledge/`（约 5GB 切片与向量库）**不进包**：按交付约定放在可执行文件同目录，
# 由 `core/app_paths.data_dir()` 解析。
_LOCAL_LLM_FILES = (
    "paths.py", "probe_tier.py", "run_server.py", "verify_delivery.py", "retrieve.py",
    "embed_chunks.py",
    "prompt_local_assistant.txt",
)
_LOCAL_LLM_DATA = [
    (os.path.join(PROJECT, "local_llm", name), "local_llm")
    for name in _LOCAL_LLM_FILES
    if os.path.exists(os.path.join(PROJECT, "local_llm", name))
]

a = Analysis(
    [os.path.join(PROJECT, "main.py")],
    pathex=[PROJECT, os.path.join(PROJECT, "local_llm")],
    binaries=[],
    datas=[
        (os.path.join(PROJECT, "resources"), "resources"),
    ] + _LOCAL_LLM_DATA,
    hiddenimports=[
        # GUI
        "PyQt6", "PyQt6.QtCore", "PyQt6.QtWidgets", "PyQt6.QtGui", "PyQt6.sip",
        # 连接与解析（只写模块名：pyserial 的模块名是 serial）
        "paramiko", "serial", "pypdf",
        # paramiko 依赖
        "cryptography", "bcrypt", "nacl", "cffi",
        # 向量检索：local_llm/retrieve.py 在函数体内惰性 import numpy。
        # 绝不能排掉，否则 RAG 静默退回词法检索（能用但变笨）。
        "numpy",
    ],
    # 排除开发/测试类依赖，减小体积
    excludes=[
        "tests", "tools",
        "PyInstaller", "setuptools", "pip", "pkg_resources",
        "PIL", "Pillow",
        "tkinter", "pandas", "matplotlib", "scipy",
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

# ---------------------------------------------------------------- 剔除重型 Qt 库
# PyQt6 的 hook 会把整个 Qt6/lib 都塞进来，其中这些本项目用不到：
#   libQt6Pdf / libQt6Qml / libQt6Quick / libQt6WebEngine* / libQt6Multimedia …
# 其余按名字匹配，避免误删 Qt6Core/Qt6Gui/Qt6Widgets/Qt6Svg（这些必需）。
_DROP_LIBS = (
    "opengl32sw",
    "qt6pdf", "qt6qml", "qt6quick", "qt6webengine", "qt6multimedia",
    "qt6sql", "qt6test", "qt6designer", "qt6help", "qt6bluetooth",
    "qt6nfc", "qt6positioning", "qt6location", "qt6sensors",
    "qt6serialport", "qt6websockets", "qt6webchannel", "qt6charts",
    "qt6datavisualization", "qt63d",
)
a.binaries = [
    b for b in a.binaries
    if not any(d in b[0].lower() for d in _DROP_LIBS)
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
    upx=False,               # Linux 发行版多无 upx，开了只会刷告警
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    windowed=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,               # 图标由 .desktop + app.png 提供
)
