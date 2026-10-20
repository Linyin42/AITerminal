#!/usr/bin/env bash
# WSL 侧一键构建：同步源码 → 装 numpy → PyInstaller → 校验产物
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

WIN_SRC="/mnt/c/Users/linyin/WorkBuddy AI/2026-09-21-18-54-37/aiterminal"
BUILD_ROOT="$HOME/ait-build"
SRC="$BUILD_ROOT/src"
VENV="$BUILD_ROOT/.venv"
LOG="/tmp/ait-linux-build.log"

echo "== [1/4] 同步源码 =="
rm -rf "$SRC"; mkdir -p "$SRC"
cd "$WIN_SRC"
tar -cf - \
    --exclude=knowledge --exclude=.venv-train --exclude=.venv --exclude=.venv-linux \
    --exclude=dist --exclude=build --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=.git --exclude='*.log' . 2>/dev/null | (cd "$SRC" && tar -xf -)
# 从 Windows 复制过来的 .sh 可能带 CRLF
find "$SRC" -maxdepth 2 -name '*.sh' -exec sed -i 's/\r$//' {} +
echo "   源码 $(du -sh "$SRC" | cut -f1)"

echo "== [2/4] 依赖核对 =="
uv pip install --python "$VENV/bin/python" -r "$SRC/requirements-linux.txt" 2>&1 | tail -2
"$VENV/bin/python" - <<'PY'
import sys
print("   python", sys.version.split()[0])
for m in ("PyQt6", "paramiko", "serial", "anthropic", "dotenv", "pypdf", "numpy",
          "requests", "urllib3", "websocket", "PyInstaller"):
    try:
        __import__(m); print(f"   {m}: ok")
    except Exception as e:
        print(f"   {m}: MISSING ({e})")
PY

echo "== [3/4] PyInstaller 构建（日志 $LOG）=="
cd "$SRC"
PY="$VENV/bin/python" bash build-linux.sh > "$LOG" 2>&1 || {
    echo "构建失败，最后 40 行："; tail -40 "$LOG"; exit 1
}
grep -E "^(产物|大小|SHA256|耗时)" "$LOG" || tail -20 "$LOG"

echo "== [4/4] 产物校验 =="
file dist/AITerminal | cut -c1-160
ls -l dist/AITerminal | awk '{print "   字节:", $5}'
echo "   动态库依赖数: $(ldd dist/AITerminal 2>/dev/null | wc -l)"
missing=$(ldd dist/AITerminal 2>/dev/null | grep -c "not found" || true)
echo "   缺失依赖: $missing"
echo "   ✅ 构建完成"
