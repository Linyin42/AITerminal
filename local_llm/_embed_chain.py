# -*- coding: utf-8 -*-
r"""顺序嵌入多个厂商（RAG 第 2 步的批处理外壳）。

为什么单独写：2026-10-16 夜那次 h3c→ruijie→tplink 的链在 ruijie 38401/94802 处死掉，
没有任何日志留下（输出走的是会话后台作业的缓冲，作业一消失就没了）。本外壳：
  · 每个厂商一个**独立子进程**（`embed_chunks.py --vendor X`），一个失败不影响下一个；
  · 输出全部走继承的 stdout（由启动方重定向到日志文件）——不建管道；
  · 断点由 embed_chunks.py 自己管理（每 20 批落 meta.json），本外壳不碰。

用法：
    python _embed_chain.py                 # 默认 ruijie, tplink
    python _embed_chain.py h3c ruijie      # 指定厂商
    python _embed_chain.py --after-pid=1234 huawei   # 等 PID 1234 退出再开始（串行接力用）

⚠ 探活**不要**用 `os.kill(pid, 0)`：在 Windows 上它等于 TerminateProcess（会真杀进程，
   本项目已踩过）。这里用 psutil.pid_exists。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_argv = sys.argv[1:]
_after = [a.split("=", 1)[1] for a in _argv if a.startswith("--after-pid=")]
AFTER_PID = int(_after[0]) if _after and _after[0].isdigit() else None
VENDORS = [a for a in _argv if not a.startswith("--")] or ["ruijie", "tplink"]


def _wait_for(after_pid):
    """等前序进程退出（含"它已死"的情况，直接放行）。"""
    if after_pid is None:
        return
    try:
        import psutil
    except ImportError:
        print(f"[chain] !! 无 psutil，跳过等待 PID {after_pid}", flush=True)
        return
    print(f"[chain] 等待 PID {after_pid} 退出…", flush=True)
    while psutil.pid_exists(after_pid):
        time.sleep(10)
    print(f"[chain] PID {after_pid} 已退出，开始", flush=True)


def main():
    tag = f"（等 PID {AFTER_PID} 退出）" if AFTER_PID else ""
    print(f"[chain] 开始 {time.strftime('%Y-%m-%d %H:%M:%S')} 厂商 = {VENDORS}{tag}", flush=True)
    _wait_for(AFTER_PID)
    rcs = {}
    for v in VENDORS:
        print(f"[chain] ▶ {v}  {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        t0 = time.time()
        rc = subprocess.call([sys.executable, os.path.join(HERE, "embed_chunks.py"),
                              "--vendor", v], cwd=HERE)
        rcs[v] = rc
        print(f"[chain] ■ {v} rc={rc}  用时 {(time.time() - t0) / 60:.1f} 分钟", flush=True)
        if rc != 0:
            print("[chain] 非零退出 —— 继续下一个（断点在盘上，可事后单独重跑）", flush=True)
    print(f"[chain] 全部结束 {time.strftime('%Y-%m-%d %H:%M:%S')} 结果 = {rcs}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
