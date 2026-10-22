# -*- coding: utf-8 -*-
"""给 DSH 的 desktop profile 补一条 MCP 客户端条目 —— 让 DSH 能驱动 NetOpsTerminal。

用法（本机开发环境，用系统 Python 跑桥）：
    python tools\\setup_dsh_mcp.py
交付机 / 打包态（用 NetOpsTerminal.exe 的 --script 通道跑同一个桥）：
    python tools\\setup_dsh_mcp.py --exe "D:\\ait_delivery\\NetOpsTerminal\\NetOpsTerminal.exe"

做的事：
    1. 备份 ~/.dsh/profiles/desktop/cordis.patch.yml（同名 .bak-<日期时间>）
    2. 没有 mcp-aiterminal 就追加一条 `- insert:`；有就原地替换（幂等）
    3. 用 yaml 校验整份文件；若原文含自定义标签（!!js 之类）导致整份解析失败，
       退化为只校验新插入的那一块，并把原因打出来
    4. 打印"要重启桌面客户端才生效"的提醒

为什么要 MCP：DSH 侧的接入点就是 dsh-mcp-client（插条目即可，不用装包、不用写插件），
工具名会变成 mcp__aiterminal__<工具名>。
"""
import argparse
import datetime
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATCH = os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop",
                             "cordis.patch.yml")
DEFAULT_PYTHON = sys.executable
DEFAULT_BRIDGE = os.path.join(BASE, "data_tools", "mcp", "aiterminal_mcp.py")
MARKER = "mcp-aiterminal"


def build_block(command: str, args: list, cwd: str) -> str:
    lines = [
        "- insert:",
        f"    - id: {MARKER}",
        "      name: '@deepseek-ai/dsh-mcp-client'",
        "      config:",
        "        serverName: aiterminal",
        "        transport: stdio",
        f"        command: '{command}'",
    ]
    if len(args) == 1:
        lines.append(f"        args: ['{args[0]}']")
    else:
        lines.append("        args:")
        for a in args:
            lines.append(f"          - '{a}'")
    lines += [
        f"        cwd: '{cwd}'",
        "        toolCallTimeoutMs: 120000",
        "        failOnStartupError: true",
    ]
    return "\n".join(lines) + "\n"


def find_block(lines: list):
    """找包含 MARKER 的那个顶层列表项（行区间 [start, end)）"""
    for i, line in enumerate(lines):
        if not line.startswith("- "):
            continue
        window = "".join(lines[i:i + 14])
        if MARKER in window:
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("- ") or lines[j].startswith("#"):
                    end = j
                    break
            return i, end
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patch", default=DEFAULT_PATCH)
    ap.add_argument("--python", default=DEFAULT_PYTHON, help="跑桥的解释器")
    ap.add_argument("--bridge", default=DEFAULT_BRIDGE, help="MCP 桥脚本路径")
    ap.add_argument("--exe", default="", help="改用 exe（--script 通道）跑桥")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.patch):
        print(f"找不到 profile 补丁文件：{a.patch}")
        return 1
    if not a.exe and not os.path.exists(a.python):
        print(f"找不到 Python：{a.python}")
        return 1
    if not os.path.exists(a.bridge):
        print(f"找不到 MCP 桥脚本：{a.bridge}")
        return 1

    if a.exe:
        block = build_block(a.exe, ["--script", os.path.relpath(a.bridge, BASE)], BASE)
        how = f"{a.exe} --script {os.path.relpath(a.bridge, BASE)}"
    else:
        block = build_block(a.python, ["-u", a.bridge], BASE)
        how = f"{a.python} -u {a.bridge}"
    print(f"接入方式：{how}")

    with open(a.patch, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines(keepends=True)
    hit = find_block(lines)
    if hit:
        start, end = hit
        lines[start:end] = [block]
        action = f"替换第 {start + 1}~{end} 行（原本已有 {MARKER}）"
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(block)
        action = "在末尾追加"
    new_text = "".join(lines)

    try:
        import yaml
        try:
            yaml.safe_load(new_text)
            print("整份 YAML 校验：通过")
        except Exception as e:
            yaml.safe_load(block)
            print(f"整份 YAML 校验：跳过（原文有自定义标签？{type(e).__name__}: {str(e)[:80]}）"
                  f"；新块单独校验：通过")
    except ImportError:
        print("没装 pyyaml，跳过语法校验（只做了文本替换）")

    if a.dry_run:
        print(f"[dry-run] 将要{action}，未写盘。新块如下：\n{block}")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{a.patch}.bak-{stamp}"
    shutil.copy2(a.patch, backup)
    with open(a.patch, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_text)
    print(f"{action} 完成；备份：{backup}")
    print("接着：重启 DSH 桌面客户端，之后会看到工具 mcp__aiterminal__ait_send 等 10 个。")
    print("注意：NetOpsTerminal 要先在「工具 → AI 设置 → 外部接管」里打开开关。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
