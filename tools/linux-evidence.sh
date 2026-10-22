#!/usr/bin/env bash
# Linux 证据收集：截图回传 + 危险命令矩阵 + xcb 平台探测
set -uo pipefail
S="/mnt/c/Users/linyin/WorkBuddy AI/2026-09-21-18-54-37/aiterminal"
D="$HOME/ait-build/src"
OUT="/mnt/d/ait_delivery/NetOpsTerminal/复验-2026-10-20"
PROBE="/mnt/d/ait_delivery/NetOpsTerminal/_probe"
cd "$D"

mkdir -p "$OUT"
cp /tmp/ait-linux-ui.png "$OUT/Linux-主窗口-offscreen.png" 2>/dev/null && echo "截图已回传: $OUT/Linux-主窗口-offscreen.png"

if [ -f "$PROBE/check_danger_fix.py" ]; then
  cp "$PROBE/check_danger_fix.py" "$D/tools/check_danger_fix.py"
  sed -i 's/\r$//' "$D/tools/check_danger_fix.py"
  echo "=== 危险命令矩阵 ==="
  QT_QPA_PLATFORM=offscreen ./dist/NetOpsTerminal --script tools/check_danger_fix.py /tmp/danger-linux.txt
  echo "rc=$?"
  tail -20 /tmp/danger-linux.txt 2>/dev/null
else
  echo "(找不到 $PROBE/check_danger_fix.py)"
fi

echo "=== xcb 平台探测（真实桌面用的插件）==="
echo "DISPLAY=${DISPLAY:-<未设置>}  WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-<未设置>}"
if [ -n "${DISPLAY:-}" ]; then
  QT_QPA_PLATFORM=xcb timeout 40 ./dist/NetOpsTerminal --script tools/linux_ui_shot.py /tmp/ait-xcb.png 2>&1 | head -25
  echo "rc=$?"
  [ -f /tmp/ait-xcb.png ] && cp /tmp/ait-xcb.png "$OUT/Linux-主窗口-xcb.png" && echo "xcb 截图已回传"
else
  echo "WSL 没有 DISPLAY，跳过（WSLg 未启用）"
fi
