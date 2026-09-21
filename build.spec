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
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

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
