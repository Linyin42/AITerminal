# -*- mode: python ; coding: utf-8 -*-
# NetOpsTerminal 打包配置（PyInstaller）
# 用法: pyinstaller build.spec
# 产物: dist/NetOpsTerminal.exe（单文件，双击即用）
import os

PROJECT = os.path.dirname(os.path.abspath(SPEC))  # 项目根目录

# ---------------------------------------------------------------- 随包脚本（datas）
# 为什么这些 .py 必须**以真实文件**进包（而不是只打进 PYZ）：
#   · `main.py --script <脚本>` 用 `runpy.run_path` 跑它们 —— runpy 要的是**文件**；
#   · `core/local_model.probe()` 会**进程内 import** probe_tier（打包态 exe 是 windowed，
#     子进程 stdout 拿不到，只能进程内调用），import 也要有真实文件在位；
#   · `core/local_rag.py` 把 local_llm 目录插进 sys.path 后 `import retrieve`（PyInstaller
#     静态分析看不到这种导入，必须 datas 兜住）；
#   · 命令行一律由 `core/app_paths.script_cmd()` 拼，见 tests/test_frozen_scripts.py。
# `knowledge/`（约 5GB 切片与向量库）与 `_vendor_cmd/`（约 30MB）**不进包**：
# 按交付约定放在 **exe 同目录**，由 `core/app_paths.data_dir()` 解析（P5-1 已收口）。
_LOCAL_LLM_FILES = (
    "paths.py", "probe_tier.py", "run_server.py", "verify_delivery.py", "retrieve.py",
    # retrieve.py 模块级 `import embed_chunks`（复用嵌入/归一化）⇒ 它也得在位，
    # 否则打包态下 core/local_rag 的 `import retrieve` 直接 ImportError → 静默退回词法层。
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
        # 连接与解析
        # ⚠ 只写**模块名**：`pyserial` 是发行包名、模块名是 `serial`。写成
        # `pyserial` 每次构建都刷一条 "Hidden import 'pyserial' not found" 假错误，
        # 真错误会被这行噪音埋掉（2026-10-17 顺手清掉）。
        "paramiko", "serial", "pypdf",
        # paramiko 依赖
        "cryptography", "bcrypt", "nacl", "cffi",
        # 向量检索：local_llm/retrieve.py 在函数体内惰性 import numpy。
        # ⚠ **绝不能**把 numpy 排掉：排掉后打包态 RAG 抛异常，而 core/local_rag.py
        # 会把异常吞掉并**静默**退回词法检索 —— 表现是"能用但变笨"，
        # 比直接报错难查得多（2026-10-17 由 P5-2 审查发现）。
        "numpy",
    ],
    # 排除开发/测试类依赖，减小体积（保留 anthropic 以兼容 Anthropic 提供方）
    excludes=[
        "tests", "tools",       # 测试/调试脚本
        "PyInstaller", "setuptools", "pip", "pkg_resources",
        # 运行时用不到的重型依赖（Pillow 只在开发期生成图标时用过）
        "PIL", "Pillow",
        # 注意：numpy **不在这里**（向量检索要用，见上面 hiddenimports）
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
    name="NetOpsTerminal",
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
