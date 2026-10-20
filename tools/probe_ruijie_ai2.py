#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""锐捷官网文档中心的 **AZAI 智能问答** 端到端实测（子代理报告说"入口被注释停用"，实测推翻）。

已知（2026-10-18 浏览器实测）：
- 页面 HTML/BODY 带 `azai-entry-active`；加载 `…/AI-conversation/{css,js}/ai-*.js`
  （ai-api / ai-panel / ai-chat / ai-message / ai-citation-validation / ai-human-ws / katex）
- 接口：`https://web-e-gw.ruijie.com.cn/api/dialogue-api-service`，前缀 `/api/azai`，SSE 流式 `/chat`
- DOM：`#AI-panel`、`.AI-panel__window`、`.ai-chat__input`、`.AI-panel__composer`
未知 ⇒ 本探针取：**入口触发元素**、发送按钮、回答区、以及端到端能不能出答案。

用法：python tools/probe_ruijie_ai2.py
"""
from __future__ import annotations

import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

URL = "https://www.ruijie.com.cn/fw/wd-sp-lxznywpts/"
Q = "display vlan 命令的作用和输出字段解释"

ENTRY_JS = r"""
() => [...document.querySelectorAll('*')]
  .filter(e => /entry|azai|ai-panel|aiPanel/i.test((e.id || '') + ' ' + (e.getAttribute('class') || '')))
  .map(e => [e.tagName, e.id || '', (e.getAttribute('class') || '').slice(0, 80),
             (e.innerText || '').replace(/\s+/g, ' ').slice(0, 50),
             (e.offsetParent !== null || e.getClientRects().length) ? 'visible' : 'hidden'])
"""

PANEL_JS = r"""
() => {
  const p = document.getElementById('AI-panel');
  const q = (s) => [...document.querySelectorAll(s)];
  return {
    panel: !!p,
    panelText: p ? (p.innerText || '').replace(/\s+/g, ' ').slice(0, 400) : '',
    inputs: q('.ai-chat__input, textarea, [contenteditable=true]')
      .map(e => [e.tagName, e.getAttribute('class') || '', e.getAttribute('placeholder') || '',
                 (e.offsetParent !== null || e.getClientRects().length) ? 'visible' : 'hidden']),
    senders: q('[class*=send], [class*=Send], button').slice(0, 30)
      .map(e => [e.tagName, (e.getAttribute('class') || '').slice(0, 60),
                 e.getAttribute('aria-label') || '', (e.innerText || '').trim().slice(0, 30)]),
    msgNodes: q('.ai-chat__round-item, .ai-chat__messages-wrap, .ai-chat__user-text, [class*=message]')
      .slice(0, 12).map(e => [e.getAttribute('class') || '', (e.innerText || '').length]),
  };
}
"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    out = [f"=== 锐捷 AZAI :: {URL}", f"QUESTION: {Q}"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        reqs = []
        page.on("request", lambda r: reqs.append(r.url))
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(4000)

        out.append("== 入口候选（id/class 含 entry|azai|ai-panel）==")
        for e in page.evaluate(ENTRY_JS):
            out.append("   " + " | ".join(str(x) for x in e))

        # ---- 点开入口 ----
        opened = False
        for sel in ("#azai-entry", ".azai-entry", "[class*=azai-entry]", "[id*=azai]",
                    "[class*=ai-entry]", "[class*=AI-entry]"):
            loc = page.locator(sel)
            try:
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=5000)
                    out.append(f"✓ 已点入口：{sel}（{loc.count()} 个）")
                    opened = True
                    break
            except Exception as exc:                          # noqa: BLE001
                out.append(f"   {sel} 点了没用：{exc}")
        if not opened:
            out.append("!! 没找到入口元素：见上面的候选清单")
        page.wait_for_timeout(4000)

        st = page.evaluate(PANEL_JS)
        out.append(f"== 面板状态 ==  panel={st['panel']}  text={st['panelText'][:200]}")
        out.append("-- 输入框 --")
        for i in st["inputs"]:
            out.append("   " + " | ".join(str(x) for x in i))
        out.append("-- 发送/按钮 候选 --")
        for s in st["senders"]:
            out.append("   " + " | ".join(str(x) for x in s))
        page.screenshot(path=os.path.join(os.environ.get("TEMP", "."), "probe_ruijie_ai2_panel.png"))

        # ---- 提问 ----
        n0 = len(reqs)
        box = None
        for sel in (".ai-chat__input", ".AI-panel__composer textarea", "textarea", "[contenteditable=true]"):
            loc = page.locator(sel)
            try:
                if loc.count() and loc.first.is_visible():
                    box = loc.first
                    out.append(f"✓ 用输入框：{sel}")
                    break
            except Exception:                                 # noqa: BLE001
                continue
        if box is not None:
            try:
                box.click()
                box.fill(Q)
                page.wait_for_timeout(500)
                sent = False
                for sel in (".ai-chat__send", "[class*=send-btn]", ".AI-panel__composer-bar button",
                            "button[class*=send]"):
                    loc = page.locator(sel)
                    if loc.count():
                        try:
                            loc.first.click(timeout=5000)
                            out.append(f"✓ 点发送：{sel}")
                            sent = True
                            break
                        except Exception:                     # noqa: BLE001
                            continue
                if not sent:
                    box.press("Enter")
                    out.append("✓ 用回车发送")
            except Exception as exc:                          # noqa: BLE001
                out.append(f"!! 提问失败：{exc}")

            prev, stable = "", 0
            for i in range(20):
                page.wait_for_timeout(4000)
                txt = page.evaluate("() => { const p = document.getElementById('AI-panel');"
                                    " return p ? (p.innerText || '') : ''; }")
                out.append(f"   轮{i}: 面板 {len(txt)} 字")
                if txt and txt == prev:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                prev = txt
            out.append("== 面板最终文本（末尾 2000 字）==")
            out.append(prev[-2000:])
        else:
            out.append("!! 面板里没有可见输入框")

        out.append("== 疑似接口请求 ==")
        for u in reqs[n0:]:
            if re.search(r"azai|dialogue|gw\.ruijie|chat", u, re.I):
                out.append("   " + u[:200])
        page.screenshot(path=os.path.join(os.environ.get("TEMP", "."), "probe_ruijie_ai2_answer.png"))
        ctx.close()
        browser.close()

    dest = os.path.join(os.environ.get("TEMP", "."), "probe_ruijie_ai2.txt")
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("\n".join(out[:60]))
    print(f"\n…（共 {len(out)} 行）✓ 落盘：{dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
