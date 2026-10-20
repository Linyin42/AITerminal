"""生成 AITerminal 应用图标：三套设计方案，各出九档 ICO + 深/浅底预览

设计目标（按 16px 能不能认得出倒推）：
  ① 轮廓只有一个：圆角方块 —— 任务栏 16px 时任何细节都会被压成噪点，靠形状+一个大字
     母的对比取胜；
  ② 字怀只有一个：终端提示符 `>` + 光标块。`>_` 是"终端"这个品类的通用符号，不用解释；
  ③ 只留一处高饱和强调色（光标块），缩到 16px 仍是一块实色，不会糊；
  ④ 渐变只在背景里，且同一色相内过渡 —— 跨色相渐变在小尺寸上会脏。

三套：
  A deep   深海蓝→紫 + 白 `>` + 翠绿光标（推荐：暗色主题工具里最稳，强调色最跳）
  B cyan   青→蓝亮渐变 + 白 `>` + 白光标（最亮眼，浅色任务栏上最好认）
  C ink    近黑深蓝 + 霓虹青 `>` + 琥珀光标（老图标的进化版，暗色桌面最协调）

用法：
    .venv-train\\Scripts\\python.exe tools\\make_app_icons.py            # 三套都出
    .venv-train\\Scripts\\python.exe tools\\make_app_icons.py --pick A    # 顺便写进 app.ico
输出：
    resources/icons/app-A.ico / app-B.ico / app-C.ico   各九档
    %TEMP%\\ait_icon_A.png / _B / _C                    深/浅底对照（人眼验收）
"""

import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt      # noqa: E402
from PyQt6.QtGui import (QBrush, QColor, QGuiApplication, QImage,      # noqa: E402
                         QLinearGradient, QPainter, QPainterPath, QPen)

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_DIR = os.path.join(PROJECT, "resources", "icons")
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# 每套方案：背景三段渐变、字怀颜色、光标颜色、是否描一圈内环
VARIANTS = {
    "A": {
        "name": "deep",
        "bg": ("#2563EB", "#4F46E5", "#7C3AED"),   # 蓝 → 靛 → 紫
        "glyph": "#FFFFFF",
        "cursor": "#34D399",                        # 翠绿：唯一强调色
        "ring": None,
    },
    "B": {
        "name": "cyan",
        "bg": ("#22D3EE", "#3B82F6", "#1D4ED8"),   # 青 → 蓝 → 深蓝
        "glyph": "#FFFFFF",
        "cursor": "#FFFFFF",
        "ring": None,
    },
    "C": {
        "name": "ink",
        "bg": ("#0B1020", "#101A30", "#1B2742"),   # 近黑深蓝
        "glyph": "#22D3EE",                        # 霓虹青
        "cursor": "#F59E0B",                       # 琥珀
        "ring": "#2C3E63",
    },
}


def render(spec: dict, size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.scale(size / 256.0, size / 256.0)

    shape = QPainterPath()
    shape.addRoundedRect(QRectF(6, 6, 244, 244), 58, 58)

    grad = QLinearGradient(QPointF(6, 6), QPointF(250, 250))   # 对角，比竖向更有动势
    grad.setColorAt(0.0, QColor(spec["bg"][0]))
    grad.setColorAt(0.55, QColor(spec["bg"][1]))
    grad.setColorAt(1.0, QColor(spec["bg"][2]))
    p.setPen(Qt.PenStyle.NoPen)
    p.fillPath(shape, QBrush(grad))

    # 顶部高光：只在大档位加，16px 下加进去只会让图片发灰
    if size >= 32:
        p.save()
        p.setClipPath(shape)
        hl = QLinearGradient(0, 6, 0, 130)
        hl.setColorAt(0.0, QColor(255, 255, 255, 46))
        hl.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(QRectF(6, 6, 244, 124), QBrush(hl))
        p.restore()

    if spec["ring"]:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(spec["ring"]), 4))
        p.drawRoundedRect(QRectF(12, 12, 232, 232), 50, 50)

    # `>`：粗线圆头。16px 下这条线只占 3px 宽，正是它能被认出来的原因
    pen = QPen(QColor(spec["glyph"]), 36)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    chevron = QPainterPath()
    chevron.moveTo(78, 70)
    chevron.lineTo(148, 128)
    chevron.lineTo(78, 186)
    p.drawPath(chevron)

    # 光标块：实色块，缩到 16px 仍是一块饱和色 —— 整套图标的"记忆点"
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(spec["cursor"]))
    p.drawRoundedRect(QRectF(158, 150, 52, 36), 14, 14)

    p.end()
    return img


def write_ico(path: str, images) -> None:
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
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        payload += blob
    with open(path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, count) + entries + payload)


def contact_sheet(images, caption: str) -> QImage:
    """深色/浅色两行各档位放大：验收"16px 在深色任务栏上还认不认得出"

    ⚠ 不在这张图上写任何文字：离屏渲染环境里没有字体（Qt 报 no longer ships fonts），
      写上去只会是一排方框 "□□□"，反而更难看。尺寸顺序 16→256 由调用方说明。
    """
    zoom, pad = 6, 12
    cell_w = 256 * zoom // 6
    row_h = cell_w + pad
    width = len(images) * (cell_w + pad) + pad
    height = row_h * 2 + pad * 2
    sheet = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(sheet)
    p.fillRect(0, 0, width, height // 2, QColor("#202020"))       # 深色任务栏
    p.fillRect(0, height // 2, width, height - height // 2, QColor("#F3F3F3"))
    for row in (0, 1):
        x = pad
        for size, img in images:
            scale = min(cell_w / size, cell_w / size)
            draw_w = int(size * scale)
            y = pad + row * (row_h + pad) + (cell_w - draw_w) // 2
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            p.drawImage(QRectF(x, y, draw_w, draw_w), img)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            x += cell_w + pad
    p.end()
    return sheet


def verify_ico(path: str) -> bool:
    """写回读：每档都得在（缺帧时系统只会显示空白，不会报错）"""
    from PyQt6.QtGui import QIcon
    have = {(s.width(), s.height()) for s in QIcon(path).availableSizes()}
    missing = [s for s in SIZES if (s, s) not in have]
    print(f"  读回 availableSizes: {sorted(s[0] for s in have)}"
          + (f"  缺档: {missing}" if missing else "  九档齐全"))
    return not missing


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    os.makedirs(ICON_DIR, exist_ok=True)
    picked = ""
    if "--pick" in sys.argv:
        i = sys.argv.index("--pick")
        if i + 1 < len(sys.argv):
            picked = sys.argv[i + 1].strip().upper()

    all_ok = True
    for key, spec in VARIANTS.items():
        images = [(s, render(spec, s)) for s in SIZES]
        ico = os.path.join(ICON_DIR, f"app-{key}.ico")
        write_ico(ico, images)
        sheet = os.path.join(os.environ.get("TEMP", "."), f"ait_icon_{key}.png")
        contact_sheet(images, f"方案 {key}（{spec['name']}）  深色任务栏 / 浅色背景  各档原生渲染").save(
            sheet, "PNG")
        print(f"[{key}] {spec['name']:<5} ICO {os.path.getsize(ico):>6} B  {ico}")
        print(f"     预览 {sheet}")
        all_ok = verify_ico(ico) and all_ok

    if picked and picked in VARIANTS:
        import shutil
        target = os.path.join(ICON_DIR, "app.ico")
        shutil.copyfile(os.path.join(ICON_DIR, f"app-{picked}.ico"), target)
        images = [(s, render(VARIANTS[picked], s)) for s in SIZES]
        images[-1][1].save(os.path.join(ICON_DIR, "app.png"), "PNG")
        print(f"\n已选用方案 {picked} -> {target}（打包 build.spec 用的就是它）")
        all_ok = verify_ico(target) and all_ok
    del app
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
