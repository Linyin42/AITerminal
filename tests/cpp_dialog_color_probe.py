# -*- coding: utf-8 -*-
"""对话框配色取证：把 8 个对话框逐个渲染出来，逐像素量「标题栏色 / 窗底色」，
再拼成一张对照图。用于 UI-5（"表头亮、内容最深底"）这类**颜色**问题的定位 ——
不靠肉眼形容词，只给色值。

背景（为什么需要它）：2026-10-03 那次 UI-5 排查用"取色表"得出了错误结论
（说 ai/diff/knowledge/patrol/usage 五个窗"内容区应为面板灰"），原因是取色点
落在中部的大控件上，而那些控件本来就该是 @bg-surface。这个脚本把采样点固定在
**左边缘、标题栏正下方**（卡片/内容控件盖不到那里），量到的才是"窗底"本身。

依赖：Pillow（`pip install Pillow`）。用法：
    python tests/cpp_dialog_color_probe.py
输出：
    %TEMP%\\ait_ui_shots\\ui5_<name>.png    每个窗的截图
    %TEMP%\\ait_ui_shots\\ui5_board.png     8 窗对照图（标题栏 vs 窗底）

判据：`标题栏色 == 窗底色` 才算"一个窗一张面板"。当前已知 6 个窗过、2 个不过
（ConnectDialog 的两种形态；它没有 setProperty("panelWindow", true)）。
"""
import os
import subprocess
import sys
import tempfile
from collections import Counter

TEMP = tempfile.gettempdir()
EXE = os.path.join(TEMP, "ait_cpp_build", "AITerminal.exe")
OUT = os.path.join(TEMP, "ait_ui_shots")

TB = 48          # 标题栏高度（设备像素；逻辑 40 × DPR 1.2）
BAND = 96        # 紧邻内容带高度
PANEL = (0x37, 0x3a, 0x3f)     # @bg-panel
WINDOW = (0x28, 0x2b, 0x30)    # @bg-window

# --shot-dialog 的值 -> (显示名, 该窗是否设了 panelWindow)
DIALOGS = [
    ("connect",        "新建连接 (ConnectDialog)",       False),
    ("connect-serial", "新建连接·串口 (ConnectDialog)",  False),
    ("ai",             "AI 助手 (AiPanel)",              True),
    ("ai-settings",    "AI 设置 (AiSettingsDialog)",     True),
    ("diff",           "配置差异比对 (DiffDialog)",      True),
    ("knowledge",      "本地资料库 (KnowledgeDialog)",   True),
    ("patrol",         "多设备巡检 (PatrolPanel)",       True),
    ("usage-summary",  "使用总结 (UsageSummaryDialog)",  True),
]


def render():
    """逐个 --shot-dialog 出图。GUI 程序不能用管道收 stdout（孙子进程会占住写端），
    这里直接重定向到文件。"""
    for name, _, _ in DIALOGS:
        path = os.path.join(OUT, "ui5_%s.png" % name)
        with open(os.path.join(TEMP, "ui5_%s.log" % name), "wb") as sink:
            subprocess.run([EXE, "--shot-dialog=%s" % name, "--screenshot=%s" % path],
                           stdout=sink, stderr=subprocess.STDOUT, timeout=120)
    print("渲染完成 ->", OUT)


def measure():
    from PIL import Image, ImageDraw, ImageFont

    os.makedirs(OUT, exist_ok=True)
    rows = []
    for name, label, has_prop in DIALOGS:
        p = os.path.join(OUT, "ui5_%s.png" % name)
        if not os.path.exists(p):
            print("  [跳过] 没有截图:", p)
            continue
        img = Image.open(p).convert("RGB")
        tb = Counter(img.crop((0, 0, img.width, TB)).getdata()).most_common(1)[0][0]
        # 窗底：左边缘 x=2、标题栏正下方 —— 卡片/内容控件不会盖到那里
        body = img.getpixel((2, TB + 6))
        rows.append((name, label, has_prop, tb, body, img))

    def hexc(c):
        return "#%02x%02x%02x" % tuple(c)

    print("\n== 标题栏 vs 窗底 ==")
    bad = 0
    for name, label, has_prop, tb, body, _ in rows:
        ok = tb == body
        bad += 0 if ok else 1
        print("  [%s] %-34s panelWindow=%-3s 标题栏 %s / 窗底 %s"
              % ("PASS" if ok else "FAIL", label, "有" if has_prop else "无",
                 hexc(tb), hexc(body)))

    # ---- 对照图 ----
    W, LBL, PAD = 700, 250, 10
    row_h = TB + BAND + PAD
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 14)
        fontb = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 15)
    except Exception:
        font = fontb = ImageFont.load_default()

    canvas = Image.new("RGB", (LBL + W + 130 + PAD * 2, row_h * len(rows) + 46), (24, 27, 33))
    d = ImageDraw.Draw(canvas)
    d.text((PAD, 8), "对话框配色取证：「标题栏 vs 窗底」实测色", font=fontb, fill=(243, 245, 252))
    d.text((PAD, 26), "窗底采样点 = 左边缘 x=2、标题栏正下方（卡片/内容控件不会盖到那里）",
           font=font, fill=(146, 150, 159))
    for i, (name, label, has_prop, tb, body, img) in enumerate(rows):
        ok = tb == body
        y = 46 + i * row_h
        sc = W / img.width
        d.text((PAD, y + 4), label, font=fontb, fill=(243, 245, 252))
        d.text((PAD, y + 26), "panelWindow 属性: %s" % ("有" if has_prop else "没有 ←"),
               font=font, fill=(189, 193, 201) if has_prop else (250, 131, 119))
        d.text((PAD, y + 46), "标题栏 %s" % hexc(tb), font=font, fill=(189, 193, 201))
        d.text((PAD, y + 64), "窗底   %s" % hexc(body), font=font,
               fill=(91, 190, 125) if ok else (250, 131, 119))
        d.text((PAD, y + 84), "一致 ✓" if ok else "两层 ✗", font=fontb,
               fill=(91, 190, 125) if ok else (250, 131, 119))
        canvas.paste(img.crop((0, 0, img.width, TB)).resize((W, int(TB * sc)), Image.LANCZOS),
                     (LBL, y))
        canvas.paste(img.crop((0, TB, img.width, TB + BAND)).resize((W, int(BAND * sc)), Image.LANCZOS),
                     (LBL, y + int(TB * sc)))
        x = LBL + W + 10
        d.rectangle([x, y, x + 100, y + TB - 2], fill=tuple(tb))
        d.rectangle([x, y + TB, x + 100, y + TB + BAND - 2], fill=tuple(body))
        d.text((x, y + TB + BAND + 2), "上=标题栏 下=窗底", font=font, fill=(146, 150, 159))
    d.text((PAD, 46 + len(rows) * row_h + 4),
           "结论：%d/%d 个窗「标题栏 ≠ 窗底」。" % (bad, len(rows)),
           font=fontb, fill=(91, 190, 125) if bad == 0 else (250, 131, 119))

    board = os.path.join(OUT, "ui5_board.png")
    canvas.save(board)
    print("\n对照图 ->", board)
    return bad


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    if "--no-render" not in sys.argv:
        render()
    sys.exit(1 if measure() else 0)
