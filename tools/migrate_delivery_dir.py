# -*- coding: utf-8 -*-
"""交付目录改名：D:\\ait_delivery\\AITerminal → NetOpsTerminal，并把包内 exe/脚本/桥换成新名字。

为什么这么做（而不是重新跑 make_portable 打包）：
  交付目录里 6.4 GB 的 knowledge/ 与 llm_data/ 没必要重拷一遍；改名 + 换 exe + 改脚本引用，
  内容与校验单都能对上。旧 exe 保留成 .bak，不删。

跑法：python -u tools/migrate_delivery_dir.py [--apply]     # 默认试运行
"""
import io
import json
import os
import shutil
import subprocess
import sys

OLD = r"D:\ait_delivery\AITerminal"
NEW = r"D:\ait_delivery\NetOpsTerminal"
REPO = r"C:\Users\linyin\WorkBuddy AI\2026-09-21-18-54-37\aiterminal"
NEW_EXE_SRC = os.path.join(REPO, "dist", "NetOpsTerminal.exe")
STAMP = "20261022-1830"

# 交付目录里需要换名字的文本文件（相对路径）
TEXT_FILES = [
    "启动.bat", "自检.bat", "换成正式包并刷新图标.bat", "使用说明.txt",
    "连接模拟器操作文档.md", "测试记录-2026-10-19.md", "资料导入规范.md",
    os.path.join("data_tools", "给AI的说明.md"),
]
REPLACES = [
    ("AITerminal.exe", "NetOpsTerminal.exe"),
    ("AITerminal 便携版", "NetOps Terminal 便携版"),
    ("AITerminal 桌面程序", "NetOps Terminal 桌面程序"),
    ("启动 AITerminal", "启动 NetOps Terminal"),
    ("关闭 AITerminal", "关闭 NetOps Terminal"),
    ("双击 AITerminal", "双击 NetOps Terminal"),
]


def read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def main():
    apply = "--apply" in sys.argv
    if not os.path.isdir(OLD):
        print("源目录不存在：%s" % OLD)
        return 2
    if os.path.exists(NEW):
        print("目标已存在，拒绝覆盖：%s" % NEW)
        return 2

    # 1) 进程占用检查
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq NetOpsTerminal.exe", "/NH"],
                         capture_output=True, text=True, errors="ignore").stdout
    if "NetOpsTerminal.exe" in out:
        print("NetOpsTerminal.exe 还在运行，先关掉再改")
        return 2

    plan = ["重命名目录 %s -> %s" % (OLD, NEW),
            "拷入新 exe %s" % NEW_EXE_SRC,
            "旧 AITerminal.exe 改名为 AITerminal.exe.bak-%s" % STAMP,
            "覆盖包内 MCP 桥（用仓库里改名后的版本）"]
    for rel in TEXT_FILES:
        p = os.path.join(OLD, rel)          # 试运行时目录还没改名，先在旧路径上数
        if os.path.exists(p):
            txt, enc = read_text(p)
            n = sum(txt.count(a) for a, _ in REPLACES)
            plan.append("改文案 %-28s %2d 处 (%s)" % (rel, n, enc))
    for line in plan:
        print("  " + line)
    if not apply:
        print("\n试运行结束（加 --apply 才真改）")
        return 0

    os.rename(OLD, NEW)
    print("已重命名目录")

    shutil.copy2(NEW_EXE_SRC, os.path.join(NEW, "NetOpsTerminal.exe"))
    print("已拷入 NetOpsTerminal.exe (%.1f MB)"
          % (os.path.getsize(os.path.join(NEW, "NetOpsTerminal.exe")) / 1048576))

    old_exe = os.path.join(NEW, "AITerminal.exe")
    if os.path.exists(old_exe):
        os.rename(old_exe, old_exe + ".bak-" + STAMP)
        print("旧 exe 已备份为 AITerminal.exe.bak-%s" % STAMP)

    bridge_src = os.path.join(REPO, "data_tools", "mcp", "aiterminal_mcp.py")
    shutil.copy2(bridge_src, os.path.join(NEW, "data_tools", "mcp", "aiterminal_mcp.py"))
    print("已覆盖包内 MCP 桥")

    for rel in TEXT_FILES:
        p = os.path.join(NEW, rel)
        if not os.path.exists(p):
            continue
        txt, enc = read_text(p)
        before = txt
        for a, b in REPLACES:
            txt = txt.replace(a, b)
        if txt != before:
            io.open(p, "w", encoding=enc, newline="").write(txt)
            print("  已改 %s (%s)" % (rel, enc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
