# -*- coding: utf-8 -*-
"""验证 `ai.ask` 的**注入预算**按提供方分档（本地收紧、云端放宽）。

为什么要专门测这个：本地模型跑在交付机上，首字延迟的大头是 prompt eval
（实测 ≈100 tok/s，1200 字上下文约 12s）。原先 `ai.ask` 是"每片 900 字 × top_k"，
top_k=3 时最坏 2700 字，与 `local_llm/retrieve.py` 定的"单片 ≤800、总量 ≤1200"不一致；
云端无所谓（多给资料更准、不占用户等待时间），所以按提供方分档。

⚠ 本用例测的是**词法检索那条路径的预算**。本地现在优先走向量 RAG（core/local_rag.py），
所以这里**故意把向量检索打桩成空**，把流程逼到词法回退路径上 —— 否则测的就不是预算，
而是向量 RAG 了（那条路径由 tests\\test_local_rag_wiring.py 负责）。

做法：打桩知识库（每片 1200 字）+ 打桩 LLMClient（把真正发出去的 user 提示词截下来量长度）。
不起服务、不联网、不消耗额度。

跑法：python tests\\test_ai_ask_context_budget.py
"""
import os
import sys
import tempfile

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)

_CFG_DIR = tempfile.mkdtemp(prefix="ait_cfg_budget_")
_CFG = os.path.join(_CFG_DIR, "settings.json")
os.environ["AIT_CONFIG_FILE"] = _CFG      # 必须在任何 Settings() 之前

PASS, FAIL = [], []
CAPTURED = {}


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


class FakeKB:
    """每片固定 1200 字，用来暴露"没按预算截断"的情况。"""
    def search(self, query, top_k=3):
        return [{"text": "命令说明" * 300, "source": rf"C:\docs\证据{i}.md", "score": 0.9}
                for i in range(max(1, top_k))]


def install_fake_client():
    """把 core.llm.LLMClient 换成假的（service.py 是函数内 import，调用时会取到）。"""
    import core.llm as llm_mod

    class FakeClient:
        def __init__(self, provider="", api_key="", model="", base_url="", **kw):
            self.provider = provider

        def chat(self, system, messages, max_tokens=1024, temperature=0.3):
            CAPTURED["system"] = system
            CAPTURED["user"] = (messages or [{}])[0].get("content", "")
            CAPTURED["max_tokens"] = max_tokens
            return "（打桩回答）"

    llm_mod.LLMClient = FakeClient


def main():
    from config.settings import Settings
    from engine.service import EngineService

    class _Emitter:
        def notify(self, *a, **k):
            pass

    s = Settings(_CFG)
    s.set("knowledge_enabled", True)
    s.set("knowledge_dir", os.path.join(P, "knowledge"))
    s.set_api_key_for("deepseek", "sk-test")
    s.save()
    install_fake_client()

    # 把向量检索打桩成"没有结果"，强制走词法回退路径（本用例要测的是那条路径的预算）
    import core.local_rag as local_rag
    local_rag.search = lambda *a, **k: []

    svc = EngineService(_Emitter())
    svc._knowledge = lambda: FakeKB()        # 打桩检索层

    print("=== ai.ask 注入预算分档 ===")

    # ---- 本地：单片 ≤800、总量 ≤1200 ----
    Settings(_CFG).ai_provider = "local"
    Settings(_CFG).save()
    res = svc.dispatch("ai.ask", {"question": "VLAN 有什么作用", "useKnowledge": True, "top_k": 3})
    user_local = CAPTURED.get("user", "")
    n = len(user_local)
    print(f"       本地 user 提示词 {n} 字；接地 {len(res.get('sources') or [])} 条")
    check("本地：提示词总量被压到 1200 字预算内（+少量框架文字）", n <= 1200 + 60, f"{n} 字")
    check("本地：确实带上了资料（不是把预算压成 0）", "【资料1】" in user_local, user_local[:40])
    # ⚠ 别用 `user.split("【资料")` 量单片：切出来的最后一段会把结尾的
    # `\n\n问题：…` 也算进去，量出来必然超 800（这是测试写错，不是代码错）。
    import re
    parts = re.findall(r"【资料\d+】(.*?)(?=\n\n【资料\d+】|\n\n问题：|\Z)",
                       user_local, re.S)
    check("本地：单片不超过 800 字", bool(parts) and all(len(b) <= 800 for b in parts),
          f"逐片 {[len(b) for b in parts]}")
    injected = user_local.split("问题：")[0]
    check("本地：整段注入（含『资料：』抬头）不超过 1200 字预算 + 少量框架文字",
          len(injected) <= 1200 + 20, f"{len(injected)} 字")

    # ---- 云端：放宽（5 片 × 900 = 4500），证明分档生效而不是一刀切 ----
    cfg = Settings(_CFG)
    cfg.ai_provider = "deepseek"
    cfg.save()
    res2 = svc.dispatch("ai.ask", {"question": "VLAN 有什么作用", "useKnowledge": True, "top_k": 5})
    user_cloud = CAPTURED.get("user", "")
    m = len(user_cloud)
    print(f"       云端 user 提示词 {m} 字；接地 {len(res2.get('sources') or [])} 条")
    check("云端：给足资料（不被 1200 字预算卡住）", m > 2000, f"{m} 字")
    check("云端：本地预算严格小于云端（分档真的生效）", n < m, f"本地 {n} < 云端 {m}")

    print(f"\n=== 合计 PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    for name in FAIL:
        print(f"   FAIL: {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
