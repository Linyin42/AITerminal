# -*- coding: utf-8 -*-
"""开训 / 起模型服务前的**前置检查**（本轮复盘落地的第 1 条纪律）。

为什么要有它
============
2026-10-23 这一轮，训练刚起就掉到 6 s/步（基线 1.30 s/步）：原因是 `embed_chunks.py`
自动拉起的 bge-m3 嵌入服务还挂在 :8081，占约 1 GB 显存。6 GB 卡上 5.9/6.1 GB 就进
WDDM 换页区（GPU 100% 但功耗只 60–90 W），吞吐掉 4–10 倍。
**问题不在训练脚本，在我起训练前没确认"卡上还有谁"。**

用法：
    python tools\\preflight_gpu.py                 # 报告：端口 / 显存 / 在训进程
    python tools\\preflight_gpu.py --for-train     # 训练前：:8081 有人就视为阻塞（返回 1）
    python tools\\preflight_gpu.py --for-serve     # 起模型前：有训练在跑就视为阻塞

退出码：0 = 可以做；1 = 有阻塞（打印谁在占）。
"""
import argparse
import os
import re
import subprocess
import sys
import urllib.request

GPU_MIN_FREE_MB = 4800          # 6 GB 卡实测：训练峰值 ~4.8 GB，低于这个数必掉进换页区
SERVE_PORTS = {8080: "本地模型服务", 8081: "bge-m3 嵌入服务"}


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30).stdout
    except Exception as exc:                     # noqa: BLE001
        return "ERR %s: %s" % (type(exc).__name__, exc)


def ps(script):
    """跑一段 PowerShell，返回 stdout。

    ⚠ 不要用 wmic：Windows 11 24H2 已移除，返回空字符串会让"检测进程"静默失效
    —— 本工具第一版就是这么错过的（训练明明在跑，却报"无训练进程、可以开始"）。
    """
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        return r.stdout or ""
    except Exception as exc:                     # noqa: BLE001
        return "ERR %s: %s" % (type(exc).__name__, exc)


def cmdlines():
    """ProcessId → CommandLine（所有 python 进程）。"""
    out = ps("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
             'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }')
    m = {}
    for ln in out.splitlines():
        if "\t" in ln:
            pid, cmd = ln.split("\t", 1)
            m[pid.strip()] = re.sub(r"\s+", " ", cmd.strip())
    return m


def gpu_apps():
    """卡上真正吃显存的进程。

    ⚠ Windows/WDDM 下 `--query-compute-apps` 会把所有带图形上下文的进程都列出来
    （explorer / 输入法 / 覆盖层…），显存一栏是 `[N/A]`。只认能报出具体 MB 的，
    否则这份清单既没用又会淹没真正的占用者。
    """
    out = sh("nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv,noheader")
    apps = []
    for ln in (out or "").splitlines():
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and re.match(r"^\d+\s*MiB$", parts[1]):
            apps.append({"pid": parts[0], "used": parts[1], "name": parts[2] if len(parts) > 2 else ""})
    return apps


def gpu_status():
    out = sh("nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader,nounits")
    line = (out or "").strip().splitlines()
    if not line:
        return None
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 3:
        return None
    try:
        return {"name": parts[0], "total": int(parts[1]), "used": int(parts[2])}
    except ValueError:
        return None


def port_owner(port):
    out = sh('netstat -ano | findstr ":%d " | findstr LISTENING' % port)
    line = out.strip().splitlines()[0] if out.strip() else ""
    m = re.search(r"(\d+)\s*$", line)
    if not m:
        return None
    pid = m.group(1)
    cmd = cmdlines().get(pid, "")
    return {"pid": pid, "cmd": cmd[:160]}


def training_procs():
    """在训进程：命令行里带 train_qlora 的 python。"""
    return [{"pid": pid, "cmd": cmd} for pid, cmd in cmdlines().items() if "train_qlora" in cmd]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--for-train", action="store_true", help="训练前检查：8081 有人就阻塞")
    ap.add_argument("--for-serve", action="store_true", help="起模型前检查：有训练在跑就阻塞")
    a = ap.parse_args()

    blockers = []
    print("== GPU ==")
    g = gpu_status()
    if g:
        free = g["total"] - g["used"]
        print("  %s：已用 %d MB / 共 %d MB（空闲 %d MB）" % (g["name"], g["used"], g["total"], free))
        if a.for_train and free < GPU_MIN_FREE_MB:
            blockers.append("显存空闲 %d MB < %d MB（会掉进换页区）" % (free, GPU_MIN_FREE_MB))
    else:
        print("  nvidia-smi 不可用")

    print("== 端口 ==")
    for port, label in SERVE_PORTS.items():
        owner = port_owner(port)
        if owner:
            print("  %d %s ← PID %s" % (port, label, owner["pid"]))
            print("     %s" % owner["cmd"])
            if a.for_train and port == 8081:
                blockers.append("%d (%s) 还占着显存，先停掉再开训" % (port, label))
        else:
            print("  %d 空闲" % port)

    print("== 卡上的进程 ==")
    apps = gpu_apps()
    if apps:
        for p in apps:
            print("  PID %s 占 %s ← %s" % (p["pid"], p["used"], p["name"]))
    else:
        print("  无（显存占用都来自系统/桌面，nvidia-smi 不列非计算进程时属正常）")
        free_now = (g["total"] - g["used"]) if g else 0
        if a.for_serve and g and free_now < 600:
            blockers.append("显存只剩 %d MB，起服务会挤爆（有别的程序占着卡）" % free_now)

    print("== 训练进程 ==")
    procs = training_procs()
    if procs:
        for p in procs:
            print("  PID %s 在训：%s" % (p["pid"], p["cmd"][:140]))
        if a.for_serve:
            blockers.append("有 train_qlora 在跑，起服务会抢显存")
    else:
        print("  无")

    if blockers:
        print("\n阻塞项 %d 个：" % len(blockers))
        for b in blockers:
            print("  ✗", b)
        return 1
    print("\n可以开始。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
