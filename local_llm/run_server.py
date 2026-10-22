# -*- coding: utf-8 -*-
"""启动/停止本地 llama-server（NetOpsTerminal 的"本地大模型"后端）。

为什么要单独一个启动器而不是裸敲命令：
  · 档位要按机器自适应（交付机显存 2-6GB 不等）——复用 probe_tier.py 的判定
  · NetOpsTerminal 侧只需要一个 **OpenAI 兼容 base_url**，这里负责把它起好、等健康检查通过、
    并把可直接填进设置界面的参数打印出来（provider=openai_compat）
  · 交付机上必须能"起了就忘"（detached）+ 能干净停掉（pid 文件）

与 NetOpsTerminal 的对接点（实码）：
  core/llm.py:49-55   provider "openai_compat" 就是"自建/中转/本地"
  core/llm.py:75-89   chat_url() 会在 base_url 后面补 /v1/chat/completions
  core/llm.py:126-131 configured 要求 api_key 非空 ⇒ 这里用占位 key 并显式 --api-key

用法:
    python run_server.py                # 按自动探测档位启动
    python run_server.py --tier mid     # 强制档位：high/mid/low/cpu
    python run_server.py --ctx 4096
    python run_server.py --status       # 看一眼在不在
    python run_server.py --stop         # 停掉
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Windows 控制台默认 GBK，print 里一旦出现 GBK 没有的字符（实测 ⚠ U+26A0 就是）
# 会直接 UnicodeEncodeError **崩掉整个进程** —— `--stop` 曾这样崩在 taskkill 之前，
# 表现成"报错但没停服务"，比难看更糟。这里只兜编码、不改输出内容：
# 编不出的字符退化成 ?，绝不因为一行日志让功能失败。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:                 # 直接跑 / 被导入 两种方式都能 import 同目录模块
    sys.path.insert(0, HERE)
import paths  # noqa: E402  （路径的唯一来源：换盘或交付机只改 paths.py）

MODELS = paths.MODELS
BIN = paths.BIN
PIDFILE = os.path.join(HERE, "_llama_server.pid")
LOG = os.path.join(HERE, "_llama_server.log")
API_KEY = "local-llm"
#: 模型别名。不带 --alias 时 /v1/models 会把**模型文件全路径**当 id 返回，
#: 填进设置界面又长又随档位变（mid 和 low 是两个不同的路径）。
#: 固定别名后，NetOpsTerminal 里不管是 4B 还是 1.7B，模型名永远是 local-llm。
MODEL_ALIAS = "local-llm"

sys.path.insert(0, HERE)
import probe_tier  # noqa: E402  （同目录，探测档位与设备）


def find_server_exe():
    exe = probe_tier.find_server()
    if not exe:
        raise SystemExit(f"!! 找不到 llama-server.exe（找过 {BIN}\\llamacpp-*）")
    return exe


def resolve_model(tier: str):
    """档位 -> 模型文件绝对路径（不存在就明确报缺哪个文件，不猜）。"""
    if tier == "cpu":
        tier = "low"
    for _need, name, model, _w, _note in probe_tier.TIERS:
        if name == tier:
            path = os.path.join(MODELS, model)
            if not os.path.exists(path):
                have = [f for f in os.listdir(MODELS)] if os.path.isdir(MODELS) else []
                # `ait` 是**自训档**，任何镜像都下不到 —— 提示必须和别的档不一样，
                # 否则用户会照着提示去 download_models.py 白找一轮。
                if tier == "ait":
                    how = ("补下: 自训档不在下载列表里，先导出 →\n"
                           "        python local_llm/export_gguf.py "
                           "--adapter D:\\ait_llm\\train\\lora\\sft\\final")
                else:
                    how = (f"补下: python download_models.py "
                           f"{'core' if tier in ('mid',) else 'all'}")
                raise SystemExit(f"!! 缺模型 {model}（档位 {tier}）\n   已有: {have}\n   {how}")
            return path, model
    raise SystemExit(f"!! 未知档位 {tier!r}")


def http_get(url, timeout=5):
    """GET。⚠ 必须带 Authorization：启动时传了 --api-key，/v1/* 会校验；
    不带就 401（实测表现为 HTTPError），而 /health 不校验所以会"健康但查不到模型"。"""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def wait_health(port, timeout=180):
    """等 /health 变 ok。llama-server 加载大模型可能要几十秒。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            _s, body = http_get(f"http://127.0.0.1:{port}/health", timeout=3)
            if '"ok"' in body:
                return True
            last = body[:120]
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}"
        time.sleep(1.5)
    print(f"   ! 健康检查超时，最后状态: {last}", flush=True)
    return False


def _alive(pid):
    r"""Windows 上安全的"进程还活着吗"判断。

    ⚠ 绝对不要用 os.kill(pid, 0) 探活：在 Windows 上它不是探活，而是
    TerminateProcess —— 会把目标进程直接杀掉（本机踩到过；项目自己的坑清单
    §6-2 也记着这条，原话是"os.kill(pid,0) 在 Windows 不可信"）。
    正确做法来自该项目既有实践：ctypes OpenProcess(SYNCHRONIZE) + WaitForSingleObject。
    """
    import ctypes
    if not sys.platform.startswith("win"):
        # Linux/macOS：os.kill(pid, 0) 是真探活，不会杀进程
        try:
            os.kill(int(pid), 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(handle)


def read_pid():
    try:
        with open(PIDFILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        pid = int(data.get("pid") or 0)
        return data if (pid and _alive(pid)) else None
    except Exception:  # noqa: BLE001
        return None


def do_status(port):
    info = read_pid()
    if not info:
        print("llama-server: 未运行（无有效 pid 或进程已退出）")
        return 1
    try:
        _s, body = http_get(f"http://127.0.0.1:{port}/v1/models", timeout=4)
        print("llama-server: 运行中")
        print(f"  pid={info.get('pid')} port={info.get('port')} model={info.get('model')}")
        print(f"  /v1/models -> {body[:300]}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"llama-server: pid {info.get('pid')} 在，但 HTTP 不通（{type(e).__name__}）")
        return 2


def other_servers():
    """列出系统里所有 llama-server.exe 的 PID（不只 pidfile 里那个）。

    为什么必须有这个：实测 Windows **允许两个进程绑同一个端口**（SO_REUSEADDR 语义），
    于是"以为停掉了、其实还在"的旧实例会和新实例**同时抢同一块显存**——
    生成速度从 ~60 tok/s 掉到 ~4 tok/s，且请求随机落到其中一个（现象极具误导性）。
    """
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq llama-server.exe",
                              "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=30).stdout or ""
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if not parts or "llama-server" not in parts[0].lower():
            continue
        try:
            pids.append(int(parts[1]))
        except (IndexError, ValueError):
            continue
    return pids


def port_pids(port):
    """列出**正在监听指定端口**的 PID（能找到 pidfile 丢失的孤儿实例）。"""
    pids = set()
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=30).stdout or ""
    except Exception:  # noqa: BLE001
        return pids
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING" or not parts[1].endswith(":" + str(port)):
            continue
        try:
            pids.add(int(parts[4]))
        except (IndexError, ValueError):
            continue
    return pids


def do_stop(port, all_servers=False):
    """停服务。

    ⚠ 默认**只停本端口上的 llama-server**。同一台机器上可能还跑着别的 llama-server：
    嵌入服务（bge-m3, :8081）就是**同名进程**、由 embed_chunks.py 拉起，与本启动器无关。
    早先这里是无差别 taskkill 所有 llama-server.exe，实测后果：验证本地模型时顺手
    `--stop`，把正在跑的全量嵌入任务当场打断（第 6465 批 ConnectionResetError、
    转入逐条兜底，服务端却已不在）——而嵌入要跑几小时、被打断就得重来。要全停请显式 --stop-all。
    """
    all_pids = set(other_servers())
    if all_servers:
        pids = all_pids
    else:
        pids = port_pids(port) & all_pids
        info = read_pid()
        if info and int(info["pid"]) in all_pids:
            pids.add(int(info["pid"]))
        rest = sorted(all_pids - pids)
        if rest:
            print(f"  [!] 另有 {len(rest)} 个 llama-server 在跑（pid {rest}），不在 {port} 端口上，"
                  f"**未动**（可能是 :8081 的嵌入服务）；确要全停用 --stop-all")
    if not pids:
        print(f"端口 {port} 上没有 llama-server 在跑，无需停止")
        try:
            os.remove(PIDFILE)
        except OSError:
            pass
        return 0
    for pid in sorted(pids):
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=30)
        print(f"  停止 pid={pid}  rc={r.returncode}")
    try:
        os.remove(PIDFILE)
    except OSError:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="", help="high/mid/low/cpu，留空=自动探测")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ctx", type=int, default=4096)
    ap.add_argument("--threads", type=int, default=0, help="0=让 llama.cpp 自决")
    ap.add_argument("--ngl", type=int, default=None, help="覆盖 GPU 层数，默认全上(-1)")
    ap.add_argument("--think", action="store_true",
                    help="保留 Qwen3 思考过程（默认关闭：实测思考会吃光 max_tokens、正文为空）")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--stop-all", action="store_true",
                    help="停掉机器上**所有** llama-server（含 :8081 嵌入服务，慎用）")
    ap.add_argument("--force", action="store_true",
                    help="同端口已有实例 / 判定内存不足时仍强行启动（默认是保护性拒绝）")
    args = ap.parse_args()

    if args.status:
        return do_status(args.port)
    if args.stop or args.stop_all:
        return do_stop(args.port, all_servers=args.stop_all)

    # 启动前守卫①：同端口已有实例 —— 实测 Windows **允许两个进程绑同一端口**
    # （SO_REUSEADDR 语义），于是"以为停掉了、其实还在"的旧实例会和新实例同时抢
    # 同一块显存：生成速度 59.75 → 4.03 tok/s，且请求随机落到其中一个（极具误导性）。
    busy = port_pids(args.port)
    if busy:
        if not args.force:
            print(f"!! 端口 {args.port} 上已有 llama-server（pid {sorted(busy)}）—— 拒绝再起一个。\n"
                  f"   重启请先 `python run_server.py --stop`；确要并行实例再加 --force。", flush=True)
            return 5
        print(f"  [!] 端口 {args.port} 上已有 llama-server（pid {sorted(busy)}），"
              f"--force 仍继续：两个实例会争抢显存。", flush=True)
    others = sorted(set(other_servers()) - set(busy))
    if others:
        print(f"  [!] 另有 {len(others)} 个 llama-server 在跑（pid {others}，可能是 :8081 嵌入服务）："
              f"它们同样占显存，显存紧张时会拖慢生成。", flush=True)

    exe = find_server_exe()
    print(paths.describe(), flush=True)   # 用了兜底目录也要让人看得见，别静默搬家

    if args.tier:
        tier = args.tier
        model_path, model_name = resolve_model(tier)
        device = "(强制指定)"
        ngl = -1 if tier != "cpu" else 0
        print(f"  （显式指定 --tier {tier}：跳过内存闸门，仅作提示）", flush=True)
    else:
        devs = probe_tier.list_devices(exe)
        total_ram, avail_ram = probe_tier.ram_gb()
        tier, model_name, weight_mb, note, device = probe_tier.pick_tier(devs, avail_ram)
        ram_ok, _need, ram_note = probe_tier.ram_check(tier, weight_mb, avail_ram)
        print(f"自动档位: {tier}（{note}） 设备={device} "
              f"内存 {total_ram:.1f}/{avail_ram:.1f}GB", flush=True)
        print(f"  {ram_note}", flush=True)
        # 启动前守卫②：内存闸门。硬起会把整机拖进换页，而本地检索本来就能用
        # （项目原则：能不用 AI 就不用 / 未处理 > 处理错）。
        if not ram_ok and not args.force:
            print("  !! 内存不足，拒绝启动。建议：① 只用本地检索（推荐）"
                  "② --tier low 降档 ③ --force 强行试。", flush=True)
            return 4
        model_path, model_name = resolve_model(tier)
        ngl = -1 if tier != "cpu" else 0

    if args.ngl is not None:
        ngl = args.ngl

    cmd = [exe, "-m", model_path, "--host", "127.0.0.1", "--port", str(args.port),
           "-ngl", str(ngl), "-c", str(args.ctx), "--api-key", API_KEY,
           "-a", MODEL_ALIAS]
    if args.threads:
        cmd += ["-t", str(args.threads)]
    # ⚠ 思考模式必须关：实测（b11696 + Qwen3-4B-Q4_K_M）max_tokens=192 全部 192 个 token
    # 都进了 reasoning_content，`content` 为空 ⇒ 终端助手完全不可用。
    # 又及：`--chat-template-kwargs {"enable_thinking":false}` **实测无效**（思考照旧），
    # 本版 llama.cpp 的正确入口是 `--reasoning off`（备用 --reasoning-budget 0）。
    # 这与 core/llm.py:246-258 为 LongCat 处理的是同一个坑：那边只能报错，这边能根治。
    cmd += ["--reasoning", "on" if args.think else "off"]

    print("启动: " + " ".join(cmd), flush=True)
    logf = open(LOG, "ab", buffering=0)
    logf.write(("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
    creation = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(cmd, cwd=os.path.dirname(exe), stdout=logf, stderr=logf,
                            creationflags=creation)
    with open(PIDFILE, "w", encoding="utf-8") as fh:
        json.dump({"pid": proc.pid, "port": args.port, "model": model_name,
                   "tier": tier, "device": device, "started": time.strftime("%Y-%m-%d %H:%M:%S")},
                  fh, ensure_ascii=False, indent=2)

    print(f"   pid={proc.pid}  日志 {LOG}", flush=True)
    if not wait_health(args.port):
        print("   ! 启动失败，请查日志尾部", flush=True)
        return 3

    print("\n=== 健康检查通过 ===", flush=True)
    served = ""
    try:
        _s, body = http_get(f"http://127.0.0.1:{args.port}/v1/models", timeout=5)
        served = ""
        try:
            served = (json.loads(body).get("data") or [{}])[0].get("id", "")
        except Exception:  # noqa: BLE001
            pass
        print(f"服务端自报模型 id: {served}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"/v1/models 查询失败: {type(e).__name__}", flush=True)

    print("\n--- 填进 NetOpsTerminal（设置→AI）---")
    print(f"  提供方(provider) : local   ← 已内置预设，base_url/模型/占位密钥都已填好，"
          f"直接选它再点[测试]即可")
    print(f"  接口地址(base_url): http://127.0.0.1:{args.port}/v1")
    print(f"  模型(model)      : {MODEL_ALIAS}  （服务端自报 id: {served or '未取到'}）")
    print(f"  API Key          : {API_KEY}   （占位即可：core/llm.py 只要求非空）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
