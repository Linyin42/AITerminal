#!/usr/bin/env python
"""DeepSeek 真实 API 联调：AI 对话 / 生成配置 / 危险命令识别 / 错误处理。

用应用自己的 Settings + AIAgent（provider=deepseek, model=deepseek-chat），
不 mock 网络，走真接口。只读：生成出来的配置**只打印不下发**。

用法：.venv-train\\Scripts\\python.exe tools\\probe_ai_deepseek.py
退出码 0 = 全过。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import Settings          # noqa: E402
from core.ai_agent import AIAgent             # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("  " + detail if detail else ""))


def mask(key: str) -> str:
    return (key[:8] + "****" + key[-4:]) if len(key) > 14 else "**"


def ask(agent: AIAgent, errs: list, prompt: str, max_tokens: int = 400):
    t0 = time.monotonic()
    text, commands = agent.converse(prompt, max_tokens=max_tokens)
    return text, commands, time.monotonic() - t0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s = Settings()
    key = s.api_key or ""
    model = s.get("model") or ""
    base = ((s.get("provider_configs") or {}).get(s.ai_provider) or {}).get("base_url", "")
    print(f"提供方={s.ai_provider}  模型={model}  base_url={base}  key={mask(key)}")
    if not key:
        print("没有可用 API Key，退出")
        return 1

    agent = AIAgent(api_key=key, model=model, provider=s.ai_provider, base_url=base)
    errs: list[str] = []
    agent.error_occurred.connect(lambda m: errs.append(m))

    # P1 连通 / 鉴权
    print("--- P1 连通与鉴权 ---")
    text, cmds, dt = ask(agent, errs, "只回复这一句：连接正常。不要解释。", max_tokens=40)
    record("一次对话拿到回复", bool(text.strip()) and not errs,
           f"{dt:.1f}s 回复={text.strip()[:40]!r}" + (f" err={errs[:1]}" if errs else ""))

    # P2 生成配置（锐捷语义）
    print("--- P2 生成配置（锐捷 S2928G-E / RGOS）---")
    errs.clear()
    prompt = ("为一台锐捷 S2928G-E（RGOS 10.4）生成配置：创建 VLAN 100 并命名为 TEST，"
              "把接口 GigabitEthernet 0/10 划入 VLAN 100（access 模式）。"
              "只给命令，用 ```text 代码块，每条一行。")
    text2, cmds2, dt2 = ask(agent, errs, prompt, max_tokens=500)
    blob = "\n".join(cmds2).lower()
    record("解析出命令列表（非空）", bool(cmds2), f"{len(cmds2)} 条：{cmds2[:6]}")
    record("命令包含建 VLAN", "vlan 100" in blob or "vlan100" in blob, f"{[c for c in cmds2 if 'vlan' in c.lower()][:3]}")
    record("命令包含端口划入", "0/10" in blob, f"{[c for c in cmds2 if '0/10' in c][:3]}")
    record("回复里带说明文字或直接给命令", bool(text2.strip()) or bool(cmds2), f"说明 {len(text2)} 字符")
    if errs:
        record("P2 无接口错误", False, str(errs[:2]))

    # P3 危险命令识别（生成的命令要能被拦截器认出来）
    print("--- P3 危险命令识别 ---")
    cases = [
        # (命令, 期望判为危险, 说明)
        ("reload", True, "重启"),
        ("reload in 5", True, "延迟重启"),
        ("show version", False, "只读"),
        ("vlan 100", False, "建 VLAN"),
        ("switchport access vlan 100", False, "划端口"),
        ("no vlan 100", True, "删整个 VLAN（本轮补）"),
        ("undo vlan 100", True, "删整个 VLAN（H3C 写法）"),
        ("no acl 3000", True, "删 ACL"),
        ("no ip route 0.0.0.0 0 172.17.1.1", True, "删默认路由"),
        ("undo ip route-static 0.0.0.0 0 172.17.1.1", True, "删默认路由（H3C）"),
        ("no shutdown", False, "恢复接口（安全）"),
        ("undo shutdown", False, "恢复接口（H3C，安全）"),
        ("no ip address 10.0.0.1 24", False, "改地址（日常操作，放行）"),
    ]
    bad_cases = []
    for cmd, want, why in cases:
        got = AIAgent.is_dangerous(cmd)
        if got != want:
            bad_cases.append(f"{cmd!r} 期望{want} 实际{got}")
    record(f"危险命令判定 {len(cases)} 条全部符合预期", not bad_cases, "；".join(bad_cases))
    gen_danger = [c for c in cmds2 if AIAgent.is_dangerous(c)]
    record("生成的配置没有被判为危险命令", not gen_danger, f"命中={gen_danger}")

    # P4 错 key 的错误处理（必须优雅报错，不能崩）
    print("--- P4 错误 Key 的处理 ---")
    bad = AIAgent(api_key="sk-invalid-000000000000000000000000", model=model,
                  provider=s.ai_provider, base_url=base)
    bad_errs: list[str] = []
    bad.error_occurred.connect(lambda m: bad_errs.append(m))
    t0 = time.monotonic()
    btext, bcmds = bad.converse("你好", max_tokens=20)
    dtb = time.monotonic() - t0
    record("错 key 优雅失败（有空回复+错误信息，不抛异常）",
           btext == "" and bool(bad_errs),
           f"{dtb:.1f}s err={bad_errs[:1]}")

    ok_n = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n结论：{ok_n}/{len(RESULTS)} 通过   延迟：P1={dt:.1f}s P2={dt2:.1f}s")
    out = os.path.join(os.environ.get("TEMP", "."), "ait_ai_deepseek.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"model": model, "provider": s.ai_provider,
                   "results": [{"name": n, "ok": o, "detail": d} for n, o, d in RESULTS],
                   "sample_commands": cmds2, "sample_text": text2[:800]},
                  fh, ensure_ascii=False, indent=1)
    print(f"明细：{out}")
    return 0 if ok_n == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
