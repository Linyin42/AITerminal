# -*- coding: utf-8 -*-
"""**"数据放哪 / 脚本怎么跑"两态的唯一判据**：源码运行态 vs 打包（PyInstaller）运行态。

为什么必须单独一个文件（而不是各模块自己判断）：
  `sys.frozen` 这类判断一旦在多处各写一遍，就会出现"一半模块认得打包态、一半不认"的
  半成品状态 —— 打包后的表现是"有的功能找得到数据、有的找不到"，而且极难定位。
  本项目的"同一事实两处写"已经因此返工过多次（见 REFERENCE §2.15）。

两条容易踩的坑（都在这里一次性处理掉）：
  1. **onefile 的 `_MEIPASS` 不是数据目录**：它是每次启动解压出来的临时目录，
     进程退出即删。往那儿找 5GB 的 `knowledge/`（或写任何东西）都会失败。
     所以打包态一律以 **exe 所在目录**为基准。
  2. **打包态下 `sys.executable` 是应用本体**，不是 python 解释器：
     `[sys.executable, "某个脚本.py"]` 会被当成"再开一个应用"（见 `script_cmd()`）。

用法：
    from core import app_paths
    app_paths.base_dir()                      # 基准目录（源码态=仓库根；打包态=exe 目录）
    app_paths.data_dir("knowledge", "AIT_KNOWLEDGE_DIR")
    app_paths.data_dir("_vendor_cmd", "AIT_VENDOR_CMD_DIR", legacy=[_LEGACY_VENDOR_CMD])
"""
import os
import sys


def is_frozen() -> bool:
    """是不是 PyInstaller 打出来的可执行体（onefile / onedir 都算）。"""
    return bool(getattr(sys, "frozen", False))


def base_dir() -> str:
    """随包数据该放在哪的**基准目录**。

    · 源码运行：仓库根（本文件在 `<repo>/core/app_paths.py`，上两级）—— 与改造前完全一致
    · 打包运行：**exe 所在目录**（不是 `_MEIPASS`，理由见模块头第 1 条）
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir(name: str, env: str = "", legacy=()) -> str:
    """取一个数据目录：**返回第一个已存在者**，都不存在时返回首选路径（供报错用）。

    优先级：
      1) 环境变量 `env`（部署/多套环境切换，显式指定最省事）
      2) `<基准目录>/<name>`（交付包：数据就摆在 exe 旁边，换盘/换目录不用改代码）
      3) `legacy` 里的候选（历史约定位置，如 `~/deepseek_harness/_vendor_cmd`）

    ⚠ 用"目录是否存在"筛，而不是无条件信第 2 条：本机（开发机）的数据按历史约定
    放在别处，交付机放 exe 旁边 —— 同一个 exe 在两种机器上都要能用。
    """
    candidates = []
    if env:
        override = (os.environ.get(env) or "").strip()
        if override:
            candidates.append(override)
    candidates.append(os.path.join(base_dir(), name))
    candidates.extend([p for p in legacy if p])
    for path in candidates:
        if os.path.isdir(path):
            return path
    return candidates[0] if candidates else ""


def script_cmd(py_file: str, *args) -> list:
    """把"跑一个随包脚本"表达成命令行。

    · 源码态：`[python.exe, 脚本.py, ...]`
    · 打包态：`[NetOpsTerminal.exe, --script, 脚本.py, ...]`
      —— onefile 下 `sys.executable` 就是应用本体，直接 `[sys.executable, 脚本.py]`
      会被当成"再开一个应用"，而永远不会跑那个脚本。`main.py` 需要在建界面**之前**
      处理 `--script`（用 `runpy.run_path` 跑完就 `sys.exit`）。

    ⚠ 打包态的 exe 是 windowed（无控制台）⇒ **拿不到子进程 stdout**。
    需要"脚本结果回传"的场合（如 `probe_tier.py --json`）不要走子进程，
    改成进程内调用（`import` 后直接调函数）——见 `core/local_model.probe()`。
    """
    if is_frozen():
        return [sys.executable, "--script", py_file, *args]
    return [sys.executable, py_file, *args]


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    print(f"frozen   = {is_frozen()}")
    print(f"base_dir = {base_dir()}")
    print(f"executable = {sys.executable}")
    print(f"knowledge  = {data_dir('knowledge', 'AIT_KNOWLEDGE_DIR')}")
