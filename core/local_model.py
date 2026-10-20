# -*- coding: utf-8 -*-
"""本地模型（llama.cpp）的**状态读取**与**降级决策**（P2 接线层）。

职责边界（重要，别搞混）：
  · 档位怎么判、判成哪个模型 —— **只有一份实现**：`local_llm/probe_tier.py`。
    本模块**不复制档位表**（阈值 5392/2986/2351 只在那一个文件里），只负责
    "读它的结论"（`probe_tier.py --json`）并翻译成界面能直接用的模式与文案。
    把阈值表抄一份到这里，两边迟早不一致 —— 项目里已经因为"同一个事实两处写"
    返工过（见 REFERENCE §2.15）。
  · **降级不等于降质**：拿不到本地 LLM 时**只关 LLM、不关检索** ——
    `core/vendor_knowledge.py`（SQLite 命令层）+ `core/tech_knowledge.py`（关键词检索）
    零额外内存就能给出"有出处"的答案，符合项目原则"能不用 AI 就不用 / 未处理 > 处理错"。
  · **永不抛异常**：探测失败一律按"不可用"处理并带上原因。界面不能因为探测失败而白屏。

三种模式（`decide()` 的返回值 `mode`）：
  MODE_LOCAL      本地模型服务已在跑，直接用（base_url 指向本机 :8080/v1）
  MODE_STARTABLE  机器档位够用、但服务没起 —— 可以一键 `run_server.py` 拉起来
  MODE_RETRIEVAL  机器不够档（或没有独显）—— **只做本地检索**，界面如实标注

用法：
    python -m core.local_model        # 打印当前状态（人读）
    from core.local_model import decide, describe
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile

from core import app_paths

MODE_LOCAL = "local"
MODE_STARTABLE = "startable"
MODE_RETRIEVAL = "retrieval"

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_LLM_DIR = os.path.join(os.path.dirname(HERE), "local_llm")
PIDFILE = os.path.join(LOCAL_LLM_DIR, "_llama_server.pid")
PROBE = os.path.join(LOCAL_LLM_DIR, "probe_tier.py")
#: 兜底用：模型目录以探测结果带出的 models_dir 为准，这里只在探测没带出来时才用
MODELS_DIR = r"D:\ait_llm\models"
DEFAULT_PORT = 8080
PROBE_TIMEOUT = 30

#: 探测结果的进程内缓存（档位不常变；--list-devices 要起子进程，不进 UI 线程反复跑）
_PROBE = {"data": None}


def _alive(pid):
    """进程还活着吗。

    ⚠ **不要**用 `os.kill(pid, 0)` 探活：Windows 上它不是探活而是 TerminateProcess，
    会把目标进程直接杀掉 —— 本地 LLM 侧就是这么误杀过 llama-server 的（REFERENCE §E）。
    正确做法是 OpenProcess(SYNCHRONIZE) + WaitForSingleObject。
    """
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    import sys as _sys
    if not _sys.platform.startswith("win"):
        # Linux / macOS：这里 os.kill(pid, 0) 才是纯探活，不会发信号杀掉进程
        # （上面那句警告只针对 Windows）。
        try:
            import os as _os
            _os.kill(int(pid), 0)
            return True
        except PermissionError:
            return True              # 进程在，只是当前用户没权限给它发信号
        except OSError:
            return False
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def server_status():
    """本地模型服务现在在跑吗？在跑就把它自报的档位/设备/端口带回来。

    数据源是 `run_server.py` 写的 pid 文件（含 tier/device/model）——比重新探测便宜得多。
    """
    status = {"running": False, "port": DEFAULT_PORT, "tier": "", "model": "",
              "device": "", "started": "", "reason": ""}
    try:
        with open(PIDFILE, "r", encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        status["reason"] = "没有启动记录（服务没起过，或记录已被清掉）"
        return status
    pid = int(info.get("pid") or 0)
    if not pid or not _alive(pid):
        status["reason"] = f"启动记录里的 pid {pid} 已不在（上次退出没清干净）"
        return status
    status.update({
        "running": True,
        "port": int(info.get("port") or DEFAULT_PORT),
        "tier": info.get("tier") or "",
        "model": info.get("model") or "",
        "device": info.get("device") or "",
        "started": info.get("started") or "",
        "reason": f"pid {pid} 在跑",
    })
    return status


def base_url(port=DEFAULT_PORT):
    """本地服务的 OpenAI 兼容地址（正是 `core/llm.py` local 提供方预设的那个）。"""
    return f"http://127.0.0.1:{int(port)}/v1"


def _probe_inprocess():
    """进程内取档位结论（**首选**）：`import probe_tier` 后直接调它的 `collect()`。

    为什么不能只靠"起子进程读它的 stdout"：打包（PyInstaller onefile + windowed）后
    没有控制台，**子进程的 stdout 拿不到** —— `probe_tier.py --json` 打印的那份在交付机上
    永远是空串，表现为"档位探测失败"，而其实机器完全够用（见 core/app_paths.py 模块头）。

    判定逻辑仍然**只有一份**（`probe_tier.collect()`），这里只做导入 + 打 ok 标记。
    """
    if LOCAL_LLM_DIR not in sys.path:
        sys.path.insert(0, LOCAL_LLM_DIR)
    import probe_tier                                   # noqa: WPS433
    data = probe_tier.collect()
    data["ok"] = True
    return data


def _probe_subprocess():
    """兜底：起子进程探测，结果从 `--out` **文件**读回（同样因为 stdout 可能拿不到）。

    返回 `{"ok": True, ...}` 或 `{"ok": False, "error": ...}`；**不抛异常**。
    """
    try:
        fd, tmp = tempfile.mkstemp(prefix="ait_probe_", suffix=".json")
        os.close(fd)
    except OSError as e:
        return {"ok": False, "error": f"建临时文件失败 {type(e).__name__}: {e}"}

    result = {"ok": False, "error": ""}
    try:
        out = subprocess.run(app_paths.script_cmd(PROBE, "--json", "--out", tmp),
                             capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                             encoding="utf-8", errors="replace")
        try:
            with open(tmp, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            raw = out.stdout or ""      # 文件没写出来（旧脚本）→ 退回 stdout，两条通道都试
        if not raw.strip() and out.returncode != 0:
            result["error"] = ((out.stderr or out.stdout or "").strip()[:200]
                               or f"rc={out.returncode}")
        else:
            try:
                data = json.loads(raw)
            except ValueError:
                result["error"] = f"探测输出不是 JSON: {(raw or '')[:120]}"
            else:
                data["ok"] = True
                result = data
    except Exception as e:  # noqa: BLE001
        result["error"] = f"探测子进程失败 {type(e).__name__}: {e}"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return result


def probe(force=False):
    """跑一次档位探测 → `probe_tier` 的结论 dict（带 ok/error 包装）。

    ⚠ 内部会起一个 `llama-server --list-devices` 子进程（几百毫秒~几秒），
    **不要放在 UI 线程里同步调**。结果缓存在模块级，`force=True` 才重跑。
    """
    if not force and _PROBE["data"] is not None:
        return _PROBE["data"]

    result = {"ok": False, "error": ""}
    if not os.path.exists(PROBE):
        result["error"] = f"找不到档位探测脚本 {PROBE}"
    else:
        try:
            result = _probe_inprocess()
        except Exception as e:  # noqa: BLE001
            # 进程内不可用（打包没收进 probe_tier / 依赖缺失）⇒ 子进程兜底。
            # 两条路都失败时把**两个原因都带上**，否则交付机上看不出该修哪边。
            result = _probe_subprocess()
            if not result.get("ok"):
                result["error"] = (f"{result.get('error') or '子进程探测失败'}"
                                   f"（进程内也不可用：{type(e).__name__}: {e}）")
    _PROBE["data"] = result
    return result


def decide(p_info=None, s_info=None):
    """这台机器现在该怎么用？→ dict(mode / label / detail / base_url / tier / …)

    参数 `p_info`（探测结果）与 `s_info`（服务状态）留空时各自实时获取；
    显式传入是为了**测试可注入**（见文件末尾的自检：模拟小显存机、模拟纯 CPU 档）。
    """
    s_info = server_status() if s_info is None else s_info

    if s_info.get("running"):
        model = s_info.get("model") or ""
        return {
            "mode": MODE_LOCAL,
            "label": f"本地模型 · 已就绪（{model or '未知模型'}）",
            "detail": f"{s_info.get('device') or 'CPU'} · 端口 {s_info.get('port')}"
                      f" · 起于 {s_info.get('started') or '?'}",
            "base_url": base_url(s_info.get("port") or DEFAULT_PORT),
            "tier": s_info.get("tier") or "", "model": model,
            "device": s_info.get("device") or "",
            "reason": s_info.get("reason") or "",
        }

    p_info = probe() if p_info is None else p_info

    if not p_info.get("ok"):
        return {
            "mode": MODE_RETRIEVAL,
            "label": "仅本地检索（档位探测失败）",
            "detail": "本地资料库检索照常可用；本次不使用 LLM。",
            "base_url": "", "tier": "", "model": "", "device": "",
            "reason": p_info.get("error") or "探测无结果",
        }

    tier = p_info.get("tier") or "cpu"
    model = p_info.get("model") or ""
    # 模型目录以**探测结果带出的**为准（probe_tier.py --json 的 models_dir）：
    # 应用侧不再自己硬编码 `D:\ait_llm\models`，否则换盘/换交付机时这里会先报
    # "找不到模型文件"，而其实只是路径抄了另一份（同一事实两处写的典型后果）。
    models_dir = p_info.get("models_dir") or MODELS_DIR
    model_path = os.path.join(models_dir, model) if model else ""

    if tier == "cpu":
        reason = p_info.get("reason") or "判定为纯 CPU 档"
        detail = f"{reason}；本地检索不受影响。"
    elif not model or not os.path.exists(model_path):
        reason = f"档位 {tier} 需要模型文件 {model}，但 {models_dir} 里没有"
        detail = f"{reason}；先跑 download_models.py，或直接只用本地检索。"
    elif p_info.get("ram_ok") is False:
        # 显存够、**内存**不够时同样要降级：交付机内存 6-14GB，而纯 CPU 档的权重
        # 全在内存里，硬起会把整机拖进换页。ram_ok 由实测系数算出（probe_tier.ram_check）。
        reason = p_info.get("ram_note") or "可用内存不足"
        detail = f"{reason}；本地检索照常可用（这是设计降级，不是故障）。"
    else:
        return {
            "mode": MODE_STARTABLE,
            "label": f"本地模型 · 可启动（{model}）",
            "detail": f"将加载到 {p_info.get('device') or 'CPU'}"
                      f"（-ngl {p_info.get('n_gpu_layers')}）；启动命令 local_llm\\run_server.py",
            "base_url": base_url(DEFAULT_PORT),
            "tier": tier, "model": model, "device": p_info.get("device") or "",
            "reason": p_info.get("reason") or "",
        }

    return {
        "mode": MODE_RETRIEVAL,
        "label": "仅本地检索（不满足本地 LLM 档位）",
        "detail": detail,
        "base_url": "", "tier": tier, "model": model, "device": "",
        "reason": reason,
    }


def start_server(port=DEFAULT_PORT, timeout=300):
    """拉起本地 llama-server。**唯一实现**：AI 设置对话框的按钮与「开机自启」都走这里。

    打包态经 `app_paths.script_cmd` 变成 `AITerminal.exe --script run_server.py`，
    源码态直接 python 跑脚本 —— 两种情况交付机都不需要装 Python。
    返回 `(ok, 说明文本)`；**不抛异常**。
    """
    script = os.path.join(LOCAL_LLM_DIR, "run_server.py")
    if not os.path.exists(script):
        return False, f"找不到启动脚本 {script}"
    cmd = app_paths.script_cmd(script, "--port", str(int(port)))
    try:
        r = subprocess.run(cmd, cwd=LOCAL_LLM_DIR, timeout=timeout,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except Exception as e:                          # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    lines = (r.stdout or r.stderr or "").strip().splitlines()
    tail = lines[-1].strip() if lines else ""
    if server_status().get("running"):
        return True, tail or "已启动"
    return False, tail or f"启动失败（rc={r.returncode}）"


def stop_server(port=DEFAULT_PORT, timeout=120):
    """停掉本端口的本地 llama-server（**不动** :8081 的嵌入服务）。返回 `(ok, 说明文本)`。"""
    script = os.path.join(LOCAL_LLM_DIR, "run_server.py")
    if not os.path.exists(script):
        return False, f"找不到启动脚本 {script}"
    cmd = app_paths.script_cmd(script, "--stop", "--port", str(int(port)))
    try:
        r = subprocess.run(cmd, cwd=LOCAL_LLM_DIR, timeout=timeout,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except Exception as e:                          # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    lines = (r.stdout or r.stderr or "").strip().splitlines()
    tail = lines[-1].strip() if lines else ""
    return (not server_status().get("running")), (tail or "已停止")


def describe(decision=None):
    """一行界面标注文案：`本地模型 · 已就绪（X）｜…`"""
    d = decide() if decision is None else decision
    return f"{d.get('label', '')}｜{d.get('detail', '')}"


def _main():
    # Windows 控制台可能是 GBK，输出里带 GBK 之外的字符会 UnicodeEncodeError 崩掉
    # 整个命令 —— 只兜编码不改内容（REFERENCE §E 2.30）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    s_info = server_status()
    print("=== 本地模型服务 ===")
    print(f"  运行中 : {s_info['running']}  ({s_info['reason']})")
    if s_info["running"]:
        print(f"  档位   : {s_info['tier']}  模型 {s_info['model']}  设备 {s_info['device']}")
        print(f"  地址   : {base_url(s_info['port'])}")

    p_info = probe()
    print("=== 档位探测（probe_tier.py --json）===")
    if p_info.get("ok"):
        print(f"  内存   : 总 {p_info.get('ram_total_gb')}GB / 空闲 {p_info.get('ram_avail_gb')}GB")
        print(f"  档位   : {p_info.get('tier')} -> {p_info.get('model')}"
              f"（权重 {p_info.get('model_weight_mb')}MB，-ngl {p_info.get('n_gpu_layers')}）")
        print(f"  设备   : {p_info.get('device')}")
        print(f"  理由   : {p_info.get('reason')}")
    else:
        print(f"  失败   : {p_info.get('error')}")

    d = decide(p_info=p_info, s_info=s_info)
    print("=== 决策 ===")
    print(f"  模式   : {d['mode']}")
    print(f"  标注   : {describe(d)}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
