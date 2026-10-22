#!/usr/bin/env bash
# 真实合成器（WSLg）下渲染验证：先查缺哪些 xcb 库，再试 wayland 平台
set -uo pipefail
D="$HOME/ait-build/src"
OUT="/mnt/d/ait_delivery/NetOpsTerminal/复验-2026-10-20"
cd "$D"

echo "=== 1) xcb 相关库有没有 ==="
for lib in libxcb-cursor libxcb-xinerama libxcb-icccm libxcb-image libxcb-keysyms \
           libxcb-render-util libxkbcommon-x11 libxkbcommon libegl libGL libfontconfig; do
  if ldconfig -p 2>/dev/null | grep -q "$lib"; then echo "  ✅ $lib"; else echo "  ❌ $lib"; fi
done

echo "=== 2) wayland 平台渲染 ==="
QT_QPA_PLATFORM=wayland timeout 60 ./dist/NetOpsTerminal --script tools/linux_ui_shot.py /tmp/ait-wayland.png 2>&1 | head -20
rc=${PIPESTATUS[0]}
echo "rc=$rc"
if [ -f /tmp/ait-wayland.png ]; then
  cp /tmp/ait-wayland.png "$OUT/Linux-主窗口-wayland.png"
  ls -l /tmp/ait-wayland.png | awk '{print "  截图字节:", $5}'
  echo "已回传: $OUT/Linux-主窗口-wayland.png"
fi

echo "=== 3) 真实启动（wayland，跑 8 秒）==="
QT_QPA_PLATFORM=wayland timeout 8 ./dist/NetOpsTerminal > /tmp/ait-wayland-run.log 2>&1
echo "退出码=$? (124=存活)"
head -6 /tmp/ait-wayland-run.log
