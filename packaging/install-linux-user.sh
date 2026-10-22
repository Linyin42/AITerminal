#!/usr/bin/env bash
# NetOpsTerminal Linux 安装脚本（不需要 sudo：默认装到当前用户的 ~/.local）
#
# 两种目录布局都支持：
#   A) 交付包目录：NetOpsTerminal 与 install.sh 并排
#   B) 源码构建目录：dist/NetOpsTerminal（或上层目录的 dist/）
#
# 用法:
#   bash install.sh                          # 装到 ~/.local/share/aiterminal
#   bash install.sh --prefix /opt/aiterminal # 系统级（需要自己对 /opt 有写权限）
#   bash install.sh --no-knowledge           # 不复制 knowledge/（约 5 GB）
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NO_KNOWLEDGE=0
PREFIX="$HOME/.local/share/aiterminal"

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="${2:?--prefix 需要一个路径}"; shift 2 ;;
        --no-knowledge) NO_KNOWLEDGE=1; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

# ---- 找二进制与资源
if [ -x "$HERE/dist/NetOpsTerminal" ]; then
    SRC_DIR="$HERE"; BIN="$HERE/dist/NetOpsTerminal"
elif [ -x "$HERE/NetOpsTerminal" ]; then
    SRC_DIR="$HERE"; BIN="$HERE/NetOpsTerminal"
elif [ -x "$HERE/../dist/NetOpsTerminal" ]; then
    SRC_DIR="$(cd "$HERE/.." && pwd)"; BIN="$SRC_DIR/dist/NetOpsTerminal"
elif [ -x "$PWD/dist/NetOpsTerminal" ]; then
    # 在源码根目录执行 `bash packaging/install-linux-user.sh` 的情况
    SRC_DIR="$PWD"; BIN="$PWD/dist/NetOpsTerminal"
else
    echo "找不到 NetOps Terminal 可执行文件。若是源码目录，先跑 bash build-linux.sh" >&2
    exit 2
fi

RES_SRC="$SRC_DIR/resources"
[ -d "$RES_SRC" ] || RES_SRC=""
ICON_SRC=""
for c in "$SRC_DIR/resources/icons/app.png" "$SRC_DIR/app.png" "$HERE/app.png"; do
    [ -f "$c" ] && { ICON_SRC="$c"; break; }
done

echo "== 安装 NetOps Terminal =="
echo "   二进制: $BIN"
echo "   目标:   $PREFIX"

mkdir -p "$PREFIX"
install -m 0755 "$BIN" "$PREFIX/NetOpsTerminal"

# 资源：源码布局直接整个拷；交付布局只有 app.png，就补一个最小 resources/icons
rm -rf "$PREFIX/resources"
if [ -n "$RES_SRC" ]; then
    cp -a "$RES_SRC" "$PREFIX/resources"
else
    mkdir -p "$PREFIX/resources/icons"
    [ -n "$ICON_SRC" ] && cp "$ICON_SRC" "$PREFIX/resources/icons/app.png"
fi

# 资料库（可选，很大）
if [ "$NO_KNOWLEDGE" -eq 0 ] && [ -d "$SRC_DIR/knowledge" ]; then
    echo "   复制 knowledge/（约 5 GB，稍等）…"
    rm -rf "$PREFIX/knowledge"
    cp -a "$SRC_DIR/knowledge" "$PREFIX/knowledge"
fi

# ---- 启动器与图标
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$APPS_DIR" "$ICON_DIR"

DESKTOP_SRC="$HERE/netops-terminal.desktop"
[ -f "$DESKTOP_SRC" ] || DESKTOP_SRC="$HERE/packaging/netops-terminal.desktop"
if [ -f "$DESKTOP_SRC" ]; then
    sed -e "s|/opt/aiterminal|$PREFIX|g" \
        -e "s|^Icon=.*|Icon=$PREFIX/resources/icons/app.png|" "$DESKTOP_SRC" \
        > "$APPS_DIR/netops-terminal.desktop"
    echo "   启动器: $APPS_DIR/netops-terminal.desktop"
fi
if [ -n "$ICON_SRC" ]; then
    install -m 0644 "$ICON_SRC" "$ICON_DIR/netops-terminal.png"
    echo "   图标:   $ICON_DIR/netops-terminal.png"
fi

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo
echo "安装完成："
echo "  程序:   $PREFIX/NetOpsTerminal"
echo "  运行:   $PREFIX/NetOpsTerminal"
echo "  菜单:   应用列表里搜 NetOps Terminal"
