"""生成 NetOpsTerminal 的应用图标（蓝底 + 白色 >_，PowerShell 那种一眼认得出是终端的形状）

为什么要有这个脚本：图标不能只有一张 256 的 PNG —— Windows 任务栏、Alt+Tab、
文件属性对话框、开始菜单磁贴分别取 16/20/24/32/40/48/64/128/256 各档。只放一张
大图让系统自己缩，小尺寸会糊成一团；每档按原生尺寸渲染再嵌进 ICO 才锐。

用法：
    .venv-train\\Scripts\\python.exe tools\\make_app_icon.py
输出：
    resources/icons/app.ico               多尺寸 ICO（打包用，build.spec 指向它）
    resources/icons/app.png               256 预览图（文档/其它地方引用）
    %TEMP%\\ait_icon_preview.png          深/浅底对照图（人眼验收用）
"""

import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QIODevice, QRectF, Qt          # noqa: E402
from PyQt6.QtGui import (QBrush, QColor, QGuiApplication, QImage,  # noqa: E402
                         QLinearGradient, QPainter, QPainterPath, QPen)

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_DIR = os.path.join(PROJECT, "resources", "icons")
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# 设计稿按 256×256 画，再按需等比缩放到各档
BLUE_TOP = "#5AA4FF"
BLUE_MID = "#2F7BEA"
BLUE_BOTTOM = "#1B4FA8"


def render(size: int) -> QImage:
    """画一张 size×size 的图标（透明底）"""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.scale(size / 256.0, size / 256.0)

    # 圆角方块（留 6 边距给系统缩放时的抗锯齿）
    shape = QPainterPath()
    shape.addRoundedRect(QRectF(6, 6, 244, 244), 58, 58)
    grad = QLinearGradient(0, 6, 0, 250)
    grad.setColorAt(0.0, QColor(BLUE_TOP))
    grad.setColorAt(0.55, QColor(BLUE_MID))
    grad.setColorAt(1.0, QColor(BLUE_BOTTOM))
    p.setPen(Qt.PenStyle.NoPen)
    p.fillPath(shape, QBrush(grad))

    # 顶部一层很淡的高光，让方块有体积感（纯平涂会显得像色块）
    p.save()
    p.setClipPath(shape)
    hl = QLinearGradient(0, 6, 0, 132)
    hl.setColorAt(0.0, QColor(255, 255, 255, 48))
    hl.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillRect(QRectF(6, 6, 244, 126), QBrush(hl))
    p.restore()

    # 白色 ">"：粗线条 + 圆头圆角，16px 下也要认得出
    pen = QPen(QColor("#FFFFFF"), 34)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    chevron = QPainterPath()
    chevron.moveTo(74, 74)
    chevron.lineTo(138, 128)
    chevron.lineTo(74, 182)
    p.drawPath(chevron)

    # 白色 "_"：右下角一根圆角横杠
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#FFFFFF"))
    p.drawRoundedRect(QRectF(150, 158, 46, 30), 12, 12)

    p.end()
    return img


def write_ico(path: str, images) -> None:
    """把若干张图写成多尺寸 ICO（每帧用 PNG 编码，Vista 以后都认）"""
    blobs = []
    for size, img in images:
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        blobs.append((size, bytes(buf.data())))
        buf.close()
    count = len(blobs)
    offset = 6 + 16 * count
    entries, payload = b"", b""
    for size, blob in blobs:
        dim = 0 if size >= 256 else size          # 256 在目录里记 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        payload += blob
    with open(path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, count) + entries + payload)


def contact_sheet(images) -> QImage:
    """深底/浅底两行、各档位按 6 倍放大排开，用来人眼验收小尺寸是否还认得出"""
    zoom, pad = 6, 10
    cell_h = max(s for s, _ in images) * zoom
    width = sum(s * zoom + pad for s, _ in images) + pad
    height = cell_h * 2 + pad * 3
    sheet = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(sheet)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    p.fillRect(0, 0, width, height // 2, QColor("#1E1E1E"))   # 深色任务栏
    p.fillRect(0, height // 2, width, height - height // 2, QColor("#F3F3F3"))
    for row in (0, 1):
        x = pad
        for size, img in images:
            y = pad + row * (cell_h + pad) + (cell_h - size * zoom) // 2
            p.drawImage(QRectF(x, y, size * zoom, size * zoom), img)
            x += size * zoom + pad
    p.end()
    return sheet


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    images = [(s, render(s)) for s in SIZES]
    ico = os.path.join(ICON_DIR, "app.ico")
    png = os.path.join(ICON_DIR, "app.png")
    os.makedirs(ICON_DIR, exist_ok=True)
    write_ico(ico, images)
    images[-1][1].save(png, "PNG")
    sheet_path = os.path.join(os.environ.get("TEMP", "."), "ait_icon_preview.png")
    contact_sheet(images).save(sheet_path, "PNG")
    print(f"ICO  {ico}  {os.path.getsize(ico)} bytes  {len(images)} 帧: "
          f"{', '.join(str(s) for s, _ in images)}")
    print(f"PNG  {png}  {os.path.getsize(png)} bytes")
    print(f"预览 {sheet_path}")

    # 自检：把刚写的 ICO 读回来，每档都要在（写错了系统只会显示空白）。
    # ⚠ 按 availableSizes() 判，不要用 pixmap(n,n).width()==n：显示器 125% 缩放时
    #    Qt 把 16px 请求返回成 20×20（devicePixelRatio=1.25），会误报。
    from PyQt6.QtGui import QIcon
    icon = QIcon(ico)
    have = {(s.width(), s.height()) for s in icon.availableSizes()}
    print(f"  读回 availableSizes: {sorted(s[0] for s in have)}")
    ok = True
    for size in SIZES:
        good = (size, size) in have
        ok = ok and good
        print(f"  {size:>3}px {'OK' if good else 'FAIL'}")
    del app
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
