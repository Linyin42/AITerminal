"""扫源码里所有 import，列出当前环境缺哪些第三方包。

用法: .venv/bin/python tools/deps_check.py
退出码: 0=没有缺失；1=有缺失（会逐行打印）
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "build", "dist", "__pycache__", "knowledge", ".venv", ".venv-linux",
             ".venv-train", "design", "docs", "_notes", "cpp", "data_tools"}
STDLIB = set(sys.stdlib_module_names) | {"__future__"}
LOCAL_TOPS = set()


def top_of(path: str) -> str:
    name = os.path.splitext(os.path.basename(path))[0]
    return name


def main() -> int:
    py_files = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(base, f))
    # 本项目的顶层包/模块（相对 Windows 源码根）
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for d in dirs:
            if os.path.exists(os.path.join(base, d, "__init__.py")):
                LOCAL_TOPS.add(d)
        for f in files:
            if f.endswith(".py"):
                LOCAL_TOPS.add(top_of(f))

    imported = {}
    for path in py_files:
        try:
            tree = ast.parse(open(path, encoding="utf-8", errors="replace").read(), path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported.setdefault(a.name.split(".")[0], set()).add(path)
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                imported.setdefault(node.module.split(".")[0], set()).add(path)

    missing = []
    for mod, users in sorted(imported.items()):
        if mod in STDLIB or mod in LOCAL_TOPS:
            continue
        try:
            __import__(mod)
        except Exception as exc:                              # noqa: BLE001
            missing.append((mod, len(users), sorted(users)[0], repr(exc)))

    if not missing:
        print(f"OK：{len(imported)} 个顶层 import 全部可解析")
        return 0
    print(f"缺失 {len(missing)} 个第三方包：")
    for mod, n, sample, err in missing:
        print(f"  {mod:<18} 被 {n:>2} 个文件引用   例: {os.path.relpath(sample, ROOT)}  ({err})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
