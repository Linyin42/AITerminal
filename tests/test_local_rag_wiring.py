# -*- coding: utf-8 -*-
"""验证**向量 RAG 已接进应用** `ai.ask`（本地提供方），且两条退路都真的能退。

背景：`local_llm/retrieve.py` 那套（结构切片 + bge-m3 向量 + numpy 余弦 + 专用提示词）
此前只建好没接线 —— 应用侧 `ai.ask` 一直走旧词法层 `core/knowledge.py`（没有「小节」）
加一句两句话的通用 system。本测试钉住接线后的行为与**降级安全**。

做法：打桩 `core.llm.LLMClient` 把真正发出去的 system/user 截下来看；检索层是真的
（读真索引、算真向量）——不起对话服务、不联网。需要一个有向量库的厂商（默认 h3c）
与 :8081 嵌入服务在跑（h3c 的嵌入任务通常正在跑）。

跑法：python tests\\test_local_rag_wiring.py [--vendor h3c]
"""
import argparse
import os
import sys
import tempfile

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)

_CFG_DIR = tempfile.mkdtemp(prefix="ait_cfg_rag_")
_CFG = os.path.join(_CFG_DIR, "settings.json")
os.environ["AIT_CONFIG_FILE"] = _CFG          # 必须在任何 Settings() 之前

PASS, FAIL = [], []
CAPTURED = {}


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def install_fake_client():
    import core.llm as llm_mod

    class FakeClient:
        def __init__(self, provider="", api_key="", model="", base_url="", **kw):
            self.provider = provider

        def chat(self, system, messages, max_tokens=1024, temperature=0.3):
            CAPTURED["system"] = system
            CAPTURED["user"] = (messages or [{}])[0].get("content", "")
            return "（打桩回答）"

    llm_mod.LLMClient = FakeClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", default="h3c")
    args = ap.parse_args()

    from config.settings import Settings
    from core import local_rag
    from engine.service import EngineService

    class _Emitter:
        def notify(self, *a, **k):
            pass

    s = Settings(_CFG)
    s.ai_provider = "local"
    s.set("knowledge_enabled", True)
    s.set("knowledge_dir", os.path.join(P, "knowledge"))
    s.save()
    install_fake_client()
    svc = EngineService(_Emitter())
    svc._knowledge = lambda: type("KB", (), {"search": lambda self, q, top_k=3: []})()

    print("=== 向量 RAG 接线（本地提供方）===")
    print(f"       {local_rag.stat()}")
    have = args.vendor in local_rag.vendors()
    check(f"厂商 {args.vendor} 有向量库（前置条件）", have, str(local_rag.vendors()))
    if not have:
        print("       （没有向量库 → 只跑降级用例）")

    # ---- 1) 有向量库：走向量 RAG + 专用提示词 ----
    if have:
        res = svc.dispatch("ai.ask", {"question": "H3C 配置静态路由的命令",
                                      "vendor": args.vendor, "top_k": 3})
        system, user = CAPTURED.get("system", ""), CAPTURED.get("user", "")
        print(f"       system 前 60 字：{system[:60]!r}")
        print(f"       user  = {user!r}")
        print(f"       接地 {len(res.get('sources') or [])} 条；"
              f"首条出处 {((res.get('sources') or [{}])[0])}")
        check("用了专用提示词（含【厂商资料】段）", "【厂商资料】" in system, "system")
        check("用了专用提示词（含【未查证】出口与标注来源规则）",
              "【未查证】" in system and "来源：" in system, "system")
        check("资料以向量 RAG 格式注入（[资料 N] + 文件 + 小节）",
              "[资料 1]" in system and "文件：" in system and "小节：" in system, "system")
        check("user 只发问题本身（资料在提示词里，不再重复塞一遍）",
              user == "H3C 配置静态路由的命令", repr(user[:40]))
        check("sources 带出「小节」（词法层给不出这个字段）",
              all(("section" in x) for x in (res.get("sources") or []))
              and any((x.get("section") or "") for x in (res.get("sources") or [])),
              str((res.get("sources") or [{}])[0]))
        check("本次标记为有资料（usedKnowledge=True）", res.get("usedKnowledge") is True,
              str(res.get("usedKnowledge")))
        check("没报错", not res.get("error"), str(res.get("error"))[:60])
        check("占位符都被替换掉了（不该残留 {context}/{vendor}/{model}）",
              "{context}" not in system and "{vendor}" not in system, "system")

    # ---- 2) 厂商不存在：必须**静默退回**词法层，不能抛 ----
    CAPTURED.clear()
    res2 = svc.dispatch("ai.ask", {"question": "随便问问", "vendor": "不存在的厂商",
                                   "top_k": 2})
    system2 = CAPTURED.get("system", "")
    print(f"       降级后 system 前 50 字：{system2[:50]!r}")
    check("厂商不存在时不抛异常、正常返回", "error" not in res2 or not res2.get("error"),
          str(res2.get("error"))[:60])
    check("  退回词法层的通用提示词（没有【厂商资料】段）",
          "【厂商资料】" not in system2 and system2 != "", system2[:40])

    # ---- 3) 云端路径不受影响：仍是通用提示词 + 资料塞在 user 里 ----
    cfg = Settings(_CFG)
    cfg.ai_provider = "deepseek"
    cfg.set_api_key_for("deepseek", "sk-test")
    cfg.save()
    svc._knowledge = lambda: type("KB", (), {"search": lambda self, q, top_k=3: [
        {"text": "命令说明" * 50, "source": r"C:\docs\x.md", "score": 1.0}]})()
    CAPTURED.clear()
    res3 = svc.dispatch("ai.ask", {"question": "VLAN 有什么作用", "top_k": 2})
    system3, user3 = CAPTURED.get("system", ""), CAPTURED.get("user", "")
    print(f"       云端 system 前 40 字：{system3[:40]!r}；user 前 20 字：{user3[:20]!r}")
    check("云端：仍是通用提示词（没被本地那套影响）",
          "【厂商资料】" not in system3 and "只根据下面提供的资料" in system3, system3[:40])
    check("云端：资料仍塞在 user 里（老行为不变）", user3.startswith("资料："), user3[:20])

    print(f"\n=== 合计 PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    for name in FAIL:
        print(f"   FAIL: {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
