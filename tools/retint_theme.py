"""把 app 配色从 One Dark Pro 换成"效果图那套"（五代），机械替换、可复核。

背景：`D:\\ait_delivery\\界面效果图.html` 定的配色是「深色三层 + #4C8DFF」，
      ui/theme.py 的注释本就写着"换配色只改这里 + dark.qss"，
      但历史散落字面量得按同一张表扫一遍：
        · resources/styles/dark.qss  637 行 / 15 种颜色
        · ui/*.py 里 setStyleSheet 的 hex（约 100 处）

用法：
    python tools\\retint_theme.py --dry-run    # 只报告，不动文件
    python tools\\retint_theme.py --apply      # 真改（每个文件先备份 .bak-retint）

规则：
    · 常规 hex 一律按 MAP（= ui/theme.py 的 LEGACY_MAP，键是小写旧色）
    · dark.qss 里 #3e4451 按上下文分流：background/selection → 选中底；border → 描边
    · 只替换小写键，新值是大写，故不会二次命中（单遍安全）
"""
import argparse
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QSS = os.path.join(BASE, "resources", "styles", "dark.qss")

#: 旧色 → 新色（与 ui/theme.py 的 LEGACY_MAP 同源，此处内联成纯文本表，避免
#: 依赖 PyQt：本脚本要能在没有 Qt 的机器上跑）
MAP = {
    # ---- 四代 One Dark Pro（本代的主要来源）----
    "#282c34": "#0E1117", "#21252b": "#12171F", "#2c313a": "#151A23",
    "#3a3f4b": "#182030", "#3e4451": "#1C2A42",
    "#4b5263": "#33373E", "#61afef": "#4C8DFF", "#7cc0f5": "#4C8DFF",
    "#4d95d0": "#3F7DE8",
    "#abb2bf": "#E6EAF2", "#d7dae0": "#E6EAF2", "#7f848e": "#93A0B4",
    "#5c6370": "#6B7684", "#262b33": "#12171F",
    "#98c379": "#3FB950", "#e5c07b": "#D29922", "#e06c75": "#F85149",
    "#56b6c2": "#4C8DFF", "#c678dd": "#9CC2FF",
    # ---- 三代：新版 PyCharm ----
    "#1e1f22": "#0E1117", "#2b2d30": "#12171F", "#26282c": "#151A23",
    "#393b40": "#242931", "#43454a": "#33373E", "#333539": "#182030",
    "#2e436e": "#1C2A42",
    "#4a88c7": "#4C8DFF", "#5b9bd5": "#3F7DE8", "#6ba3d6": "#9CC2FF",
    "#3d76ad": "#3F7DE8",
    "#dfe1e5": "#E6EAF2", "#9da0a8": "#93A0B4", "#6f737a": "#6B7684",
    "#57965c": "#3FB950", "#d5b778": "#D29922", "#db5c5c": "#F85149",
    "#bcbec4": "#CFD8E6", "#4e5157": "#33373E", "#5a5d63": "#6B7684",
    # ---- 二代：经典 Darcula ----
    "#2b2b2b": "#0E1117", "#3c3f41": "#12171F", "#313335": "#151A23",
    "#323232": "#242931", "#3f4143": "#33373E",
    "#45494a": "#151A23", "#4c5052": "#182030", "#5a5e60": "#33373E",
    "#214283": "#1C2A42",
    "#bbbbbb": "#E6EAF2", "#e8e8e8": "#E6EAF2", "#808080": "#93A0B4",
    "#5f6164": "#6B7684",
    "#a9b7c6": "#CFD8E6", "#4b4b4b": "#33373E", "#5f5f5f": "#6B7684",
    "#466d94": "#4C8DFF",
    "#499c54": "#3FB950", "#e05454": "#F85149", "#d9a343": "#D29922",
    "#6e7175": "#6B7684", "#c7d5e6": "#CFD8E6",
    "#ffa657": "#D29922", "#79c0ff": "#4C8DFF", "#f778ba": "#9CC2FF",
    "#ce9178": "#D29922",
    # ---- 一代：靛蓝 Indigo（仿 Netcatty）----
    "#12141c": "#0E1117", "#14161f": "#0E1117", "#111318": "#0E1117",
    "#171a24": "#12171F", "#1a1e29": "#151A23", "#1e222f": "#151A23",
    "#242938": "#182030", "#262b3a": "#182030", "#1c202c": "#151A23",
    "#2b3143": "#242931", "#39415a": "#33373E", "#232a45": "#1C2A42",
    "#e6e9f2": "#E6EAF2", "#c5cddd": "#E6EAF2", "#98a1b5": "#93A0B4",
    "#7c8cf8": "#4C8DFF", "#98a5ff": "#9CC2FF", "#a5b1ff": "#9CC2FF",
    "#5f6fe8": "#3F7DE8", "#2b3450": "#1C2A42",
    "#3fb950": "#3FB950", "#f85149": "#F85149", "#d29922": "#D29922",
    "#22d3ee": "#4C8DFF", "#666e82": "#6B7684", "#565e70": "#6B7684",
    "#8b93a7": "#93A0B4", "#7d8598": "#93A0B4",
}

#: dark.qss 特例：#3e4451 在旧配色里既是描边又是选中底，按行内容分流
QSS_FILL_KEYS = ("background:", "selection-background-color:")

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")
BORDER_8 = "rgba(255, 255, 255, 20)"     # 白 8%（效果图的 1px 描边）
BORDER_14 = "rgba(255, 255, 255, 36)"    # 白 14%（悬停描边 / 滚动条把手）


def sweep_text(text, path, report):
    """返回替换后的文本；report 收 (旧, 新, 次数)"""
    counts = {}

    is_qss = os.path.basename(path) == "dark.qss"

    def resolve(old, line):
        """#3e4451/#4b5263 在旧配色里既是底也是描边，按行内容分流"""
        low = line.lower()
        if old == "#3e4451":
            if any(k in low for k in QSS_FILL_KEYS):
                return "#1C2A42"                       # 选中/悬停底（accent 14% 叠面板）
            return BORDER_8 if is_qss else "#242931"   # 描边：qss 用 8% 白，py 里用等效实色
        if old == "#4b5263":
            return BORDER_14 if is_qss else "#33373E"
        return MAP[old]

    def one_line(line):
        out = line
        hit = False
        for old in sorted(MAP, key=len, reverse=True):
            if old not in out.lower():
                continue
            new = resolve(old, out)
            out = re.sub(re.escape(old), new, out)
            key = f"{old}->{new}"
            counts[key] = counts.get(key, 0) + 1
            hit = True
        return out, hit

    out_lines = []
    for line in text.splitlines(keepends=True):
        new_line, hit = one_line(line)
        out_lines.append(new_line)
        if hit:
            report[os.path.relpath(path, BASE)] = report.get(os.path.relpath(path, BASE), 0) + 1
    return "".join(out_lines), counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真改文件（默认只报告）")
    ap.add_argument("--dry-run", action="store_true", help="只报告（默认行为）")
    a = ap.parse_args()
    apply = a.apply

    targets = [QSS]
    ui_dir = os.path.join(BASE, "ui")
    for name in sorted(os.listdir(ui_dir)):
        if name.endswith(".py") and name != "theme.py":   # 令牌表本身手工改，别机械扫
            targets.append(os.path.join(ui_dir, name))

    total = {}
    for path in targets:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        new, counts = sweep_text(src, path, total)
        if new == src:
            continue
        rel = os.path.relpath(path, BASE)
        print(f"  {rel}: " + ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())))
        if apply:
            with open(path + ".bak-retint", "w", encoding="utf-8", newline="") as fh:
                fh.write(src)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)

    print(f"\n共 {len(total)} 个文件、{sum(total.values())} 处" + ("（已写盘，备份 .bak-retint）" if apply else "（仅报告，未写盘）"))
    if not apply:
        print("dry-run 结束：『残留旧色』核对只在 --apply 后才有意义。")
        return 0
    left = []
    for path in targets:
        with open(path, encoding="utf-8") as fh:
            body = fh.read().lower()
        for old in MAP:
            if old in body and old != MAP[old].lower():
                left.append((os.path.relpath(path, BASE), old))
        if os.path.basename(path) == "dark.qss":
            if "#3e4451" in body or "#4b5263" in body:
                left.append((os.path.relpath(path, BASE), "#3e4451/#4b5263"))
    print("残留旧色：" + ("无" if not left else str(left)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
