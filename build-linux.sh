#!/usr/bin/env bash
# AITerminal Linux 打包脚本
#
# 用法:
#   bash build-linux.sh              # 在源码根目录构建 → dist/AITerminal
#   bash build-linux.sh --clean      # 先删 build/ dist/ 再构建
#
# 前置（一次性）:
#   uv python install 3.12
#   uv venv --python 3.12 .venv-linux
#   uv pip install --python .venv-linux/bin/python PyQt6 paramiko pyserial \
#       anthropic python-dotenv pypdf numpy pyinstaller
#
# 产物是**单文件** ELF；knowledge/（约 5GB）按约定不进包，放可执行文件同目录。
set -euo pipefail

cd "$(dirname "$0")"

PY="${PY:-.venv-linux/bin/python}"
if [ ! -x "$PY" ]; then
    echo "找不到构建用 Python: $PY" >&2
    echo "先建环境: uv venv --python 3.12 .venv-linux && uv pip install --python .venv-linux/bin/python -r requirements-linux.txt" >&2
    exit 2
fi

if [ "${1:-}" = "--clean" ]; then
    echo "== 清理 build/ dist/ =="
    rm -rf build dist
fi

echo "== Python: $("$PY" -V) =="
echo "== PyInstaller: $("$PY" -m PyInstaller --version) =="

START=$(date +%s)
"$PY" -m PyInstaller --noconfirm --clean AITerminal-linux.spec
END=$(date +%s)

BIN="dist/AITerminal"
if [ ! -f "$BIN" ]; then
    echo "构建失败：$BIN 不存在" >&2
    exit 1
fi
chmod +x "$BIN"

echo
echo "== 完成 =="
echo "产物: $BIN"
echo "大小: $(du -h "$BIN" | cut -f1)"
echo "SHA256: $(sha256sum "$BIN" | cut -d' ' -f1)"
echo "耗时: $((END-START)) 秒"
