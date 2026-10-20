#!/usr/bin/env bash
# 把源码（不含 5GB knowledge）复制进 WSL 的构建目录
set -euo pipefail

SRC="/mnt/c/Users/linyin/WorkBuddy AI/2026-09-21-18-54-37/aiterminal"
DST="${HOME}/ait-build/src"

rm -rf "$DST"
mkdir -p "$DST"
cd "$SRC"

tar -cf - \
    --exclude=knowledge \
    --exclude=.venv-train \
    --exclude=.venv \
    --exclude=dist \
    --exclude=build \
    --exclude=__pycache__ \
    --exclude='*.pyc' \
    --exclude=.git \
    --exclude='*.log' \
    . 2>/dev/null | (cd "$DST" && tar -xf -)

echo "=== 复制完成 ==="
du -sh "$DST"
echo "=== 顶层结构 ==="
ls -1 "$DST" | head -30
echo "=== py 文件数 / 行数 ==="
find "$DST" -name '*.py' | wc -l
find "$DST" -name '*.py' -exec cat {} + | wc -l
echo "=== resources ==="
du -sh "$DST/resources" 2>/dev/null || true
ls "$DST/resources" 2>/dev/null | head
