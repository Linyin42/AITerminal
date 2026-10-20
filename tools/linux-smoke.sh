#!/usr/bin/env bash
# Linux 冻结包冒烟：把 tools/smoke_linux.py 同步进构建目录并跑一遍
set -uo pipefail
S="/mnt/c/Users/linyin/WorkBuddy AI/2026-09-21-18-54-37/aiterminal"
D="$HOME/ait-build/src"
mkdir -p "$D/tools"
cp "$S/tools/smoke_linux.py" "$D/tools/smoke_linux.py"
sed -i 's/\r$//' "$D/tools/smoke_linux.py"
cp "$S/AITerminal-linux.spec" "$D/AITerminal-linux.spec" 2>/dev/null || true
cd "$D"

echo "=== 1) --script 冒烟（offscreen）==="
QT_QPA_PLATFORM=offscreen ./dist/AITerminal --script tools/smoke_linux.py /tmp/smoke-linux.json
rc=$?
echo "rc=$rc"

echo "=== 2) 结果文件 ==="
head -40 /tmp/smoke-linux.json 2>/dev/null || echo "(没有结果文件)"

echo "=== 3) 无 DISPLAY 直接启动（应能在 offscreen 下存活）==="
QT_QPA_PLATFORM=offscreen timeout 12 ./dist/AITerminal > /tmp/ait-run.log 2>&1
echo "退出码=$? （124=存活到超时，正常）"
echo "--- 启动日志 ---"
head -15 /tmp/ait-run.log
