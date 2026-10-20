#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Juniper/Aruba「Ask AI」端到端取证：填 `#promptInput` → 提交 → 找**回答区**与发送按钮。

为什么单独写：`dump` 只能看静态 DOM（站点自有模态框里有个 #promptInput，但没有 Kapa）。
这里真提交一次问题，看回答渲染到哪个容器、走哪个接口。

用法：python tools/probe_juniper_askai.py [juniper|aruba]
"""
from __future__ import annotations

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SITES = {
    "juniper": ("https://networkingdocs.hpe.com/documentation/juniper/",
                "button[aria-label=\"Open Juniper Ask AI\"], button.ask-ai-btn, button:has-text(\"Ask AI\")"),
    "aruba": ("https://networkingdocs.hpe.com/documentation/",
              "button[aria-label*=\"Ask AI\"], button.ask-ai-btn, button:has-text(\"Ask AI\")"),
}
Q = "show interfaces terse 命令的作用和输出字段解释"

CONTAINER_JS = r"""
() => [...document.querySelectorAll('*')].filter(e => {
  const s = ((e.getAttribute('class') || '') + ' ' + (e.id || '')).toLowerCase();
  return /chat|message|answer|response|conversation|result|assistant|dialog|-modal|prompt|stream|markdown/.test(s)
         && e.children.length < 40;
}).map(e => [e.tagName, (e.getAttribute('class') || '').slice(0, 70), e.id || '',
             (e.innerText || '').length, (e.innerText || '').replace(/\s+/g, ' ').slice(0, 110)])
"""

BTN_JS = r"""
() => [...document.querySelectorAll('button,[role=button],a[href]')]
  .filter(e => e.offsetParent !== null || e.getClientRects().length)
  .map(e => [e.tagName, (e.getAttribute('class') || '').slice(0, 70), e.getAttribute('aria-label') || '',
             (e.innerText || '').trim().slice(0, 40)])
"""


def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "juniper").lower()
    url, click_sel = SITES[which]
    from playwright.sync_api import sync_playwright

    out = [f"=== {which} :: {url}", f"QUESTION: {Q}"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False,
                                     args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                                              "Chrome/131.0.0.0 Safari/537.36"))
        page = ctx.new_page()
        reqs = []
        page.on("request", lambda r: reqs.append(r.url))

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        try:
            page.locator(click_sel).first.click(timeout=8000)
            out.append("✓ 点开 Ask AI")
        except Exception as exc:                              # noqa: BLE001
            out.append(f"!! 点不开：{exc}")
        page.wait_for_timeout(2500)

        # ---- 提交问题 ----
        n_req_before = len(reqs)
        try:
            box = page.locator("#promptInput").first
            out.append(f"#promptInput count={page.locator('#promptInput').count()} visible={box.is_visible()}")
            box.click()
            box.fill(Q)
            page.wait_for_timeout(600)
            # 先看有没有发送按钮（模态框里的 submit）
            btns = [b for b in page.evaluate(BTN_JS) if b[3] and b[3].strip()]
            out.append("-- 提交按钮候选（可见按钮里有文案的）--")
            for b in btns:
                out.append("   " + " | ".join(str(x) for x in b))
            box.press("Enter")
            out.append("✓ 已按回车提交")
        except Exception as exc:                              # noqa: BLE001
            out.append(f"!! 提交失败：{exc}")

        # ---- 等文本稳定（无固定完成文案）----
        prev, stable, snapshot = "", 0, ""
        for i in range(40):
            page.wait_for_timeout(1500)
            try:
                snapshot = page.inner_text("body")
            except Exception:                                 # noqa: BLE001
                continue
            if snapshot == prev and len(snapshot) > 0:
                stable += 1
                out.append(f"   轮{i}: body {len(snapshot)} 字（稳定 {stable}/3）")
                if stable >= 3:
                    break
            else:
                stable = 0
                out.append(f"   轮{i}: body {len(snapshot)} 字")
            prev = snapshot

        page.screenshot(path=os.path.join(os.environ.get("TEMP", "."), f"probe_{which}_askai.png"),
                        full_page=False)
        out.append("")
        out.append("=== 提交后的候选容器 ===")
        for c in page.evaluate(CONTAINER_JS)[:60]:
            out.append("   " + " | ".join(str(x) for x in c))
        out.append("")
        out.append("=== 提交后可见按钮 ===")
        for b in page.evaluate(BTN_JS):
            out.append("   " + " | ".join(str(x) for x in b))
        out.append("")
        out.append("=== 提交后新发的请求（疑似接口）===")
        for u in reqs[n_req_before:]:
            out.append("   " + u[:200])
        out.append("")
        out.append(f"=== body 文本（{len(snapshot)} 字，末尾 2500 字）===")
        out.append(snapshot[-2500:])
        ctx.close()
        browser.close()

    dest = os.path.join(os.environ.get("TEMP", "."), f"probe_{which}_askai.txt")
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("\n".join(out[:40]))
    print(f"\n…（完整 {len(out)} 行）✓ 落盘：{dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
