# -*- coding: utf-8 -*-
r"""交付机自检：这套"本地 LLM"拷过来之后，到底能不能跑。

为什么要有这个脚本：
  交付机的内存/显存/盘符都与本机不同（用户口径：内存 6-14G、独显显存 2-6G、盘符未必是 D:），
  而"能不能跑"取决于**三件东西同时成立**：① 有可用的 llama-server 构建；② 该机档位对应的
  模型文件在位且**字节数正确**（下到一半的 GGUF 会加载失败或输出乱码，光看"文件存在"看不出来）；
  ③ 需要 RAG 时向量库在位。这三件东西人肉核对很容易漏，所以做成一条命令。

用法（在交付机上）：
    python verify_delivery.py            # 清单 + 档位判定（**只读，不启动任何服务**）
    python verify_delivery.py --live     # 再真起一次服务、发一句话、然后停掉（这才是真验收）
    python verify_delivery.py --report 自检报告.txt   # 顺手落一份报告（打包态无控制台时看它）
    #   打包态（exe 无控制台）：`AITerminal.exe --script <自检目录>\verify_delivery.py --report ...`

退出码：0 = 必查项全过（可能有可选性 WARN）；1 = 有必查项 FAIL。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import paths  # noqa: E402

PASS, WARN, FAIL = [], [], []

#: 模型清单：{文件名: (精确字节, 是否必需, 用途)}
#: 字节数都是**实测值**（不是估算）——用来抓"下到一半/被截断"的 GGUF，
#: 这类文件"存在但加载失败"，只看存在性会误判为就绪。
EXPECTED_MODELS = {
    "Qwen3-1.7B-Q8_0.gguf": (1834426016, True, "小显存档（2GB 左右显存 / 纯 CPU 兜底）"),
    "Qwen3-4B-Q4_K_M.gguf": (2497280256, True, "★ 交付主力档（3-4GB 显存）"),
    # 自训档：体积与官方 4B-Q4_K_M 完全相同（同架构同量化），故字节数一致；属可选增强，
    # 缺了只 WARN 不算交付失败（required=False）。导出方式见 export_gguf.py。
    "Qwen3-4B-AIT-Q4_K_M.gguf": (2497280256, False, "自训档（QLoRA 微调 4B · --tier ait）"),
    "Qwen3-8B-Q4_K_M.gguf": (5027783488, False, "顶配档（5GB+ 显存）"),
    "bge-m3-Q8_0.gguf": (634553760, True, "RAG 嵌入模型（1024 维）"),
    "Qwen3-Embedding-0.6B-Q8_0.gguf": (639150592, False, "嵌入备选（更小）"),
}


def check(name, ok, detail="", level="required"):
    bucket = PASS if ok else (WARN if level == "optional" else FAIL)
    bucket.append(name)
    tag = "PASS" if ok else ("WARN" if level == "optional" else "FAIL")
    print(f"  [{tag}] {name}{('  ' + detail) if detail else ''}")


def human(n):
    return f"{n/1024**3:.2f}GB" if n >= 1024 ** 3 else f"{n/1024**2:.0f}MB"


class _Tee:
    """把报告同时写给控制台和 `--report` 的文件。

    打包态的 exe 是 windowed（**无控制台**）：`自检.bat` 里看不到任何打印，
    所以脚本得自己落一份文件（落在交付机本地，打开就能看）。
    任一路写失败都不能影响自检本身 —— 这是诊断工具，不能因为"日志写不进去"而停下。
    """

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, text):
        for s in self._streams:
            try:
                s.write(text)
            except Exception:  # noqa: BLE001
                pass
        return len(text)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="额外真起一次服务并发一句话（会占显存/内存，首次加载几十秒）")
    ap.add_argument("--report", default="", metavar="文件",
                    help="把这份自检报告原文写到该文件（打包态无控制台时的唯一可见途径）")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):      # 控制台可能是 GBK，见 REFERENCE §2.30
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    report_fh = None
    if args.report:
        try:
            report_fh = open(args.report, "w", encoding="utf-8")
        except OSError as e:                     # 写不了就只在控制台打，别让自检失败
            print(f"  !! --report 文件打不开（{type(e).__name__}: {e}），只在控制台打印")
        else:
            sys.stdout = _Tee(sys.stdout, report_fh)

    print("=== 本地 LLM 交付自检 ===")
    print(f"  {paths.describe()}")
    print(f"  Python {sys.version.split()[0]}")

    print("\n--- ① llama-server 构建 ---")
    exe = paths.server_exe()
    check("存在可用的 llama-server.exe", bool(exe), exe or f"找过 {paths.BIN}\\llamacpp-*")
    have = [os.path.basename(os.path.dirname(p))
            for p in paths.SERVER_CANDIDATES
            if os.path.exists(p)]
    print(f"       已解出的后端：{have or '（无）'}")
    if exe and "vulkan" not in exe:
        # Vulkan 是首选：N 卡/A 卡/Intel 通吃。只有 CPU 版时纯 CPU 跑得动但慢。
        check("优先用 Vulkan 后端（A/N/Intel 通吃）", False,
              f"当前落在 {os.path.basename(os.path.dirname(exe))}；纯 CPU 也能用，只是慢",
              level="optional")

    print("\n--- ② 模型文件（精确字节比对）---")
    for name, (size, required, why) in EXPECTED_MODELS.items():
        path = os.path.join(paths.MODELS, name)
        if not os.path.exists(path):
            check(f"{name} 在位", False, f"缺文件（{why}）",
                  level="required" if required else "optional")
            continue
        got = os.path.getsize(path)
        if got == size:
            check(f"{name} 字节数正确", True, f"{human(got)} · {why}",
                  level="required" if required else "optional")
        elif got < size:
            check(f"{name} 字节数正确", False,
                  f"**截断**：{got} < 应有 {size}（差 {human(size-got)}）→ 重跑 download_models.py",
                  level="required" if required else "optional")
        else:
            check(f"{name} 字节数正确", False, f"比预期大：{got} > {size}（可能是别的版本）",
                  level="required" if required else "optional")

    print("\n--- ③ 本机档位判定 ---")
    probe = None
    try:
        probe, why = _probe()
        if probe is None:
            check("档位探测可运行", False, why)
    except Exception as e:  # noqa: BLE001
        check("档位探测可运行", False, f"{type(e).__name__}: {e}")

    if probe:
        check("档位探测可运行", True,
              f"tier={probe['tier']} model={probe['model']} device={probe['device']}")
        print(f"       内存 {probe['ram_total_gb']}GB（空闲 {probe['ram_avail_gb']}GB）"
              f" · 判定理由：{probe['reason']}")
        for dev in probe.get("devices") or []:
            print(f"       设备 {dev['id']}: {dev['name']} {dev['total_mb']}MiB"
                  f" (free {dev['free_mb']}){'  [核显·共享内存，不计入显存]' if dev['is_igpu'] else ''}")
        target = os.path.join(paths.MODELS, probe["model"])
        check(f"档位所需模型在位（{probe['model']}）", os.path.exists(target),
              target if os.path.exists(target) else f"缺 {probe['model']}")
        if probe["tier"] == "cpu":
            print("       注意：判定为纯 CPU 档 ⇒ 对话会很慢，但**本地检索照常可用**（设计如此）")

    print("\n--- ④ RAG 向量库（可选：纯对话不需要）---")
    idx = paths.INDEX
    chunks = [f for f in os.listdir(idx) if f.endswith("_chunks.jsonl")] if os.path.isdir(idx) else []
    if not chunks:
        check("存在切片文件", False, f"{idx} 下没有 *_chunks.jsonl（要么还没建索引，要么没随包）",
              level="optional")
    for f in sorted(chunks):
        vendor = f[:-len("_chunks.jsonl")]
        emb = os.path.join(idx, f"{vendor}_emb.f16")
        meta = os.path.join(idx, f"{vendor}_emb.meta.json")
        note = ""
        if os.path.exists(meta):
            try:
                m = json.load(open(meta, encoding="utf-8"))
                note = f"已嵌入 {m.get('done')}/{m.get('n')}（失败 {m.get('failed')}）"
            except Exception:  # noqa: BLE001
                note = "meta 读不出来"
        check(f"{vendor}: 向量库 + meta 在位", os.path.exists(emb) and os.path.exists(meta),
              human(os.path.getsize(emb)) if os.path.exists(emb) else "缺 " + os.path.basename(emb),
              level="optional")
        if note:
            print(f"       {vendor}: {note}")

    if args.live:
        print("\n--- ⑤ 真起一次服务（--live）---")
        run_server = os.path.join(HERE, "run_server.py")
        up = _start(run_server)
        check("服务起来了", up)
        if up:
            try:
                answer = _ask_once()
                check("发一句话能拿到回答", bool(answer), (answer or "")[:80])
            except Exception as e:  # noqa: BLE001
                check("发一句话能拿到回答", False, f"{type(e).__name__}: {e}")
        subprocess.run(paths.script_cmd(run_server, "--stop"), cwd=HERE,
                       capture_output=True, text=True, timeout=180)
        print("       已停掉服务")

    print(f"\n=== 汇总：必查 PASS {len(PASS)} / FAIL {len(FAIL)}；可选 WARN {len(WARN)} ===")
    for n in FAIL:
        print(f"   FAIL: {n}")
    if WARN:
        print(f"   可选未过（不影响纯对话）：{'、'.join(WARN)}")
    if not FAIL:
        print("   结论：这台机器可以跑本地模型。"
              + ("（档位为 CPU 档时较慢，检索功能不受影响）" if probe and probe["tier"] == "cpu" else ""))
    if report_fh is not None:                    # 报告落盘（--report）
        try:
            sys.stdout.flush()
            report_fh.close()
        except OSError:
            pass
    return 1 if FAIL else 0


def _probe():
    """跑一次档位探测。返回 `(结论 dict 或 None, 失败原因)`。

    结论**从 `--out` 文件读回**，不读 stdout：打包态 exe 是 windowed（无控制台），
    子进程 stdout 拿不到（见 core/app_paths.py 模块头）—— 读 stdout 的话，
    自检在交付机上的"档位探测"一项会必 FAIL，而机器其实完全够用。
    """
    fd, tmp = tempfile.mkstemp(prefix="ait_probe_", suffix=".json")
    os.close(fd)
    try:
        out = subprocess.run(
            paths.script_cmd(os.path.join(HERE, "probe_tier.py"), "--json", "--out", tmp),
            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        raw = ""
        try:
            with open(tmp, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            raw = out.stdout or ""          # 文件没写出来 → 退回 stdout（两条通道都试）
        if not raw.strip():
            return None, ((out.stderr or out.stdout or "").strip()[:120]
                          or f"探测无输出（rc={out.returncode}）")
        try:
            return json.loads(raw), ""
        except ValueError:
            return None, f"探测输出不是 JSON: {raw[:120]}"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _start(run_server):
    """起服务（不阻塞：run_server 自己会等健康检查，超时给它留够）。"""
    try:
        r = subprocess.run(paths.script_cmd(run_server), cwd=HERE, capture_output=True,
                           text=True, timeout=600, encoding="utf-8", errors="replace")
        print(f"       run_server rc={r.returncode}")
        for line in (r.stdout or "").splitlines()[-3:]:
            print(f"       {line}")
    except Exception as e:  # noqa: BLE001
        print(f"       启动异常 {type(e).__name__}: {e}")
        return False
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/health",
                                     headers={"Authorization": "Bearer local-llm"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return b"ok" in resp.read()
    except Exception:  # noqa: BLE001
        return False


def _ask_once():
    import urllib.request
    body = json.dumps({"model": "local-llm",
                       "messages": [{"role": "user", "content": "只回两个字：就绪"}],
                       "max_tokens": 16, "stream": False}).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions", data=body,
                                headers={"Content-Type": "application/json",
                                         "Authorization": "Bearer local-llm"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")


if __name__ == "__main__":
    sys.exit(main())
