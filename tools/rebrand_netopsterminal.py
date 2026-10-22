# -*- coding: utf-8 -*-
"""把产品名 AITerminal 改成 NetOps Terminal（网络运维终端）——一次性改名工具。

为什么要有这个脚本（而不是手改 488 处）：
  1) 名字散在 125 个文件里，手改必漏；漏一处就是"界面叫 NetOps Terminal、exe 还叫
     AITerminal.exe"这种半拉子状态。
  2) **大小写是分界线**：`AITerminal` 是品牌，改；`aiterminal` 是路径/标识
     （`~/.aiterminal`、`AIT_*` 环境变量、MCP 的 serverName、`aiterminal_mcp.py`），
     一律不动 —— 动了会丢用户配置、5GB 资料库和外部 AI 客户端的注册。
  3) 可重复跑：改过的规则自动跳过，并用"改前/改后计数"自证命中，不是盲替换。

跑法：
    python -u tools/rebrand_netopsterminal.py            # 试运行，只打印计划
    python -u tools/rebrand_netopsterminal.py --apply    # 真改
    python -u tools/rebrand_netopsterminal.py --check    # 改完查残留

**保护名单**（这些地方的 AITerminal 是路径/外部契约，不能改）：
  · core/usage_log.py                     %APPDATA%\\AITerminal\\usage-log = 用量日志数据目录
  · tests/snapshots/usage_dto_baseline.json  快照里存着上面那个真实路径
  · cpp/** 与 tests/cpp_ui_regression.py、tools/check_terminal_colors_snapshot.py
                                          旧 C++ 界面本轮不改（exe 仍叫 AITerminal.exe）
  · PROJECT-DIGEST.md                     历史记录，本轮改写进 §17
  · 本脚本自身
"""
import io
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".git", "dist", "build", "__pycache__", "knowledge", "node_modules",
             ".venv", "venv", "evidence", "_shots"}
# 只跳过文档里的厂商资料目录
SKIP_REL_PREFIX = ("docs" + os.sep + "vendor",)

SKIP_FILES = {
    "PROJECT-DIGEST.md",                        # 历史记录：保留旧名，改名写进 §17
    "core" + os.sep + "usage_log.py",            # %APPDATA%\AITerminal\usage-log 数据目录
    os.path.join("tests", "snapshots", "usage_dto_baseline.json"),  # 快照里那条真实路径
    "tools" + os.sep + "rebrand_netopsterminal.py",   # 本脚本
}
SKIP_PREFIX_DIRS = ("cpp" + os.sep,)            # C++ 旧界面单独处理
SKIP_EXACT = {
    "cpp_ui_regression.py",                 # 跑的是 C++ exe（本轮不改）
    "check_terminal_colors_snapshot.py",    # 同上
}

EXTS = (".py", ".spec", ".md", ".txt", ".desktop", ".sh", ".bat", ".json",
        ".qss", ".ps1", ".html", ".svg", ".gitignore")

# ---------------------------------------------------------------- 规则
# 顺序有意义：**产物/标识**（ASCII、无空格）先替，再替散文里的品牌名。
ARTIFACT_RULES = [
    ("AITerminal.exe", "NetOpsTerminal.exe"),
    ("AITerminal-便携.zip", "NetOpsTerminal-便携.zip"),
    ("AITerminal-linux.spec", "NetOpsTerminal-linux.spec"),
    ("dist/AITerminal", "dist/NetOpsTerminal"),
    ("dist\\AITerminal", "dist\\NetOpsTerminal"),
    ("--name=AITerminal", "--name=NetOpsTerminal"),
    ('name="AITerminal"', 'name="NetOpsTerminal"'),
    ("AITerminal.Terminal.0.7", "NetOpsTerminal.Terminal.0.7"),
    ("AITerminalControl/1.0", "NetOpsTerminalControl/1.0"),
]

# 小写里少数"名字"（不是路径）才改；其余 aiterminal 全部保留
LOWER_NAME_RULES = [
    ('name="aiterminal"', 'name="netops-terminal"'),
    ('"aiterminal=main:main"', '"netopsterminal=main:main"'),
    ("aiterminal.desktop", "netops-terminal.desktop"),
    ("aiterminal.png", "netops-terminal.png"),
]

BRAND_RULES_MD = [
    ("AI智能终端", "网络运维终端"),
    ("AITerminal Team", "NetOps Terminal Team"),
    ("AITerminal", "NetOps Terminal"),
]

BRAND_RULES_CODE = [
    ("AI智能终端", "网络运维终端"),
    ("AITerminal Team", "NetOps Terminal Team"),
    ("AITerminal", "NetOpsTerminal"),
]

# 代码里少数"给人看"的串要带空格（在 BRAND_RULES_CODE 之后、按改后文本精确修）
DISPLAY_FIXES = {
    os.path.join("ui", "main_window.py"): [
        ('setWindowTitle("NetOpsTerminal - 网络运维终端 v0.7")',
         'setWindowTitle("NetOps Terminal · 网络运维终端 v0.7")'),
        ('QLabel("NetOpsTerminal")', 'QLabel("NetOps Terminal")'),
        ('"关于 NetOpsTerminal"', '"关于 NetOps Terminal"'),
        ('"<h3>NetOpsTerminal v0.7</h3>"', '"<h3>NetOps Terminal v0.7</h3>"'),
        ('"来源": "NetOpsTerminal AI 助手"', '"来源": "NetOps Terminal AI 助手"'),
    ],
    os.path.join("core", "control_api.py"): [
        ('APP_NAME = "NetOpsTerminal"', 'APP_NAME = "NetOps Terminal"'),
    ],
    "setup.py": [
        ('description="NetOpsTerminal - 集成 AI 辅助功能的网络设备终端工具"',
         'description="NetOps Terminal - 集成 AI 辅助功能的网络设备运维终端工具"'),
        ('description = "使用 PyInstaller 打包 NetOpsTerminal"',
         'description = "使用 PyInstaller 打包 NetOps Terminal"'),
    ],
    os.path.join("data_tools", "mcp", "aiterminal_mcp.py"): [
        ("—— NetOpsTerminal 桌面程序可能没在运行", "—— NetOps Terminal 桌面程序可能没在运行"),
        ("（先启动 NetOpsTerminal，", "（先启动 NetOps Terminal，"),
        ("NetOpsTerminal 接口 %s %s 返回 HTTP", "NetOps Terminal 接口 %s %s 返回 HTTP"),
        ("连不上 NetOpsTerminal 本地接口", "连不上 NetOps Terminal 本地接口"),
        ('"NetOpsTerminal %s  pid=%s', '"NetOps Terminal %s  pid=%s'),
        ("查询本机 NetOpsTerminal 桌面程序", "查询本机 NetOps Terminal 桌面程序"),
        ("列出本机 NetOpsTerminal 当前打开", "列出本机 NetOps Terminal 当前打开"),
        ("在本机 NetOpsTerminal 里打开", "在本机 NetOps Terminal 里打开"),
    ],
    os.path.join("tests", "test_mcp_bridge.py"): [
        ('"app": "NetOpsTerminal"', '"app": "NetOps Terminal"'),
        ('"NetOpsTerminal", "0.7.0"', '"NetOps Terminal", "0.7.0"'),
    ],
}

# 文档/脚本里的整句文案
TEXT_FIXES = [
    ("NetOpsTerminal 便携版", "NetOps Terminal 便携版"),
    ("安装 NetOpsTerminal ==", "安装 NetOps Terminal =="),
    ("应用列表里搜 NetOpsTerminal", "应用列表里搜 NetOps Terminal"),
    ("找不到 NetOpsTerminal 可执行文件", "找不到 NetOps Terminal 可执行文件"),
    ("# NetOpsTerminal Linux 桌面版", "# NetOps Terminal Linux 桌面版"),
    ("# NetOpsTerminal 开发文档", "# NetOps Terminal 开发文档"),
    ("AITerminal 保存", "NetOps Terminal 保存"),   # tests/smoke_test.py 断言手册署名
]


def walk():
    for root, dirs, files in os.walk(BASE):
        rel_root = os.path.relpath(root, BASE)
        if rel_root == ".":
            rel_root = ""
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                   and not (rel_root + os.sep).startswith(SKIP_PREFIX_DIRS)]
        if rel_root.startswith(SKIP_REL_PREFIX):
            continue
        for name in files:
            if not name.endswith(EXTS):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, BASE)
            if name in SKIP_FILES or rel in SKIP_FILES or name in SKIP_EXACT:
                continue
            if rel.startswith(SKIP_PREFIX_DIRS):
                continue
            yield path, rel


def rules_for(rel):
    """按文件类型选规则集：文档走"带空格的品牌名"，代码走"无空格的标识"。"""
    if rel.endswith(".md") or rel.endswith(".txt"):
        return ARTIFACT_RULES + LOWER_NAME_RULES + BRAND_RULES_MD
    return ARTIFACT_RULES + LOWER_NAME_RULES + BRAND_RULES_CODE


def rebrand_text(text, rel, report):
    """返回 (新文本, 命中数)。report 收集"规则没命中"的告警。"""
    hits = 0
    for old, new in rules_for(rel):
        n = text.count(old)
        if n:
            text = text.replace(old, new)
            hits += n
    for old, new in TEXT_FIXES:
        n = text.count(old)
        if n:
            text = text.replace(old, new)
            hits += n
    for old, new in DISPLAY_FIXES.get(rel, []):
        n = text.count(old)
        if n:
            text = text.replace(old, new)
            hits += n
        elif new not in text:
            report["unmatched"].append("%s :: %s" % (rel, old[:50]))
    return text, hits


def main():
    apply = "--apply" in sys.argv
    check_only = "--check" in sys.argv
    planned = []
    total_hits = 0
    report = {"unmatched": []}
    seen = set()

    for path, rel in walk():
        seen.add(rel)
        with io.open(path, encoding="utf-8", newline="") as fh:
            orig = fh.read()
        text, hits = rebrand_text(orig, rel, report)
        if hits and text != orig:
            planned.append((rel, hits))
            total_hits += hits
            if apply:
                with io.open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)

    print("%s：%d 个文件 / %d 处命中"
          % ("应用" if apply else "试运行", len(planned), total_hits))
    for rel, hits in sorted(planned, key=lambda t: -t[1]):
        print("  %4d  %s" % (hits, rel))
    if report["unmatched"]:
        print("\n规则没命中（说明目标文本已变，需人工确认）：%d 条" % len(report["unmatched"]))
        for item in report["unmatched"]:
            print("  " + item)
    # 键写错（路径分隔符/拼写）会让整组文案修正静默失效，必须报出来
    missing_keys = [k for k in DISPLAY_FIXES if k not in seen]
    if missing_keys:
        print("\nDISPLAY_FIXES 里这些键在仓库里没找到对应文件：%s" % missing_keys)

    if check_only or not apply:
        leftovers = []
        for path, rel in walk():
            with io.open(path, encoding="utf-8", newline="") as fh:
                for i, line in enumerate(fh, 1):
                    if "AITerminal" in line:
                        leftovers.append((rel, i, line.strip()[:110]))
        print("\n仍含 AITerminal 的行：%d 处" % len(leftovers))
        for rel, i, line in leftovers[:60]:
            print("  %s:%d: %s" % (rel, i, line))
        if len(leftovers) > 60:
            print("  …… 还有 %d 处" % (len(leftovers) - 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
