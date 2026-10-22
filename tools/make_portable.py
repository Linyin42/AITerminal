# -*- coding: utf-8 -*-
"""组装 NetOpsTerminal **便携交付包**（交付机不装 Python，拷到哪都能跑）。

布局（都在 `<out>\\NetOpsTerminal\\` 下）：
    NetOpsTerminal.exe      PyInstaller onefile（build.bat 的产物）
    knowledge/          厂商文档语料：tech/ + chapters/ + commands.sqlite + index.json
    _vendor_cmd/        命令补全数据（h3c/huawei/ruijie）
    local_llm/          随包脚本 —— 打包态的 `--script` 运行器要按**真实文件路径**跑它们
    llm_data/           bin/ + models/ + index/（模型与向量库）
    启动.bat  自检.bat  使用说明.txt

两条不这么做就会踩的坑：
  1. **llm_data 必须在包内，且由 启动.bat 显式设 `AIT_LLM_HOME`。**
     `local_llm/paths.py` 的兜底根是"本子系统目录下的 llm_data"，但它**只在没有 D: 盘时**
     才命中 —— 交付机多半有 D:，于是会去 `D:\\ait_llm` 找模型（那儿什么都没有）。
  2. **knowledge / _vendor_cmd 走 exe 同目录**，不进 onefile 的 `datas`：
     往 onefile 里塞 5GB 会让每次启动都解压一遍（`_MEIPASS` 是临时目录）。

用法：
    python tools\\make_portable.py                    # 只出清单与体积（**默认不复制**）
    python tools\\make_portable.py --go               # 真复制
    python tools\\make_portable.py --go --zip         # 复制 + 打 zip（zip64）
    python tools\\make_portable.py --go --tier mid    # 模型档：minimal(默认)/mid/full
    python tools\\make_portable.py --go --out E:\\交付
退出码：0 = 成功；1 = 前置缺失（如 exe 还没构建）或复制失败
"""
import argparse
import os
import shutil
import sys
import time
import zipfile

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
sys.path.insert(0, os.path.join(P, "local_llm"))

#: 嵌入模型必须带（没有它连索引都建不起来）
EMBED_MODEL = "bge-m3-Q8_0.gguf"
#: 按档选 LLM 权重（键名与 download_models.py / probe_tier.TIERS 一致）
MODEL_TIERS = {
    "minimal": ["Qwen3-4B-Q4_K_M.gguf"],                       # 主力档：3-6GB 显存
    "mid": ["Qwen3-4B-Q4_K_M.gguf", "Qwen3-1.7B-Q8_0.gguf"],   # 加小显存保底
    "full": ["Qwen3-4B-Q4_K_M.gguf", "Qwen3-1.7B-Q8_0.gguf", "Qwen3-8B-Q4_K_M.gguf"],
}
#: llama-server 只带这两个：Vulkan（N/A/Intel 通吃）+ CPU（保底）。
#: CUDA 版仍是 zip（254+373MB，本机实测缺 cudart64_12.dll），要带得先解压并验证。
BIN_DIRS = ["llamacpp-vulkan", "llamacpp-cpu"]
#: 随包脚本（打包态 `--script` 要按真实路径跑它们）
SCRIPTS = ["paths.py", "probe_tier.py", "run_server.py", "verify_delivery.py",
           "retrieve.py", "embed_chunks.py", "build_index.py", "download_models.py",
           # retrieve.py 从**自身目录**读这份提示词（手跑 retrieve.py --ask 时要用；
           # 应用内那条路径走 exe 内 datas 的 _MEIPASS/local_llm，不依赖这里）
           "prompt_local_assistant.txt"]

LAUNCHER = """@echo off
chcp 65001 >nul
REM 便携启动器：显式指定数据根，避免交付机去 D:\\ait_llm 找模型（那儿是空的）
setlocal
set "AIT_LLM_HOME=%~dp0llm_data"
set "AIT_KNOWLEDGE_DIR=%~dp0knowledge"
set "AIT_VENDOR_CMD_DIR=%~dp0_vendor_cmd"
start "" "%~dp0NetOpsTerminal.exe"
endlocal
"""


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def tree_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def build_manifest(args):
    """返回 [(来源路径, 包内相对路径, 是否目录)]；来源不存在则抛 SystemExit。"""
    items = []
    exe = os.path.join(P, "dist", "NetOpsTerminal.exe")
    if not os.path.exists(exe):
        raise SystemExit(f"!! 找不到 {exe}\\n   先构建：build.bat（或 python -m PyInstaller build.spec）")
    items.append((exe, "NetOpsTerminal.exe", False))

    # knowledge（语料，默认仓库根/knowledge，可用 --knowledge 换语料根）+ _vendor_cmd（补全数据）
    # ⚠ 必须先校验存在性：knowledge 若不检查，`--go` 会先复制完 exe 才在 copytree 里抛原生
    # FileNotFoundError，留下一个只含 exe 的**半成品目录**（2026-10-17 端到端测试发现）。
    if not os.path.isdir(args.knowledge):
        raise SystemExit(f"!! 找不到语料目录 {args.knowledge}\n"
                         f"   用 --knowledge <目录> 指定；默认位置是 <仓库根>\\knowledge")
    items.append((args.knowledge, "knowledge", True))
    vendor_cmd = os.path.join(P, "_vendor_cmd")
    if not os.path.isdir(vendor_cmd):
        # 生成侧（tools/doc_to_commands.py 等）按历史约定写在用户目录下
        vendor_cmd = os.path.join(os.path.expanduser("~"), "deepseek_harness", "_vendor_cmd")
    if not os.path.isdir(vendor_cmd):
        raise SystemExit(f"!! 找不到 _vendor_cmd（也试过 {vendor_cmd}）")
    items.append((vendor_cmd, "_vendor_cmd", True))

    # 随包脚本（缺失就报出来，别静默少带一个文件）
    missing = [n for n in SCRIPTS if not os.path.exists(os.path.join(P, "local_llm", n))]
    if missing:
        print(f"⚠ 随包脚本缺失、已跳过：{'、'.join(missing)}")
    for name in SCRIPTS:
        src = os.path.join(P, "local_llm", name)
        if os.path.exists(src):
            items.append((src, os.path.join("local_llm", name), False))

    # llm_data：bin（两个构建）+ models（按档）+ index（全量）
    llm_home = args.llm_home
    for d in BIN_DIRS:
        src = os.path.join(llm_home, "bin", d)
        if not os.path.isdir(src):
            raise SystemExit(f"!! 缺少 llama-server 构建 {src}")
        items.append((src, os.path.join("llm_data", "bin", d), True))
        # VC 运行库必须**和 llama-server.exe 放同一目录**（2026-10-17 在成品包上实测的因果链）：
        # 应用起本地模型时是让 llama-server 以 DETACHED_PROCESS 常驻，它会通过"父进程的
        # DLL 目录"从 onefile 的 `<TEMP>\_MEIxxxx` 解析 VCRUNTIME140*.dll ⇒ 它运行期间锁住
        # 那个临时解包目录 ⇒ 引导器退出时清不掉、**残留一个 12MB 的 NetOpsTerminal.exe 常驻**
        # （实测该进程 _PYI_PARENT_PROCESS_LEVEL=0、卡在清理重试；删掉 _MEI 里这两个 dll 才解锁）。
        # 放到 bin/ 后"应用自身目录"优先于父进程注入的目录，子进程改从 bin/ 加载 ⇒ 引导器
        # 正常收尾（实测残留 = 0）。附带好处：交付机没装 VC++ 运行库也能起 llama-server。
        for dll in ("VCRUNTIME140.dll", "VCRUNTIME140_1.dll"):
            dsrc = os.path.join(sys.base_prefix, dll)      # 与 PyInstaller 打进 _MEIPASS 的同源
            if os.path.exists(dsrc):
                items.append((dsrc, os.path.join("llm_data", "bin", d, dll), False))
            else:
                print(f"⚠ 找不到 {dsrc}：交付机需自备 VC++ 运行库")
    for model in [EMBED_MODEL] + MODEL_TIERS[args.tier]:
        src = os.path.join(llm_home, "models", model)
        if not os.path.exists(src):
            raise SystemExit(f"!! 缺少模型 {src}\\n   补下：python local_llm\\download_models.py core")
        items.append((src, os.path.join("llm_data", "models", model), False))
    idx = os.path.join(llm_home, "index")
    if not os.path.isdir(idx):
        raise SystemExit(f"!! 缺少索引目录 {idx}")
    keep = ("_chunks.jsonl", "_emb.f16", "_emb.meta.json")
    dropped = [n for n in sorted(os.listdir(idx)) if not n.endswith(keep)]
    if dropped:
        # 白名单是刻意的（只带检索/自检要用的三类），但必须**说出来**——
        # 静默少带文件是"包里缺东西却没人知道"的典型来源。
        print(f"ℹ index 里按后缀白名单未纳入包：{'、'.join(dropped)}")
    for name in sorted(os.listdir(idx)):
        if name.endswith(keep):
            items.append((os.path.join(idx, name), os.path.join("llm_data", "index", name), False))
    return items


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:                                       # noqa: BLE001
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"D:\ait_delivery", help="输出根目录（默认 D:\\ait_delivery）")
    ap.add_argument("--llm-home", default=r"D:\ait_llm", help="模型与索引的来源根")
    ap.add_argument("--knowledge", default=os.path.join(P, "knowledge"),
                    help="厂商语料目录（默认 <仓库根>/knowledge）")
    ap.add_argument("--tier", choices=sorted(MODEL_TIERS), default="minimal")
    ap.add_argument("--go", action="store_true", help="真复制（默认只出清单）")
    ap.add_argument("--zip", action="store_true", help="复制完再打 zip64（耗时、占额外空间）")
    args = ap.parse_args()

    dest_root = os.path.join(args.out, "NetOpsTerminal")
    items = build_manifest(args)

    print("=== 便携包清单 ===")
    print(f"    {'来源':<64} {'包内':<40} 体积")
    total = 0
    for src, rel, is_dir in items:
        size = tree_size(src) if is_dir else os.path.getsize(src)
        total += size
        print(f"    {src:<64} {rel:<40} {human(size):>10}")
    print(f"\n    合计（不含生成的 bat/txt）：{human(total)}")
    print(f"    输出：{dest_root}")

    if not args.go:
        print("\n（这是**清单模式**，什么都没复制。要真复制请加 --go）")
        return 0

    if os.path.exists(dest_root) and os.listdir(dest_root):
        print(f"\n⚠ {dest_root} 已存在且非空 —— 只覆盖同名项，不删多余文件")
    t0 = time.time()
    os.makedirs(dest_root, exist_ok=True)
    for src, rel, is_dir in items:
        dst = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if is_dir:
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        print(f"  ✓ {rel}")

    # 清掉运行/测试残留，别把"打包机跑过的痕迹"发给客户：
    #   local_llm/_llama_server.log ← 起停本地模型时 llama-server 写的运行日志
    #   local_llm/__pycache__/      ← 有人对包内脚本跑过 py_compile / 直接 python 执行
    for name in ("_llama_server.log", "_llama_embed.log", "_llama_server.pid"):
        junk = os.path.join(dest_root, "local_llm", name)
        if os.path.exists(junk):
            os.remove(junk)
            print(f"  · 已清运行残留 local_llm\\{name}")
    # 根目录的运行产物：`--script` 的失败日志、RAG 降级留痕、自检报告 —— 都是**跑出来的**，
    # 不该随包发（2026-10-17 发现包内混入 2KB 测试日志，故加入清理）。
    for name in ("aiterminal-script.log", "aiterminal-rag.log", "交付自检报告.txt"):
        junk = os.path.join(dest_root, name)
        if os.path.exists(junk):
            os.remove(junk)
            print(f"  · 已清运行残留 {name}")
    # 跑过随包脚本的目录都会长出 __pycache__（`import paths` 就会）—— 一并清掉
    for rel in ("local_llm", os.path.join("data_tools", "chunk_embed"),
                os.path.join("data_tools", "raw_to_tech")):
        junk_dir = os.path.join(dest_root, rel, "__pycache__")
        if os.path.isdir(junk_dir):
            shutil.rmtree(junk_dir, ignore_errors=True)
            print(f"  · 已清 {rel}\\__pycache__\\")

    # 生成的三个小文件
    with open(os.path.join(dest_root, "启动.bat"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(LAUNCHER)
    # ⚠ 这一段**不能**调系统 `python`：交付机不保证装了 Python（P5-2 审查指出）。
    # 走 exe 自带的 `--script` 通道；exe 是 windowed 无控制台 ⇒ 明细靠 --report
    # 落文件再 `type` 出来（这样"有没有控制台"都能看到结果）。
    check = (
        "@echo off\nchcp 65001 >nul\n"
        "setlocal\n"
        "set \"AIT_LLM_HOME=%~dp0llm_data\"\n"
        "set \"AIT_KNOWLEDGE_DIR=%~dp0knowledge\"\n"
        "set \"AIT_VENDOR_CMD_DIR=%~dp0_vendor_cmd\"\n"
        "echo === 交付自检（明细同时写入 交付自检报告.txt）===\n"
        "\"%~dp0NetOpsTerminal.exe\" --script \"%~dp0local_llm\\verify_delivery.py\""
        " --report \"%~dp0交付自检报告.txt\"\n"
        "echo.\n"
        "type \"%~dp0交付自检报告.txt\"\n"
        "pause\n"
    )
    with open(os.path.join(dest_root, "自检.bat"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(check)
    with open(os.path.join(dest_root, "使用说明.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "NetOps Terminal 便携版\n"
            "=================\n"
            "1) 双击「启动.bat」运行（它会指定数据根，别直接双击 exe —— 直接双击会去找 D:\\ait_llm）。\n"
            "2) 首次使用先跑「自检.bat」：核对 llama-server、模型字节、本机档位与向量库。\n"
            "3) 换盘/换目录：整个文件夹拷走即可（不要拆散 knowledge / llm_data）。\n"
            "4) 想改数据位置：编辑「启动.bat」里的 AIT_LLM_HOME / AIT_KNOWLEDGE_DIR。\n"
            "5) 卸载：停掉本地模型（AI 设置 → 停止本地模型）后删掉整个文件夹，无残留进程。\n"
            "6) 资料要怎么整理才能被 AI 查到：看《资料导入规范.md》"
            "（也可在「资料管理 → 📖 导入规范」里打开），可整份交给别的 AI 照着准备资料。\n"
            "7) 要拿新资料训练/入库：把「data_tools」整个文件夹交给别的 AI（里面"
            "《给AI的说明.md》是操作手册，含可直接粘贴的提示词；脚本可用本包 exe 跑，"
            "交付机不需要装 Python）。\n"
            "   ⚠ 传给 exe 的脚本路径与厂商名请用 ASCII —— 实测 --script 通道对非 ASCII 路径不工作。\n"
            "8) 让 DSH 等本机智能体接管这个界面（连终端会话一起）：先在「工具 → AI 设置 →\n"
            "   外部接管」里打开开关（默认关；只绑 127.0.0.1，令牌可在同一处复制/重置，\n"
            "   发给设备的每条命令都写审计日志）。然后在 DSH 的 profile 补丁\n"
            "   cordis.patch.yml 末尾加一条，再重启 DSH 桌面客户端：\n"
            "     - insert:\n"
            "         - id: mcp-aiterminal\n"
            "           name: '@deepseek-ai/dsh-mcp-client'\n"
            "           config:\n"
            "             serverName: aiterminal\n"
            "             transport: stdio\n"
            "             command: '<本包目录>\\NetOpsTerminal.exe'\n"
            "             args: ['--script', 'data_tools\\mcp\\aiterminal_mcp.py']\n"
            "             cwd: '<本包目录>'\n"
            "             toolCallTimeoutMs: 120000\n"
            "   接上后 DSH 里会多出 10 个 mcp__aiterminal__ 工具（ait_open / ait_send /\n"
            "   ait_read / ait_knowledge_search …）：开 SSH/Telnet/串口会话、下发命令读回显、\n"
            "   查本地资料库。⚠ 这个通道的能力等于人在终端里敲命令，令牌不要外发。\n")
    # 《资料导入规范》随包发布（2026-10-17）：资料管理里点「📖 导入规范」要能打开；
    # 也方便把这份规范直接交给别的 AI 照着准备资料。母本在仓库 docs\。
    spec_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "docs", "资料导入规范.md")
    if os.path.exists(spec_src):
        shutil.copy2(spec_src, os.path.join(dest_root, "资料导入规范.md"))
        print("  ✓ 资料导入规范.md")
    else:
        print("  ⚠ 缺 docs\\资料导入规范.md —— 包里不会有导入规范")

    # 数据工具随包发布（2026-10-17）：用户要能把"原始资料 → 可入库资料"的**脚本连同说明**
    # 一起交给别的 AI。脚本每次出包都从仓库重拷 ⇒ 与仓库永远同步；说明与示例的母本在
    # 仓库 docs\数据工具\；《资料导入规范》也放一份进本目录，好整份交出（自包含）。
    # 目录名**必须全 ASCII**：2026-10-17 实测 `NetOpsTerminal.exe --script "数据工具\切片与向量\x.py"`
    # rc=1 零输出，同一个脚本换成 `local_llm\x.py` 就 rc=0 ⇒ exe 的 --script 通道不认非 ASCII 路径。
    _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tool_root = os.path.join(dest_root, "data_tools")
    # 旧版本用过中文目录名「数据工具」：留着会在包里多一份过时脚本，删掉
    _stale = os.path.join(dest_root, "数据工具")
    if os.path.isdir(_stale):
        shutil.rmtree(_stale, ignore_errors=True)
        print("  · 已清掉旧的中文目录 数据工具\\")
    _stage = (
        ("raw_to_tech", "tools", ("build_tech_docs.py", "hdx_to_tech.py",
                                  "hdx_records_build.py", "doc_to_commands.py",
                                  "fetch_vendor_docs.py", "gen_tech_summary.py")),
        ("chunk_embed", "local_llm", ("build_index.py", "embed_chunks.py", "_embed_chain.py",
                                      "check_vectors.py", "paths.py", "retrieve.py")),
        # 外部接管用的 MCP 桥（纯标准库）：交付机上用 NetOpsTerminal.exe --script 跑它，
        # 于是**不需要**交付机装 Python。DSH 侧的接法见包内 使用说明.txt。
        ("mcp", os.path.join("data_tools", "mcp"), ("aiterminal_mcp.py",)),
    )
    os.makedirs(tool_root, exist_ok=True)
    n_tool = 0
    for sub, srcdir, names in _stage:
        os.makedirs(os.path.join(tool_root, sub), exist_ok=True)
        for name in names:
            src = os.path.join(_repo, srcdir, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tool_root, sub, name))
                n_tool += 1
            else:
                print(f"  ⚠ 缺 {srcdir}\\{name} —— data_tools 里少一个脚本")
    docs_src = os.path.join(_repo, "docs", "data_tools")
    if os.path.isdir(docs_src):
        shutil.copytree(docs_src, tool_root, dirs_exist_ok=True)
    else:
        print("  ⚠ 缺 docs\\data_tools\\ —— 包里没有给 AI 的说明与示例")
    spec_src2 = os.path.join(_repo, "docs", "资料导入规范.md")
    if os.path.exists(spec_src2):
        shutil.copy2(spec_src2, os.path.join(tool_root, "资料导入规范.md"))
    # 三个双击就跑的小 bat（路径全 ASCII；用法：把厂商名当参数传）
    for bat, script, extra_args in (("run_build_index.bat", "chunk_embed\\build_index.py", "--vendor %V% --chunk"),
                                    ("run_embed_chunks.bat", "chunk_embed\\embed_chunks.py", "--vendor %V%"),
                                    ("run_check_vectors.bat", "chunk_embed\\check_vectors.py", "%V%")):
        with open(os.path.join(tool_root, bat), "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write("@echo off\nchcp 65001 >nul\nsetlocal\n"
                     "if not \"%~1\"==\"\" (set \"V=%~1\") else (set /p \"V=厂商名(h3c/huawei/ruijie/tplink): \")\n"
                     "if \"%V%\"==\"\" (echo 未给厂商名，退出 & pause & exit /b 2)\n"
                     "set \"AIT_LLM_HOME=%~dp0..\\llm_data\"\n"
                     f"echo === {script}  厂商=%V% ===\n"
                     f"\"%~dp0..\\NetOpsTerminal.exe\" --script \"%~dp0{script}\" {extra_args}\n"
                     "echo.\necho 退出码=%ERRORLEVEL%\npause\n")
    print(f"  ✓ data_tools\\（{n_tool} 个脚本 + 3 个 bat + 给AI的说明.md + sample + 资料导入规范.md）")

    print("  ✓ 启动.bat / 自检.bat / 使用说明.txt")

    actual = tree_size(dest_root)
    print(f"\n复制完成：{human(actual)}，用时 {time.time() - t0:.0f} 秒")
    print(f"  实测/预计 偏差 {abs(actual - total) / max(total, 1) * 100:.2f}%")

    if args.zip:
        zip_path = os.path.join(args.out, "NetOpsTerminal-便携.zip")
        tz = time.time()
        print(f"\n打包 zip64 → {zip_path}（{human(actual)}，会压很久）")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                             allowZip64=True, compresslevel=6) as zf:
            for root, _dirs, files in os.walk(dest_root):
                for f in files:
                    full = os.path.join(root, f)
                    zf.write(full, os.path.relpath(full, args.out))
        print(f"  ✓ {zip_path}  {human(os.path.getsize(zip_path))}"
              f"  用时 {time.time() - tz:.0f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
