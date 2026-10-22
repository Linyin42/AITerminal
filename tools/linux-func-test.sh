#!/usr/bin/env bash
# Linux 功能验证：主窗口离屏截图 + 危险命令矩阵
set -uo pipefail
S="/mnt/c/Users/linyin/WorkBuddy AI/2026-09-21-18-54-37/aiterminal"
D="$HOME/ait-build/src"
cp "$S/tools/linux_ui_shot.py" "$D/tools/"
sed -i 's/\r$//' "$D/tools/linux_ui_shot.py"
[ -f "$S/tools/check_danger_fix.py" ] && cp "$S/tools/check_danger_fix.py" "$D/tools/" && sed -i 's/\r$//' "$D/tools/check_danger_fix.py"
cd "$D"

echo "=== 1) 主窗口离屏截图 ==="
QT_QPA_PLATFORM=offscreen ./dist/NetOpsTerminal --script tools/linux_ui_shot.py /tmp/ait-linux-ui.png
echo "rc=$?"
ls -l /tmp/ait-linux-ui.png 2>/dev/null | awk '{print "  截图字节:", $5}'

echo "=== 2) 危险命令矩阵（冻结包内）==="
if [ -f tools/check_danger_fix.py ]; then
  QT_QPA_PLATFORM=offscreen ./dist/NetOpsTerminal --script tools/check_danger_fix.py /tmp/danger-linux.txt
  echo "rc=$?"
  head -25 /tmp/danger-linux.txt 2>/dev/null
else
  echo "  (没有 check_danger_fix.py，跳过)"
fi
