#!/usr/bin/env bash
# 依赖体检 + 按缺失清单补齐（Linux 构建机）
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
S="/mnt/c/Users/linyin/WorkBuddy AI/2026-09-21-18-54-37/aiterminal"
D="$HOME/ait-build/src"
VENV="$HOME/ait-build/.venv"

cp "$S/tools/deps_check.py" "$D/tools/deps_check.py"
sed -i 's/\r$//' "$D/tools/deps_check.py"
cd "$D"
echo "=== 依赖体检 ==="
"$VENV/bin/python" tools/deps_check.py
