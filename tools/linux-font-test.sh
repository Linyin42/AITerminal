#!/usr/bin/env bash
# 验证"缺 CJK 字体导致方块字"：从 Windows 借字体装到用户目录（测试用，不随包分发）
set -uo pipefail
D="$HOME/ait-build/src"
OUT="/mnt/d/ait_delivery/NetOpsTerminal/复验-2026-10-20"
FONT_DIR="$HOME/.local/share/fonts"

echo "=== 0) 现在有多少中文字体 ==="
echo "fc-list zh: $(fc-list :lang=zh 2>/dev/null | wc -l) 个"

echo "=== 1) 从 Windows 借字体到 ~/.local/share/fonts ==="
mkdir -p "$FONT_DIR"
for f in msyh.ttc msyhbd.ttc simsun.ttc Deng.ttf simhei.ttf; do
  src="/mnt/c/Windows/Fonts/$f"
  if [ -f "$src" ]; then
    cp -n "$src" "$FONT_DIR/" 2>/dev/null && echo "  装了 $f"
  fi
done
fc-cache -f "$FONT_DIR" >/dev/null 2>&1
echo "现在 fc-list zh: $(fc-list :lang=zh 2>/dev/null | wc -l) 个"
fc-list :lang=zh family 2>/dev/null | sort -u | head -5

echo "=== 2) 重新渲染（wayland）==="
cd "$D"
QT_QPA_PLATFORM=wayland timeout 60 ./dist/NetOpsTerminal --script tools/linux_ui_shot.py /tmp/ait-wayland-cjk.png 2>&1 | tail -16
if [ -f /tmp/ait-wayland-cjk.png ]; then
  cp /tmp/ait-wayland-cjk.png "$OUT/Linux-主窗口-wayland-中文字体.png"
  ls -l /tmp/ait-wayland-cjk.png | awk '{print "  截图字节:", $5}'
  echo "已回传: $OUT/Linux-主窗口-wayland-中文字体.png"
fi
