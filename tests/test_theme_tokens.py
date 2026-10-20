# -*- coding: utf-8 -*-
"""主题守卫：效果图那套配色必须**真的**落在 app 里（2026-10-17）。

背景：效果图（`D:\\ait_delivery\\界面效果图.html`）定的是「深色三层 + #4C8DFF」。
      `ui/theme.py` 的注释写着"换配色只改这里 + dark.qss"，可一/二/三/四代的颜色
      散落在 `ui/*.py` 的 setStyleSheet 里 —— 只改 theme.py 是看不见效果的。
      这里四条守卫把"落地"钉住：
        ① `P` 就是效果图那套（逐键比对，防止谁顺手改回去）
        ② `dark.qss` 里只允许出现 `P` 的颜色 + 白 8%/14% 两种透明描边
        ③ `dark.qss` 能被 Qt 真的解析（抓 QSS 解析告警，顺便离屏抓一张预览图）
        ④ `ui/*.py` 里不再有历代旧色字面量（tools/retint_theme.py 的成果）

跑法：python -u tests\\test_theme_tokens.py
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

FAIL = []
TOTAL = [0]


def check(name, cond, detail=""):
    TOTAL[0] += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAIL.append(name)


from ui.theme import P, LEGACY_MAP                             # noqa: E402

QSS_PATH = os.path.join(BASE, "resources", "styles", "dark.qss")

#: 效果图 :root 里那张表（`界面效果图.html:8,347-355`）—— 这就是"设计契约"
EXPECT = {
    "bg": "#0E1117", "bg_panel": "#12171F", "bg_sunken": "#151A23", "bg_input": "#151A23",
    "bg_hover": "#182030", "bg_active": "#1C2A42",
    "border_focus": "#4C8DFF",
    "fg": "#E6EAF2", "fg_muted": "#93A0B4", "fg_disabled": "#6B7684",
    "accent": "#4C8DFF", "accent_hover": "#3F7DE8", "accent_fg": "#9CC2FF",
    "ok": "#3FB950", "ok_fg": "#5FD47A", "warn": "#D29922", "warn_fg": "#E3B341",
    "err": "#F85149",
    "term_fg": "#CFD8E6", "term_prompt": "#7FD18A", "term_cmd": "#8AB4FF",
}

print("=== ① 令牌就是效果图那套 ===")
for key, want in EXPECT.items():
    check(f"P[{key!r}] == {want}", P.get(key) == want, str(P.get(key)))
palette = set(P.values())
check("每个令牌都是 #RRGGBB", all(re.fullmatch(r"#[0-9A-F]{6}", v) for v in P.values()),
      str([v for v in P.values() if not re.fullmatch(r"#[0-9A-F]{6}", v)]))
check("四代旧色已登记进 LEGACY_MAP（#61afef/#282c34/#abb2bf 等）",
      all(k in LEGACY_MAP for k in ("#61afef", "#282c34", "#abb2bf", "#7f848e", "#e06c75")),
      f"LEGACY_MAP 共 {len(LEGACY_MAP)} 条")

print("\n=== ② dark.qss 只使用令牌色 ===")
with open(QSS_PATH, encoding="utf-8") as fh:
    qss = fh.read()
hexes = set(re.findall(r"#[0-9a-fA-F]{6}", qss))
stray = sorted(h for h in hexes if h.upper() not in {v.upper() for v in palette} | {"#FFFFFF"})
check("没有令牌之外的颜色（含漏网的旧色）", not stray, str(stray))
rgba = set(re.findall(r"rgba\([^)]*\)", qss))
check("只有白 8% / 14% 两种透明描边", rgba <= {"rgba(255, 255, 255, 20)",
                                            "rgba(255, 255, 255, 36)"}, str(sorted(rgba)))
check("画布/面板/强调三色都在 qss 里出现",
      all(v.upper() in qss.upper() for v in (P["bg"], P["bg_panel"], P["accent"])),
      f"{len(hexes)} 种颜色 / {len(qss)} 字符")
check("旧一代的标题字面量已清掉", "One Dark" not in qss)

print("\n=== ③ Qt 真能解析这份 qss ===")
from PyQt6.QtWidgets import (QApplication, QDialog, QPushButton,            # noqa: E402
                             QLineEdit)
from PyQt6.QtCore import qInstallMessageHandler                            # noqa: E402

messages = []
qInstallMessageHandler(lambda mode, ctx, msg: messages.append(str(msg)))
app = QApplication.instance() or QApplication([])
app.setStyleSheet(qss)
probe = QDialog()       # qss 里只有 QMainWindow/QDialog 显式给了画布底色
probe.resize(320, 140)
btn = QPushButton("确定", probe)
btn.setGeometry(20, 20, 90, 30)
edit = QLineEdit(probe)
edit.setGeometry(20, 70, 200, 28)
probe.show()
pix = probe.grab()
preview = os.path.join(os.environ.get("TEMP", "."), "ait-theme-preview.png")
saved = pix.save(preview)
bad = [m for m in messages if "parse" in m.lower() or "unknown" in m.lower()]
check("没有 QSS 解析告警", not bad, "; ".join(bad[:3]))
check("能离屏渲染出非空画面", saved and not pix.isNull(), f"{pix.width()}x{pix.height()} -> {preview}")
shade = pix.toImage().pixelColor(5, 5).name().upper()
check("窗口底色 = 画布色", shade == P["bg"], f"实际 {shade}")
probe.close()

print("\n=== ④ ui/*.py 里不再有历代旧色字面量 ===")
ui_dir = os.path.join(BASE, "ui")
hits = []
for name in sorted(os.listdir(ui_dir)):
    if not name.endswith(".py") or name == "theme.py":
        continue                        # theme.py 里的 LEGACY_MAP 就是旧色登记表
    with open(os.path.join(ui_dir, name), encoding="utf-8") as fh:
        body = fh.read()
    for old in LEGACY_MAP:
        if old in body:         # 大小写敏感：新色是大写 #3FB950，别把同名新色判成旧色
            hits.append(f"{name}:{old}")
check("ui/*.py 无旧色（新色请写成 theme.P[...] 或直接令牌色）", not hits,
      ", ".join(hits[:6]))

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
