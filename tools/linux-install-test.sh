#!/usr/bin/env bash
# 安装脚本验收：交付布局 + 源码布局两种都要能装、能跑
set -uo pipefail
S="/mnt/c/Users/linyin/WorkBuddy AI/2026-09-21-18-54-37/aiterminal"
DEL="/mnt/d/ait_delivery/AITerminal/linux"
D="$HOME/ait-build/src"
FAIL=0

# 用最新 install.sh 刷新交付目录那份
cp "$S/packaging/install-linux-user.sh" "$DEL/install.sh"
sed -i 's/\r$//' "$DEL/install.sh"; chmod +x "$DEL/install.sh"

echo "=== A) 交付布局安装 ==="
rm -rf "$HOME/.local/share/aiterminal" "$HOME/.local/share/applications/aiterminal.desktop" \
       "$HOME/.local/share/icons/hicolor/256x256/apps/aiterminal.png"
cd "$DEL" && bash install.sh --no-knowledge 2>&1 | tail -6
echo "--- 落地文件 ---"
for f in "$HOME/.local/share/aiterminal/AITerminal" \
         "$HOME/.local/share/aiterminal/resources/icons/app.png" \
         "$HOME/.local/share/applications/aiterminal.desktop" \
         "$HOME/.local/share/icons/hicolor/256x256/apps/aiterminal.png"; do
  [ -e "$f" ] && echo "  ✅ $f" || { echo "  ❌ 缺 $f"; FAIL=1; }
done
echo "--- desktop Exec/Icon ---"
grep -E "^(Exec|Icon)=" "$HOME/.local/share/applications/aiterminal.desktop" || true

echo "=== 从安装位置启动（offscreen 8 秒）==="
QT_QPA_PLATFORM=offscreen timeout 8 "$HOME/.local/share/aiterminal/AITerminal" >/tmp/inst.log 2>&1
rc=$?
echo "  rc=$rc （124=存活正常）"
[ "$rc" -eq 124 ] || { echo "  ❌ 启动异常"; head -5 /tmp/inst.log; FAIL=1; }

echo "=== B) 源码布局安装（--prefix 到临时目录）==="
cd "$D" && bash "$S/packaging/install-linux-user.sh" --prefix /tmp/ait-inst --no-knowledge 2>&1 | tail -4
[ -x /tmp/ait-inst/AITerminal ] && echo "  ✅ /tmp/ait-inst/AITerminal" || { echo "  ❌ 源码布局安装失败"; FAIL=1; }

echo
[ "$FAIL" -eq 0 ] && echo "总结: 安装脚本 PASS" || echo "总结: 安装脚本 FAIL"
exit $FAIL
