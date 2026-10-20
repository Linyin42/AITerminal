# -*- coding: utf-8 -*-
"""验证 P0 的判据（原文）："应用内问一句「VLAN 有什么作用」，能在 AI 面板拿到回答（不经云端）"。

关键：走**应用自己的 RPC 通道** `EngineService.dispatch("ai.ask", …)` —— 这才等于
"AI 面板那条路"。只测 `LLMClient` 是测不到 `_ai_config` / 知识接地 / 错误分支的
（本轮就是这么抓到：`_ai_config` 在**免密钥**提供方上返回空 key，`ai.ask` 直接
回"还没有配置 local 的 API 密钥"拒答，而本地根本不需要密钥）。

前提与代价：
  · 本地模型服务要么已在跑，要么本脚本用 **CPU 档**自己拉起来 —— CPU 档零显存占用，
    不与并行的嵌入任务抢那 6GB 显存；脚本只停"自己拉起来的那一个"（`--stop` 现在
    也只会停 :8080，不会误杀 :8081 嵌入服务）。
  · 配置写进**临时文件**（`AIT_CONFIG_FILE`，见 engine/service.py:205-215）——
    绝不碰用户真实 settings.json。

跑法：python tests\\test_local_llm_app_path.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)

# ⚠ 必须在**任何** Settings() 之前设好：Settings 是 __new__ 单例，谁先构造谁决定配置路径
_CFG_DIR = tempfile.mkdtemp(prefix="ait_cfg_")
_CFG = os.path.join(_CFG_DIR, "settings.json")
os.environ["AIT_CONFIG_FILE"] = _CFG

LOCAL_LLM = os.path.join(P, "local_llm")
PY = sys.executable
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def local_up():
    """本地对话服务(:8080)活着吗 —— 直接问它的 /health，不看 pid 文件。"""
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/health",
                                     headers={"Authorization": "Bearer local-llm"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return b"ok" in r.read()
    except Exception:  # noqa: BLE001
        return False


def main():
    from config.settings import Settings
    s = Settings(_CFG)
    s.ai_provider = "local"
    s.set("ai_auto_search", False)                 # 测试不该联网
    s.set("knowledge_enabled", True)               # 本地接地要开（"不经云端"的关键）
    s.set("knowledge_dir", os.path.join(P, "knowledge"))
    s.save()

    s2 = Settings(_CFG)
    print(f"     临时配置 {_CFG}")
    print(f"     ai_provider={s2.ai_provider!r}  api_key={s2.api_key!r}（应为空：本地免密钥）")
    check("ai_provider 认得出新加的 local（旧坑：不在 PROVIDERS 里会被静默改回 anthropic）",
          s2.ai_provider == "local", s2.ai_provider)
    check("本地提供方确实没有用户密钥（后面要靠兜底）", s2.api_key == "", repr(s2.api_key))

    started = False
    try:
        if not local_up():
            print("     本地服务没在跑 → 用 CPU 档拉起来（零显存占用）")
            r = subprocess.run([PY, os.path.join(LOCAL_LLM, "run_server.py"), "--tier", "cpu"],
                               cwd=LOCAL_LLM, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=600)
            started = (r.returncode == 0)
            print(f"     启动 rc={r.returncode}  现在 up={local_up()}")
            for line in (r.stdout or "").splitlines()[-3:]:
                print(f"       {line}")
        check("本地服务已就绪(:8080/health → ok)", local_up(), str(local_up()))

        from engine.service import EngineService

        class _Emitter:                     # 与 tests/command_suggest_test.py 同款
            def notify(self, *args, **kwargs):
                pass

        svc = EngineService(_Emitter())
        check("methods 里注册了 ai.ask", "ai.ask" in svc.methods)

        st = svc.dispatch("ai.status", {})
        shown = {k: st.get(k) for k in ("provider", "model", "configured", "label") if k in st}
        print(f"     ai.status → {json.dumps(shown, ensure_ascii=False)}")

        question = "VLAN 有什么作用"
        t0 = time.time()
        res = svc.dispatch("ai.ask", {"question": question, "useKnowledge": True, "top_k": 2})
        elapsed = time.time() - t0
        answer = (res.get("answer") or "").strip()
        print(f"     用时 {elapsed:.1f}s")
        print(f"     回答 → {answer[:200]!r}")
        print(f"     error={res.get('error')!r}  接地资料 {len(res.get('sources') or [])} 条")

        check("ai.ask 没报错（尤其不能是「还没有配置 local 的 API 密钥」）",
              not res.get("error"), str(res.get("error"))[:80])
        check("拿到了非空回答 ⇒ 应用内通道打通（不经云端）", bool(answer), f"{len(answer)} 字")
        check("回答耗时在可接受范围（<5 分钟）", elapsed < 300, f"{elapsed:.1f}s")
    finally:
        if started:
            subprocess.run([PY, os.path.join(LOCAL_LLM, "run_server.py"), "--stop"],
                           cwd=LOCAL_LLM, capture_output=True, text=True, timeout=180)
            print("     已停掉脚本自己拉起的本地服务（只停 :8080）")

    print(f"\n=== 合计 PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    for n in FAIL:
        print(f"   FAIL: {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
