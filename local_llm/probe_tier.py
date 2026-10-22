# -*- coding: utf-8 -*-
"""探测本机可支撑的本地 LLM 档位（交付机 6-14GB 内存 + 独显 2-6GB）。

为什么要这个脚本：交付机配置参差（内存 6-14GB、独显 2-6GB、厂商未知），
不能把"用哪个模型"写死在代码里 —— 必须启动时探测 + 分档 + 不足就退化。

两条探测依据，**都不用第三方依赖**：
  1) 内存：ctypes GlobalMemoryStatusEx（Windows 原生，免 psutil）
  2) 显存：`llama-server --list-devices`（Vulkan 后端能同时看到 N 卡/A 卡/Intel，
     比 Win32_VideoController.AdapterRAM 可靠 —— 后者在 4GB 处截断）

⚠ 关键坑（实测）：**Intel 核显会把"共享内存"报成显存**
    Vulkan1: Intel(R) Iris(R) Xe Graphics (16227 MiB, 15459 MiB free)
  这是系统内存，不是可用显存。所以判定独显时要**排除 Intel**，否则会误判成
  "显存 15GB，能跑 14B"，实际上只能跑核显那点共享内存。

用法:
    python probe_tier.py                       # 人读
    python probe_tier.py --json                # 给程序读（源码态下 NetOpsTerminal 用的就是这个）
    python probe_tier.py --json --out out.json # 打包态：windowed exe **拿不到子进程 stdout**，
                                               # 调用方只能从 --out 的文件里读回结果
                                               # （见 core/local_model.probe()）

**被 import 时不跑任何探测**：唯一入口是 `collect()`（返回结果 dict），
`main()` 只负责解析参数与输出。判定逻辑与阈值表**只有这一份**，别在别处再抄一遍。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import re
import subprocess
import sys

# 档位规则：**自由显存** >= 权重 + 预留(600MB, 给 KV 与运行时) ⇒ 选该档。
# 权重体积实测：8B-Q4_K_M=4792MB / 4B-Q4_K_M=2386MB / 1.7B-Q8_0=1751MB
# 口径说明：要求"整块装进显存"而不是"部分卸载"——部分卸载速度掉一大截，
# 6GB 卡上"4B 全上卡(快)"优于"8B 半上卡(慢)"。8B 留给显存余量更大的机器。
TIERS = [
    (5392, "high", "Qwen3-8B-Q4_K_M.gguf", 4792, "交付机顶配档"),
    (2986, "mid", "Qwen3-4B-Q4_K_M.gguf", 2386, "★ 交付主力档"),
    # 自训档（QLoRA 于厂商文档语料微调后的 Qwen3-4B）。体积与 mid 同级，故阈值相同；
    # 放在 mid **之后**属"显式 opt-in"（`run_server.py --tier ait`），不动交付默认档 ——
    # 等评测数字证明它不差于 mid 再决定是否前移（`pick_tier()` 取第一个装得下的档）。
    (2986, "ait", "Qwen3-4B-AIT-Q4_K_M.gguf", 2386, "自训厂商文档档（opt-in）"),
    (2351, "low", "Qwen3-1.7B-Q8_0.gguf", 1751, "小显存档"),
]
CPU_TIER = ("cpu", "Qwen3-1.7B-Q8_0.gguf", 1751, "无可用独显：纯 CPU 或退化为纯检索")

#: 权重常驻内存的经验系数：**纯 CPU 档**下进程工作集 ≈ 权重 × 该系数。
#: **实测**（本机 2026-10-16）：Qwen3-1.7B-Q8 权重 1834MB → 稳态工作集 2349MB
#: （系数 1.28）；发一次请求后 2352MB（仅 +3MB）⇒ 上下文(KV)开销很小，主要是权重本身。
RAM_FACTOR = 1.28
#: 除模型外还要留给应用本身/系统/波动的余量（GB）
RAM_RESERVE_GB = 0.8
#: 有独显时权重进显存（-ngl -1），RAM 只装 KV 与缓冲 —— 给一个保守常数即可
RAM_NEED_GPU_GB = 0.6


def ram_need_gb(tier, weight_mb):
    """跑这一档大约要占多少**内存**（GB）。

    ⚠ 必须区分"权重在显存"还是"权重在内存"：有独显时权重进显存，RAM 只留 KV/缓冲；
    只有纯 CPU 档才按 权重×系数 算。把两者混为一谈，会把"6GB 内存 + 2-6GB 独显"的
    交付机误判成跑不动（用户的交付机正是 6-14GB 内存 + 2-6GB 显存这一档）。
    """
    if (tier or "").lower() == "cpu":
        return weight_mb / 1024.0 * RAM_FACTOR
    return RAM_NEED_GPU_GB


def ram_check(tier, weight_mb, avail_ram_gb):
    """内存够不够跑这一档。返回 (ok, need_gb, 说明)。

    不够时**不是**硬失败，而是让上层按项目原则降级：只关 LLM、保留本地检索
    （"能不用 AI 就不用 / 未处理 > 处理错"）。
    """
    need = ram_need_gb(tier, weight_mb)
    avail = float(avail_ram_gb or 0)
    if avail >= need + RAM_RESERVE_GB:
        return True, need, (f"内存够：约需 {need:.1f}GB + 余量 {RAM_RESERVE_GB}GB，"
                            f"当前空闲 {avail:.1f}GB")
    return False, need, (f"内存不够：需约 {need:.1f}GB + 余量 {RAM_RESERVE_GB}GB，"
                         f"当前空闲仅 {avail:.1f}GB ⇒ 建议只跑本地检索")

# 路径的唯一来源见 paths.py（换盘/交付机只改那一处）。本文件无论被
# `python probe_tier.py` 直接跑、还是被 run_server.py 以 sys.path 插入方式导入，
# 所在目录都在 sys.path 上，所以直接 import 即可。
import paths  # noqa: E402

#: 保留旧名以免外部引用断裂；值与 paths.SERVER_CANDIDATES 同源
BIN_GLOBS = list(paths.SERVER_CANDIDATES)


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def ram_gb():
    # Windows 走 GlobalMemoryStatusEx；Linux/macOS 读 /proc/meminfo
    # （ctypes.windll 在非 Windows 上根本不存在，硬调会 AttributeError）。
    if not sys.platform.startswith("win"):
        try:
            info = {}
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    k, _, v = line.partition(":")
                    info[k.strip()] = int(v.split()[0])  # kB
            total = info.get("MemTotal", 0) / 1024 ** 2
            avail = info.get("MemAvailable", info.get("MemFree", 0)) / 1024 ** 2
            return total, avail
        except Exception:                            # noqa: BLE001
            return 0.0, 0.0
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return st.ullTotalPhys / 1024 ** 3, st.ullAvailPhys / 1024 ** 3


def find_server():
    return paths.server_exe()


DEV_RE = re.compile(r"^\s*(\S+):\s+(.+?)\s+\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)\s*$")


def list_devices(server):
    """返回 [{id, name, total_mb, free_mb, is_igpu}]；探测失败返回 []。"""
    if not server:
        return []
    try:
        out = subprocess.run([server, "--list-devices"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=60).stdout or ""
    except Exception:  # noqa: BLE001
        return []
    devs = []
    for line in out.splitlines():
        m = DEV_RE.match(line)
        if not m:
            continue
        dev_id, name, total, free = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        igpu = any(k in name for k in ("Intel", "UHD Graphics", "Iris"))
        devs.append({"id": dev_id, "name": name, "total_mb": total,
                     "free_mb": free, "is_igpu": igpu})
    return devs


def pick_tier(devs, avail_ram_gb):
    """选定档位。返回 (tier, model, weight_mb, reason, device) —— 恒为 5 元组。"""
    dgpus = [d for d in devs if not d["is_igpu"]]
    if not dgpus:
        reason = ("只发现核显（共享内存，不算显存）" if devs
                  else "未发现任何 Vulkan 设备")
        return (CPU_TIER[0], CPU_TIER[1], CPU_TIER[2], reason,
                devs[0]["id"] if devs else None)
    best = max(dgpus, key=lambda d: d["free_mb"])
    for min_free, tier, model, weight_mb, note in TIERS:
        if best["free_mb"] >= min_free:
            return tier, model, weight_mb, note, best["id"]
    return (CPU_TIER[0], CPU_TIER[1], CPU_TIER[2],
            f"独显自由显存仅 {best['free_mb']}MB（需 {TIERS[-1][0]}MB 才够最小档）",
            best["id"])


def collect():
    """跑一次完整探测，把结论组装成 dict —— **唯一一份**（--json / --out / 进程内调用共用）。

    `core/local_model.py` 直接 `import probe_tier` 调这个函数（不走子进程）：打包态
    exe 是 windowed（无控制台），子进程 stdout 拿不到，详见 `core/app_paths.py` 模块头。
    所以本函数的返回值必须**自己就够用**，别把它写进 stdout 才算数。
    """
    total_ram, avail_ram = ram_gb()
    server = find_server()
    devs = list_devices(server)
    tier, model, weight_mb, note, device = pick_tier(devs, avail_ram)
    ram_ok, ram_need, ram_note = ram_check(tier, weight_mb, avail_ram)

    return {
        "ram_total_gb": round(total_ram, 1),
        "ram_avail_gb": round(avail_ram, 1),
        "ram_need_gb": round(ram_need, 2),
        "ram_ok": ram_ok,
        "ram_note": ram_note,
        # 把根目录与模型目录一并报出去，调用方（如 NetOpsTerminal 的 core/local_model.py）
        # 就不用自己再硬编码一份 `D:\ait_llm\models` —— 少一处"同一事实两处写"。
        "root": paths.ROOT,
        "models_dir": paths.MODELS,
        "llama_server": server,
        "devices": devs,
        "tier": tier,
        "model": model,
        "model_weight_mb": weight_mb,
        "device": device,
        "reason": note,
        "n_gpu_layers": (-1 if tier != "cpu" else 0),
        "note": "档位为启发式判定；最终以 llama-server 实际加载结果为准",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="", metavar="文件",
                    help="把同一份 JSON 写到这个文件（打包态无控制台时的唯一回传通道）")
    args = ap.parse_args()

    result = collect()

    if args.out:
        # 先写文件再打印：写失败就报非 0，别让调用方拿到一份"看起来成功"的空结果。
        # 打包态调用方**只认这个文件**（windowed exe 没有 stdout）。
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"内存    : 总 {result['ram_total_gb']:.1f} GB / 空闲 {result['ram_avail_gb']:.1f} GB")
    print(f"llama   : {result['llama_server']}")
    print("设备    :")
    for d in result["devices"]:
        flag = "（核显，共享内存，不计入显存）" if d["is_igpu"] else ""
        print(f"  {d['id']:8s} {d['name']}  {d['total_mb']} MiB (free {d['free_mb']}) {flag}")
    if not result["devices"]:
        print("  （无：--list-devices 失败或无设备）")
    print(f"档位    : {result['tier']}  -> {result['model']}  (权重 {result['model_weight_mb']} MB)")
    print(f"设备选用 : {result['device']}")
    print(f"判定理由 : {result['reason']}")
    print(f"内存闸门 : {result['ram_note']}")
    print(f"-ngl    : {result['n_gpu_layers']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
