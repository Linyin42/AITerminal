"""
AITerminal 打包配置

开发安装:
    pip install -e .

打包（生成单文件 exe，需先安装 pyinstaller）:
    python setup.py build_exe
"""
import os
import sys
from setuptools import setup, find_packages, Command

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def read_requirements():
    req_file = os.path.join(BASE_DIR, "requirements.txt")
    if not os.path.exists(req_file):
        return []
    with open(req_file, 'r', encoding='utf-8') as f:
        return [
            line.strip() for line in f
            if line.strip() and not line.startswith('#')
        ]


APP = ['main.py']
DATA_FILES = [
    ('resources/styles', ['resources/styles/dark.qss']),
]


class BuildExeCommand(Command):
    """使用 PyInstaller 打包为单文件可执行程序"""

    description = "使用 PyInstaller 打包 AITerminal"
    user_options = [
        ('onefile', None, '打包为单文件'),
        ('console', None, '保留控制台窗口'),
    ]

    def initialize_options(self):
        self.onefile = False
        self.console = False

    def finalize_options(self):
        pass

    def run(self):
        try:
            import PyInstaller.__main__ as pyi
        except ImportError:
            print("请先安装 PyInstaller: pip install pyinstaller")
            sys.exit(1)

        sep = ';' if sys.platform.startswith('win') else ':'
        args = [
            'main.py',
            '--name=AITerminal',
            '--noconfirm',
            '--clean',
            f'--add-data=resources/styles/dark.qss{sep}resources/styles',
            '--hidden-import=paramiko',
            '--hidden-import=serial',
            '--hidden-import=anthropic',
            '--hidden-import=pypdf',
        ]
        if self.onefile:
            args.append('--onefile')
        if not self.console:
            args.append('--windowed')
        pyi.run(args)


setup(
    name="aiterminal",
    version="0.7.0",
    description="AITerminal - 集成 AI 辅助功能的网络设备终端工具",
    author="AITerminal Team",
    license="MIT",
    packages=find_packages(exclude=("tests", "docs")),
    py_modules=["main"],
    install_requires=read_requirements(),
    python_requires=">=3.9",
    entry_points={
        "console_scripts": [
            "aiterminal=main:main",
        ],
    },
    cmdclass={"build_exe": BuildExeCommand},
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: X11 Applications :: Qt",
        "Intended Audience :: Telecommunications Industry",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Topic :: System :: Networking",
    ],
)
