# -*- coding: utf-8 -*-
"""本地 LLM 子系统的**目录布局**——全子系统里唯一一处定义这些路径。

为什么要单独一个文件（而不是各脚本各写一遍）：
  · 这套东西要**拷到交付机**上跑：模型 ~5GB + 向量库 ~0.5GB + llama.cpp 二进制 ~300MB。
    交付机的盘符未必与本机相同，而原先 `r"D:\\ait_llm\\..."` 硬编码在 **8 个脚本、17 处**，
    要换盘就得 8 处同改、漏一处就是"模型/二进制找不到"（同类"同一事实两处写"项目里已被咬过）。
  · 收口到这里之后，换盘只需设一个环境变量，或让下面第 3 条兜底自动生效。

解析优先级（第一个可用者胜）：
  1) 环境变量 `AIT_LLM_HOME`  —— 显式指定（部署/多套环境切换最省事）
  2) `D:\\ait_llm`            —— 本机与交付机的默认（用户要求：模型与索引放 D 盘）
  3) `<本子系统目录>/llm_data` —— **没有 D: 盘时的兜底**，保证换台机器也能跑起来
     注意这是"降级可用"而非"静默搬家"：调用 `describe()`/打印一次，让人知道实际用了哪个。

子目录约定（三个都是相对 ROOT）：
    bin/    llama-server.exe 的各后端构建：llamacpp-vulkan / llamacpp-cuda / llamacpp-cpu
    models/ GGUF 模型
    index/  切片 jsonl 与向量库（<vendor>_chunks.jsonl、<vendor>_emb.f16、meta.json）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: 默认根（本机与交付机）
DEFAULT_ROOT = r"D:\ait_llm"
#: 没有 D: 盘时的兜底根（就在本子系统目录下，随包一起走）
FALLBACK_ROOT = os.path.join(HERE, "llm_data")


def _portable_root():
    """便携包自带的 `llm_data`（**仅打包态**、且确实存在时才返回；否则空串）。

    为什么必须有这一步：交付机**多半有 D: 盘**，而下面那条判据是"盘符存在就用
    D:\\ait_llm" ⇒ 便携包会被送去一个空的 D:\\ait_llm，模型永远找不到
    （"拷到哪都能跑"直接报废）。源码态不干预，行为与改造前完全一致。
    """
    if not getattr(sys, "frozen", False):
        return ""
    root = os.path.dirname(HERE)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from core import app_paths
        candidate = os.path.join(app_paths.base_dir(), "llm_data")
    except Exception:                                            # noqa: BLE001
        return ""
    return candidate if os.path.isdir(candidate) else ""


def _pick_root(env=None):
    """按优先级选根目录。返回 (root, 来源说明)。"""
    env = os.environ if env is None else env
    override = (env.get("AIT_LLM_HOME") or "").strip()
    if override:
        return override, "环境变量 AIT_LLM_HOME"
    portable = _portable_root()
    if portable:
        return portable, "便携包自带的 llm_data（exe 同目录）"
    # 用"磁盘根是否存在"判断，而不是"目录存不存在"：本机全新部署时目录还没建，
    # 但只要 D: 在，就该用它（否则会先落到 llm_data，装完模型才发现用错了地方）。
    drive = os.path.splitdrive(DEFAULT_ROOT)[0] + os.sep
    if os.path.exists(drive):
        return DEFAULT_ROOT, f"默认位置（{drive} 存在）"
    return FALLBACK_ROOT, "本机没有 D: 盘 → 兜底到本子系统目录下的 llm_data"


ROOT, ROOT_SOURCE = _pick_root()
BIN = os.path.join(ROOT, "bin")
MODELS = os.path.join(ROOT, "models")
INDEX = os.path.join(ROOT, "index")

#: llama-server 的候选构建，按优先级：Vulkan（N/A/Intel 通吃）→ CUDA → CPU（保底）
SERVER_CANDIDATES = [
    os.path.join(BIN, "llamacpp-vulkan", "llama-server.exe"),
    os.path.join(BIN, "llamacpp-cuda", "llama-server.exe"),
    os.path.join(BIN, "llamacpp-cpu", "llama-server.exe"),
]


def server_exe():
    """找可用的 llama-server.exe：先按候选顺序，再扫 llamacpp-* 目录。"""
    for path in SERVER_CANDIDATES:
        if os.path.exists(path):
            return path
    import glob
    for path in sorted(glob.glob(os.path.join(BIN, "llamacpp-*", "llama-server.exe"))):
        return path
    return None


def describe():
    """一行说明当前用了哪个根 —— 界面/日志里打一次，避免"静默用了兜底路径"。"""
    return f"本地 LLM 根目录 {ROOT}（{ROOT_SOURCE}）"


def script_cmd(py_file, *args):
    """跑本子系统某个脚本的命令行 —— **唯一实现**在 `core/app_paths.script_cmd()`。

    为什么这里要转一层、而不是各脚本自己拼 `[sys.executable, 脚本]`：
      打包（PyInstaller onefile）后 `sys.executable` 是 **AITerminal.exe 本身**，
      `[exe, "run_server.py"]` 会被当成"再开一个应用"，而永远不会跑那个脚本。
      这个坑只在一处修（`core/app_paths.py` 有完整说明与测试），别在每个脚本里各写一遍。

    本文件被 `local_llm/` 下的脚本导入时，仓库根不在 sys.path 上（那些脚本跑起来时
    `sys.path[0]` 是脚本所在目录 = 本目录），所以先补一下再转调。
    """
    root = os.path.dirname(HERE)
    if root not in sys.path:
        sys.path.insert(0, root)
    from core import app_paths
    return app_paths.script_cmd(py_file, *args)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    print(describe())
    print(f"  bin    = {BIN}")
    print(f"  models = {MODELS}")
    print(f"  index  = {INDEX}")
    print(f"  可用 llama-server = {server_exe()}")
    for name, path in (("bin", BIN), ("models", MODELS), ("index", INDEX)):
        print(f"  {name:6s} 存在={os.path.isdir(path)}")
