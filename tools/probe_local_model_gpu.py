# -*- coding: utf-8 -*-
"""本地模型「起得来 / 真上 GPU / 停得掉」探针。

对应用户反馈："软件起来之后没有使用GPU这个问题需要解决"。
先量事实再改代码：2026-10-17 实测本机（RTX 3060 Laptop 6GB）用 `run_server.py`
拉起时 **VRAM 1051 → 4094 MiB（+3.0GB）**、启动命令里 `-ngl -1` 且后端是
`llamacpp-vulkan` —— 也就是说**模型本身用 GPU 没问题**，问题是应用从不自动启动它
（`ui/ai_panel.py` 只标注档位），而 AI 提供方默认还是云端 deepseek。

本探针验证应用侧新增的起停通道（`core/local_model.start_server/stop_server`，
也是 AI 设置对话框与「启动时自动起」共用的唯一实现）：
  ① 起：`start_server()` 返回 ok、pid 文件写出、`server_status().running` 为真
  ② GPU：pid 文件里的 device 是 Vulkan*，且进程起来后显存实增（有 nvidia-smi 才量）
  ③ 服务真能用：`/v1/models` 返回服务端自报的模型 id
  ④ 停：`stop_server()` 之后进程真的没了、pid 文件被清掉

跑法：python -u tools\\probe_local_model_gpu.py   （约 1~2 分钟，会真的起/停模型）
"""
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import local_model                                        # noqa: E402

FAIL, TOTAL = [], [0]


def check(name, cond, detail=""):
    TOTAL[0] += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAIL.append(name)


def vram_used_mb():
    """当前整机显存占用（MB）；没有 nvidia-smi 就返回 None"""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20)
        return float((out.stdout or "").strip().splitlines()[0])
    except Exception:                                               # noqa: BLE001
        return None


print("=== 0. 清场：先确保 :8080 上没有残留 ===")
local_model.stop_server()
check("起跑线上没有本地模型在跑", not local_model.server_status().get("running"))

print("\n=== 1. 档位判定：这台机器够不够跑 ===")
info = local_model.probe()
check("档位探测成功", info.get("ok") is True, str(info.get("error") or "")[:80])
check("判定为可用档位（非纯 CPU）", info.get("tier") not in ("", "cpu"),
      f"tier={info.get('tier')} model={info.get('model')}")
check("要用的 GPU 层数是全上（-ngl -1）", info.get("n_gpu_layers") == -1,
      f"n_gpu_layers={info.get('n_gpu_layers')}")

print("\n=== 2. 起：应用侧通道能拉起本地模型 ===")
before = vram_used_mb()
ok, detail = local_model.start_server()
st = local_model.server_status()
check("start_server() 报成功", ok is True, detail[:100])
check("服务状态显示运行中", st.get("running") is True, st.get("reason", ""))
check("pid 文件写出了设备信息", bool(st.get("device")), f"device={st.get('device')} tier={st.get('tier')}")
check("设备是 Vulkan（= 走 GPU，不是 CPU）", "vulkan" in (st.get("device") or "").lower(),
      st.get("device") or "(空)")

after = vram_used_mb()
if before is None or after is None:
    print("  [skip] 没有 nvidia-smi，跳过显存实增量测")
else:
    check("显存实增（模型确实在 GPU 上）", (after - before) > 500,
          f"VRAM {before:.0f} → {after:.0f} MiB（+{after - before:.0f}）")

print("\n=== 3. 服务真能用（/v1/models） ===")
try:
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{st.get('port') or 8080}/v1/models",
                                 headers={"Authorization": "Bearer local-llm"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    served = (body.get("data") or [{}])[0].get("id", "")
    check("接口返回服务端自报模型 id", served == "local-llm", f"id={served}")
except Exception as e:                                              # noqa: BLE001
    check("接口可访问", False, f"{type(e).__name__}: {e}")

print("\n=== 4. 停：停得掉、清得干净 ===")
ok_stop, detail_stop = local_model.stop_server()
st2 = local_model.server_status()
check("stop_server() 报成功", ok_stop is True, detail_stop[:100])
check("服务状态显示未运行", st2.get("running") is False, st2.get("reason", ""))
check("pid 文件已清掉", not os.path.exists(local_model.PIDFILE), local_model.PIDFILE)

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
