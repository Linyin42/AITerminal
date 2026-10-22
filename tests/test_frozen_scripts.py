# -*- coding: utf-8 -*-
"""验证 P5-2「打包（PyInstaller onefile）态也能跑随包脚本」的改造。

为什么必须有这个用例 —— 三条都是真实硬伤，且都只在**交付机**上暴露：
  · 打包后 `sys.executable` 是 **NetOpsTerminal.exe 本身**：`[sys.executable, "probe_tier.py"]`
    会被当成"再开一个应用"，脚本根本不跑（改造前 5 处都是这个写法）。
    ⇒ 断言 `script_cmd()` 两态产出不同命令行，且 `main.py --script` 真能跑起来、退出码透传。
  · 打包态 exe 是 windowed（**无控制台**）⇒ 子进程 stdout 拿不到，"打印结果让父进程读"
    这条路是死的。⇒ 断言 `probe_tier.py --out <文件>` 与 stdout 两份 JSON 同源，
    且 `probe()` 能真的拿到结果。
  · `datas` 漏掉某个 .py ⇒ 打包态 `import retrieve` / `import probe_tier` 直接失败，
    RAG 与档位判定会**静默**降级（最坏的一种失败：不报错、只是变笨）。
    ⇒ 直接解析 build.spec，断言清单与 `local_llm/` 落盘布局一致。

跑法：python tests\\test_frozen_scripts.py
"""
import json
import os
import subprocess
import sys
import tempfile

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)

from core import app_paths                                  # noqa: E402

LOCAL_LLM = os.path.join(P, "local_llm")
MAIN = os.path.join(P, "main.py")
PY = sys.executable

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def run(args, cwd=None, extra_env=None, timeout=180):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.update(extra_env or {})
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def spec_analysis():
    """用桩执行 build.spec，取回 Analysis 的 datas/pathex 与 EXE 参数（**不真打包**）。"""
    spec = os.path.join(P, "build.spec")
    src = open(spec, encoding="utf-8").read()
    rec = {}

    class _Analysis:                        # noqa: N801
        def __init__(self, *a, **k):
            rec["datas"] = list(k.get("datas") or [])
            rec["pathex"] = list(k.get("pathex") or [])
            self.pure, self.zipped_data = [], []
            self.scripts, self.binaries, self.zipfiles = [], [], []
            self.datas = list(rec["datas"])          # spec 会把 a.datas 传给 EXE()

    namespace = {
        "SPEC": spec,
        "Analysis": lambda *a, **k: _Analysis(*a, **k),
        "PYZ": lambda *a, **k: object(),
        "EXE": lambda *a, **k: rec.update(**k),
    }
    exec(compile(src, spec, "exec"), namespace)             # noqa: S102
    rec["src"] = src
    return rec


def main():
    print("=== P5-2 打包态脚本通道 测试 ===")

    # ---------------- ① script_cmd 两态
    check("源码态 script_cmd = [python, 脚本, ...]（与改造前一致）",
          app_paths.script_cmd("x.py", "--json")[:2] == [PY, "x.py"],
          " ".join(app_paths.script_cmd("x.py", "--json")))

    real_frozen = getattr(sys, "frozen", None)
    real_exe = sys.executable
    try:
        sys.frozen = True                                   # type: ignore[attr-defined]
        sys.executable = os.path.join(tempfile.gettempdir(), "NetOpsTerminal.exe")
        cmd = app_paths.script_cmd("x.py", "--json", "--out", "o.json")
        check("打包态 script_cmd = [exe, --script, 脚本, ...]（不是再开一个应用）",
              cmd[:3] == [sys.executable, "--script", "x.py"] and cmd[3:] == ["--json", "--out", "o.json"],
              " ".join(cmd))
        check("打包态 base_dir() = exe 所在目录（--script 的解析基准）",
              os.path.abspath(app_paths.base_dir()) == tempfile.gettempdir())
    finally:
        if real_frozen is None:
            del sys.frozen                                   # type: ignore[attr-defined]
        else:
            sys.frozen = real_frozen                         # type: ignore[attr-defined]
        sys.executable = real_exe

    # ---------------- ② local_llm 侧的转调（脚本自己 import paths 就能用）
    if LOCAL_LLM not in sys.path:
        sys.path.insert(0, LOCAL_LLM)
    import paths as llm_paths                                # noqa: E402
    check("paths.script_cmd 与 core/app_paths 同源（只此一份实现）",
          llm_paths.script_cmd("run_server.py", "--stop") == app_paths.script_cmd("run_server.py", "--stop"),
          " ".join(llm_paths.script_cmd("run_server.py", "--stop")))

    # ---------------- ③ probe_tier.py --out 与 stdout 同源
    tmpdir = tempfile.mkdtemp(prefix="ait_frozen_")
    outs = os.path.join(tmpdir, "probe.json")
    r = run([PY, os.path.join(LOCAL_LLM, "probe_tier.py"), "--json", "--out", outs])
    check("probe_tier.py --json --out 退出码 0", r.returncode == 0, (r.stderr or "")[:150])
    ok_json, stdout_keys, file_keys = False, [], []
    if os.path.exists(outs):
        try:
            file_keys = sorted(json.loads(open(outs, encoding="utf-8").read()).keys())
            stdout_keys = sorted(json.loads(r.stdout or "{}").keys())
            ok_json = True
        except ValueError as e:
            check("--out 写出的是合法 JSON", False, str(e))
    else:
        check("--out 写出了文件（打包态唯一的回传通道）", False, outs)
    if ok_json:
        check("--out 写出的是合法 JSON", True, f"{os.path.getsize(outs)}B")
        check("两个通道同源：键集合一致（16 个）",
              stdout_keys == file_keys and len(file_keys) == 16,
              f"stdout={len(stdout_keys)} file={len(file_keys)}")

    # ---------------- ④ probe() 进程内路径 + 子进程兜底
    from core import local_model                             # noqa: E402
    p_info = local_model.probe(force=True)
    check("probe() 进程内拿到 ok=True（打包态唯一可靠路径）",
          p_info.get("ok") is True, str(p_info.get("error"))[:150])
    need = ["ok", "tier", "model", "models_dir", "device", "n_gpu_layers", "ram_ok",
            "ram_note", "reason", "ram_total_gb", "ram_avail_gb", "model_weight_mb"]
    check("probe() 结果含 decide()/_main() 依赖的全部键",
          all(k in p_info for k in need),
          "缺: " + ",".join(k for k in need if k not in p_info))
    sub = local_model._probe_subprocess()
    check("子进程兜底（script_cmd + --out 文件回传）也能拿到结果",
          sub.get("ok") is True, str(sub.get("error"))[:150])

    # ---------------- ⑤ main.py --script 通道
    script = os.path.join(tmpdir, "mini.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("import sys\nprint('MINI', sys.argv[1:])\nsys.exit(3)\n")
    r = run([PY, MAIN, "--script", script, "a", "b"])
    check("main.py --script 能跑脚本且透传退出码（期望 rc=3）",
          r.returncode == 3 and "MINI ['a', 'b']" in (r.stdout or ""),
          f"rc={r.returncode} out={(r.stdout or '').strip()[:80]}")

    # 相对名解析：交付机上 .bat 写的是 `local_llm\xxx.py`（datas 解压目录每次启动都变）
    r = run([PY, MAIN, "--script", os.path.join("local_llm", "probe_tier.py"), "--json"],
            cwd=tmpdir)
    check("main.py --script 支持“基准目录/相对名”（.bat 不必知道 _MEIPASS）",
          r.returncode == 0 and '"tier"' in (r.stdout or ""),
          f"rc={r.returncode} out={(r.stdout or '').strip()[:60]}")

    r = run([PY, "-c", "import sys; sys.path.insert(0, r'%s'); import main; "
                       "print('PyQt6' in sys.modules)" % P])
    check("--script 通道不依赖 Qt（main 模块级不再 import PyQt6）",
          (r.stdout or "").strip() == "False", (r.stdout or "").strip())

    log = os.path.join(P, "aiterminal-script.log")
    had_log = os.path.exists(log)
    r = run([PY, MAIN, "--script", os.path.join(tmpdir, "nope_xyz.py")])
    check("脚本不存在时返回非 0", r.returncode != 0, f"rc={r.returncode}")
    check("脚本不存在时把原因写进 aiterminal-script.log（无控制台时唯一可见途径）",
          os.path.exists(log) and "nope_xyz.py" in open(log, encoding="utf-8").read())
    if not had_log:                                          # 别在仓库里留测试垃圾
        try:
            os.remove(log)
        except OSError:
            pass

    # ---------------- ⑥ build.spec 的打包清单（datas 漏文件 ⇒ 打包态静默降级）
    rec = spec_analysis()
    want = ("paths.py", "probe_tier.py", "run_server.py", "verify_delivery.py",
            "retrieve.py", "embed_chunks.py", "prompt_local_assistant.txt")
    got = {os.path.basename(src): dst for src, dst in rec["datas"]}
    missing = [n for n in want if n not in got]
    check("build.spec 把 local_llm 随包脚本收进 datas（缺一个就 import 失败）",
          not missing, "缺: " + ",".join(missing) if missing else f"{len(want)} 个齐")
    check("datas 的目标目录是 `local_llm`（与 core 里 base/local_llm 的解析一致）",
          all(got.get(n) == "local_llm" for n in want if n in got))
    check("datas 里的源文件都真实存在", all(os.path.exists(s) for s, _ in rec["datas"]))
    check("pathex 含 local_llm（静态分析也能找到 paths/probe_tier 这些裸 import）",
          os.path.abspath(os.path.join(P, "local_llm")) in
          [os.path.abspath(p) for p in rec["pathex"]], str(rec["pathex"]))
    check("仍是 onefile + windowed（没被这次改造改成 onedir/带控制台）",
          "COLLECT" not in rec["src"] and rec.get("console") is False
          and rec.get("windowed") is True)
    check("5GB 的 knowledge / 30MB 的 _vendor_cmd **没有**被打进包",
          not any(("knowledge" in s or "_vendor_cmd" in s) for s, _ in rec["datas"]))

    print(f"\n{'=' * 70}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n in FAIL:
        print("  FAIL: " + n)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
