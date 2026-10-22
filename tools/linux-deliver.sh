#!/usr/bin/env bash
# 把 Linux 构建产物归档到交付目录 D:\ait_delivery\NetOpsTerminal\linux
set -euo pipefail
D="$HOME/ait-build/src"
S="/mnt/c/Users/linyin/WorkBuddy AI/2026-09-21-18-54-37/aiterminal"
OUT="/mnt/d/ait_delivery/NetOpsTerminal/linux"

rm -rf "$OUT"; mkdir -p "$OUT"

install -m 0755 "$D/dist/NetOpsTerminal" "$OUT/NetOpsTerminal"
cp "$S/packaging/netops-terminal.desktop" "$OUT/netops-terminal.desktop"
cp "$S/packaging/install-linux-user.sh" "$OUT/install.sh"
cp "$D/resources/icons/app.png" "$OUT/app.png"
cp "$S/README-Linux.md" "$OUT/README-Linux.md"

sed -i 's/\r$//' "$OUT/install.sh" "$OUT/netops-terminal.desktop"
chmod +x "$OUT/install.sh"

cd "$OUT"
sha256sum NetOpsTerminal > SHA256SUMS.txt
echo "=== 交付目录 ==="
ls -l | awk 'NR>1 {printf "  %-22s %10d B\n", $9, $5}'
echo
echo "二进制:"
file NetOpsTerminal | cut -c1-120
sha256sum NetOpsTerminal
echo "ldd 缺失依赖: $(ldd NetOpsTerminal 2>/dev/null | grep -c 'not found' || true)"
